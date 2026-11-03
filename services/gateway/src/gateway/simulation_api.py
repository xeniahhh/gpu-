"""Bounded asynchronous execution and persisted immutable v2 simulation records.

One coordinator process is supported. SQLite stores evidence, not a durable
execution queue: unfinished runs become interrupted when the service restarts.
"""
from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import BoundedSemaphore, RLock
from typing import Any, Callable
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse

ROOT = Path(__file__).resolve().parents[4]
INTEGRATION_EVIDENCE = ROOT / "evidence" / "integration_1103" / "provenance.json"


class SimulationBusy(RuntimeError):
    pass


class SimulationService:
    def __init__(self, database_path: str | Path | None = None, *, max_pending: int = 2,
                 runner: Callable[[dict], dict] | None = None) -> None:
        if max_pending < 0:
            raise ValueError("max_pending must be nonnegative")
        if database_path is None:
            base = Path(os.getenv("SQLITE_PATH", str(ROOT / "var" / "fabric_sandbox.db")))
            if not base.is_absolute():
                base = ROOT / base
            database_path = os.getenv("FABRIC_SIM_DB", str(base.with_name("simulations_v2.sqlite3")))
        database_path = str(database_path)
        if database_path != ":memory:":
            Path(database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = RLock()
        self.slots = BoundedSemaphore(max_pending + 1)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fabric-simulation")
        self.runner = runner
        self.closed = False
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS simulations_v2 (
              id TEXT PRIMARY KEY, status TEXT NOT NULL, input_hash TEXT NOT NULL,
              spec_json TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL,
              completed_at TEXT, error TEXT)""")
            self.db.execute("""UPDATE simulations_v2 SET status='interrupted',
              completed_at=?, error='Coordinator restarted; automatic continuation is not implemented'
              WHERE status IN ('queued','running')""", (self._now(),))

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def submit(self, spec: dict) -> dict:
        from fabric_sim.validation import canonical_hash, validate_spec
        frozen = validate_spec(spec)
        serialized = json.dumps(frozen, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(serialized.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("Simulation input exceeds 2 MiB")
        frozen = json.loads(serialized)
        input_hash = canonical_hash(frozen)
        if not self.slots.acquire(blocking=False):
            raise SimulationBusy("One simulation is running and the waiting queue is full")
        simulation_id = "sim-" + uuid4().hex
        try:
            with self.lock, self.db:
                if self.closed:
                    raise SimulationBusy("Simulation service is shutting down")
                self.db.execute("INSERT INTO simulations_v2 (id,status,input_hash,spec_json,created_at) VALUES (?,?,?,?,?)",
                                (simulation_id, "queued", input_hash, serialized, self._now()))
                self.executor.submit(self._execute, simulation_id, frozen)
        except Exception:
            self.slots.release()
            raise
        return {"id": simulation_id, "status": "queued", "input_hash": input_hash}

    def _execute(self, simulation_id: str, spec: dict) -> None:
        try:
            with self.lock, self.db:
                self.db.execute("UPDATE simulations_v2 SET status='running' WHERE id=?", (simulation_id,))
            if self.runner is None:
                from fabric_sim import simulate
                result = simulate(spec)
            else:
                result = self.runner(spec)
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
            with self.lock, self.db:
                self.db.execute("UPDATE simulations_v2 SET status='completed', result_json=?, completed_at=? WHERE id=?",
                                (encoded, self._now(), simulation_id))
        except Exception as exc:
            error = str(exc) if isinstance(exc, ValueError) else f"Simulation failed ({type(exc).__name__})"
            with self.lock, self.db:
                self.db.execute("UPDATE simulations_v2 SET status='failed', error=?, completed_at=? WHERE id=?",
                                (error[:1000], self._now(), simulation_id))
        finally:
            self.slots.release()

    def get(self, simulation_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM simulations_v2 WHERE id=?", (simulation_id,)).fetchone()
        if row is None:
            raise KeyError(simulation_id)
        return {"id": row["id"], "status": row["status"], "input_hash": row["input_hash"],
                "spec": json.loads(row["spec_json"]),
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "created_at": row["created_at"], "completed_at": row["completed_at"], "error": row["error"]}

    def list_recent(self, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT id,status,input_hash,created_at,completed_at,error FROM simulations_v2 ORDER BY created_at DESC LIMIT ?",
                                   (limit,)).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=True)
        with self.lock:
            self.db.close()


def explain_record(record: dict) -> dict:
    """Explain observed data with source references; no external LLM or writes."""
    result = record.get("result")
    if result is None:
        return {"mode": "rules_based_read_only", "simulation_id": record["id"],
                "input_hash": record["input_hash"], "status": record["status"],
                "observations": [{"text": "评估尚未生成完整结果，暂不能比较性能。", "source": "status"}],
                "assumptions": []}
    observations = []
    interesting = ("fail", "fault", "wait", "reject", "recover", "restart", "interrupt", "checkpoint", "ignored", "admit")
    for i, event in enumerate(result.get("events", [])):
        kind = str(event.get("kind", event.get("type", "")))
        if any(word in kind.lower() for word in interesting):
            subject = event.get("subject_id", event.get("job_id", event.get("request_id", "系统")))
            message = event.get("message") or event.get("reason") or kind
            observations.append({"text": f"模拟 {event.get('time_ms', 0):.3f} ms，{subject}：{message}",
                                 "source": f"events[{i}]", "event": event})
        if len(observations) >= 12:
            break
    if not observations:
        observations.append({"text": "未发现可直接引用的故障或等待事件；请结合汇总指标查看结果。", "source": "summary"})
    failed = [x for x in result.get("invariant_checks", []) if x.get("passed") is False]
    observations.append({"text": f"约束检查报告 {len(failed)} 项未通过。请注意检查项覆盖范围。", "source": "invariant_checks"})
    return {"mode": "rules_based_read_only", "simulation_id": record["id"], "input_hash": record["input_hash"],
            "observations": observations, "assumptions": result.get("assumptions", []),
            "boundary": "解释仅引用本次仿真记录；设定时间不是 GPU 实测，不能据此推断真实训练精度或在线迁移能力。"}


def load_integration_evidence() -> dict:
    """Return the curated A/B/C handoff summary without exposing raw secrets."""
    payload = json.loads(INTEGRATION_EVIDENCE.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported integration evidence schema")
    contributors = payload.get("contributors")
    if not isinstance(contributors, list) or {
        item.get("id") for item in contributors if isinstance(item, dict)
    } != {"A", "B", "C"}:
        raise ValueError("Integration evidence must contain A, B and C")
    return payload


def create_simulation_router(service: SimulationService) -> APIRouter:
    router = APIRouter()

    def get_record(simulation_id: str) -> dict:
        try:
            return service.get(simulation_id)
        except KeyError:
            raise HTTPException(404, "Simulation not found") from None

    @router.get("/", include_in_schema=False)
    def index():
        return RedirectResponse("/workload")

    @router.get("/legacy", include_in_schema=False)
    def legacy_page():
        return FileResponse(ROOT / "apps" / "web" / "dist" / "index.html", media_type="text/html")

    @router.get("/workload", include_in_schema=False)
    def workload_page():
        return FileResponse(ROOT / "apps" / "web" / "workload.html", media_type="text/html")

    @router.get("/api/v2/scenarios")
    def scenarios():
        from fabric_sim.ddp import demo_ddp
        from fabric_sim.tp import demo_tp
        definitions = [
            ("ddp", "competition", "DDP · 跨机架共享链路"),
            ("ddp", "compact", "DDP · 集中放置对照"),
            ("ddp", "admission", "DDP · 资源不足时等待"),
            ("ddp", "memory_reject", "DDP · 单卡显存不适配"),
            ("ddp", "fault_spare", "DDP · 故障后有备用资源"),
            ("ddp", "fault_no_spare", "DDP · 故障后没有备用资源"),
            ("ddp", "slow_rank", "DDP · 慢计算与同步等待"),
            ("tp", "baseline", "TP · 基准推理"),
            ("tp", "tp1", "TP · 单卡副本"),
            ("tp", "tp2", "TP · 两卡副本"),
            ("tp", "tp4", "TP · 四卡副本"),
            ("tp", "competition", "TP · 多副本共享网络"),
            ("tp", "kv_reject", "TP · KV 显存准入"),
            ("tp", "fault", "TP · 副本故障"),
        ]
        items = []
        for scenario, variant, label in definitions:
            factory = demo_ddp if scenario == "ddp" else demo_tp
            try:
                spec = factory(variant)
            except ValueError:
                continue
            items.append({"id": f"{scenario}:{variant}", "label": label, "spec": spec})
        return {"schema_version": 2, "scenarios": items,
                "boundary": "事件驱动的工作负载仿真；示例时延与显存峰值中标记 SYNTHETIC 的值不是实测。"}

    @router.get("/api/v2/evidence")
    def integration_evidence():
        try:
            return load_integration_evidence()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                503, f"Integrated evidence is unavailable: {exc}"
            ) from exc

    @router.post("/api/v2/simulations", status_code=202)
    def submit(spec: dict[str, Any]):
        try:
            return service.submit(spec)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except SimulationBusy as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "2"}) from exc

    @router.get("/api/v2/simulations")
    def recent(limit: int = Query(20, ge=1, le=100)):
        return {"simulations": service.list_recent(limit)}

    @router.get("/api/v2/simulations/{simulation_id}")
    def get(simulation_id: str):
        return get_record(simulation_id)

    @router.get("/api/v2/simulations/{simulation_id}/events")
    def events(simulation_id: str, offset: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=5000)):
        record = get_record(simulation_id)
        items = (record.get("result") or {}).get("events", [])
        return {"id": simulation_id, "status": record["status"], "mode": "completed_simulation_replay",
                "total": len(items), "offset": offset, "events": items[offset:offset + limit]}

    @router.post("/api/v2/simulations/{simulation_id}/explain")
    def explain(simulation_id: str):
        return explain_record(get_record(simulation_id))

    return router
