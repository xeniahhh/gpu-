"""Run the live C-role HTTP acceptance flow and write token-free evidence."""
from __future__ import annotations

import argparse
import json
import os
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


def _check(checks: list[dict[str, Any]], name: str, passed: bool, detail: Any) -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    editor_token = os.getenv("FABRIC_EDITOR_TOKEN", "")
    viewer_token = os.getenv("FABRIC_VIEWER_TOKEN", "")
    if not editor_token or not viewer_token:
        parser.error("FABRIC_EDITOR_TOKEN and FABRIC_VIEWER_TOKEN are required")

    editor = {"Authorization": "Bearer " + editor_token}
    viewer = {"Authorization": "Bearer " + viewer_token}
    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "kind": "LIVE_HTTP_ACCEPTANCE",
        "base_url": args.base_url,
    }

    with httpx.Client(base_url=args.base_url, timeout=20.0) as client:
        health = client.get("/api/health")
        _check(
            checks,
            "health is public",
            health.status_code == 200 and health.json().get("status") == "ok",
            {"status_code": health.status_code, "body": health.json()},
        )

        page = client.get("/workload")
        _check(
            checks,
            "workload page is served",
            page.status_code == 200
            and "工作负载与故障分析" in page.text
            and "SYNTHETIC" in page.text
            and "仿真记录回放" in page.text,
            {"status_code": page.status_code, "content_type": page.headers.get("content-type")},
        )

        anonymous = client.get("/api/v2/scenarios")
        _check(
            checks,
            "anonymous scenario read is rejected",
            anonymous.status_code == 401,
            {"status_code": anonymous.status_code},
        )

        scenario_response = client.get("/api/v2/scenarios", headers=viewer)
        scenarios = scenario_response.json().get("scenarios", [])
        selected = next(
            (item for item in scenarios if item.get("id") == "ddp:competition"), None
        )
        _check(
            checks,
            "viewer can load scenarios",
            scenario_response.status_code == 200 and selected is not None,
            {
                "status_code": scenario_response.status_code,
                "scenario_count": len(scenarios),
                "selected": "ddp:competition",
            },
        )
        if selected is None:
            raise RuntimeError("ddp:competition scenario is unavailable")

        scenario_spec = selected["spec"]
        viewer_submit = client.post("/api/v2/simulations", headers=viewer, json=scenario_spec)
        _check(
            checks,
            "viewer cannot submit",
            viewer_submit.status_code == 403,
            {"status_code": viewer_submit.status_code},
        )

        invalid_spec = deepcopy(scenario_spec)
        invalid_spec["horizon_ms"] = -1
        invalid = client.post("/api/v2/simulations", headers=editor, json=invalid_spec)
        _check(
            checks,
            "invalid input is rejected with 422",
            invalid.status_code == 422,
            {"status_code": invalid.status_code, "detail": invalid.json().get("detail")},
        )

        submitted = client.post("/api/v2/simulations", headers=editor, json=scenario_spec)
        submitted_body = submitted.json()
        simulation_id = submitted_body.get("id", "")
        input_hash = submitted_body.get("input_hash", "")
        _check(
            checks,
            "editor submission returns 202, id and input hash",
            submitted.status_code == 202 and simulation_id and len(input_hash) == 64,
            {
                "status_code": submitted.status_code,
                "id": simulation_id,
                "input_hash": input_hash,
                "initial_status": submitted_body.get("status"),
            },
        )
        if not simulation_id:
            raise RuntimeError("simulation submission did not return an id")

        # Mutating the local draft must not alter the stored input snapshot.
        scenario_spec["horizon_ms"] = 1
        record: dict[str, Any] = {}
        statuses: list[str] = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            query = client.get(f"/api/v2/simulations/{simulation_id}", headers=viewer)
            record = query.json()
            statuses.append(str(record.get("status")))
            if record.get("status") in {"completed", "failed", "interrupted"}:
                break
            time.sleep(0.05)

        _check(
            checks,
            "running record can be queried until terminal",
            bool(statuses)
            and any(status in {"queued", "running"} for status in statuses)
            and record.get("status") == "completed",
            {"observed_statuses": statuses, "error": record.get("error")},
        )
        _check(
            checks,
            "submitted input snapshot is frozen",
            record.get("spec", {}).get("horizon_ms") != 1,
            {
                "stored_horizon_ms": record.get("spec", {}).get("horizon_ms"),
                "draft_horizon_ms_after_submit": 1,
            },
        )
        _check(
            checks,
            "result remains bound to submitted input hash",
            record.get("input_hash") == input_hash,
            {"submitted": input_hash, "stored": record.get("input_hash")},
        )

        events = client.get(
            f"/api/v2/simulations/{simulation_id}/events?offset=0&limit=5",
            headers=viewer,
        )
        events_body = events.json()
        _check(
            checks,
            "viewer can page completed event replay",
            events.status_code == 200
            and events_body.get("mode") == "completed_simulation_replay"
            and len(events_body.get("events", [])) <= 5,
            {
                "status_code": events.status_code,
                "mode": events_body.get("mode"),
                "total": events_body.get("total"),
                "returned": len(events_body.get("events", [])),
            },
        )

        explain = client.post(
            f"/api/v2/simulations/{simulation_id}/explain", headers=viewer
        )
        explain_body = explain.json()
        observations = explain_body.get("observations", [])
        _check(
            checks,
            "viewer explain is read-only and evidence-referenced",
            explain.status_code == 200
            and explain_body.get("mode") == "rules_based_read_only"
            and bool(observations)
            and all(item.get("source") for item in observations),
            {
                "status_code": explain.status_code,
                "mode": explain_body.get("mode"),
                "observation_count": len(observations),
                "sources": [item.get("source") for item in observations],
            },
        )

        missing = client.get("/api/v2/simulations/not-a-real-run", headers=viewer)
        _check(
            checks,
            "unknown simulation id is explicitly rejected",
            missing.status_code == 404,
            {"status_code": missing.status_code},
        )

        evidence["simulation"] = {
            "id": simulation_id,
            "input_hash": input_hash,
            "status": record.get("status"),
            "implementation_hash": (record.get("result") or {}).get(
                "implementation_hash"
            ),
            "summary": (record.get("result") or {}).get("summary"),
        }

    evidence["checks"] = checks
    evidence["passed"] = all(check["passed"] for check in checks)
    evidence["passed_count"] = sum(check["passed"] for check in checks)
    evidence["total_count"] = len(checks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print(
        f"C HTTP acceptance: {evidence['passed_count']}/{evidence['total_count']} "
        f"checks passed; evidence={args.output}"
    )
    return 0 if evidence["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
