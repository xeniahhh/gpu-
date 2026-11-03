"""Prepare and verify a real coordinator restart against the C-owner state."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
PENDING_ID = "sim-task-c-interrupted"


def prepare(acceptance_path: Path, database_path: Path) -> dict:
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    accepted_id = acceptance["accepted_run_id"]
    with sqlite3.connect(database_path) as connection:
        accepted = connection.execute(
            "SELECT id,status,input_hash,result_json FROM simulations_v2 WHERE id=?",
            (accepted_id,),
        ).fetchone()
        if accepted is None:
            raise RuntimeError(f"Accepted run {accepted_id} is missing before restart")
        connection.execute(
            """INSERT OR REPLACE INTO simulations_v2
               (id,status,input_hash,spec_json,result_json,created_at,completed_at,error)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                PENDING_ID,
                "running",
                "0" * 64,
                json.dumps({"schema_version": 2, "restart_probe": True}),
                None,
                datetime.now(UTC).isoformat(),
                None,
                None,
            ),
        )
    return {
        "accepted_run_id": accepted_id,
        "accepted_status_before": accepted[1],
        "accepted_input_hash_before": accepted[2],
        "pending_run_id": PENDING_ID,
        "pending_status_before": "running",
    }


def verify(base_url: str, acceptance_path: Path, output: Path) -> int:
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    accepted_id = acceptance["accepted_run_id"]
    submitted_check = next(
        item for item in acceptance["checks"] if item["name"] == "editor_submit_accepted"
    )
    expected_input_hash = submitted_check["actual"]["body"]["input_hash"]
    viewer_token = os.environ.get("TASK_C_VIEWER_TOKEN", "")
    if not viewer_token:
        raise RuntimeError("TASK_C_VIEWER_TOKEN must be set")
    headers = {"Authorization": f"Bearer {viewer_token}"}

    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        completed = client.get(f"/api/v2/simulations/{accepted_id}", headers=headers)
        completed.raise_for_status()
        completed_body = completed.json()
        interrupted = client.get(f"/api/v2/simulations/{PENDING_ID}", headers=headers)
        interrupted.raise_for_status()
        interrupted_body = interrupted.json()
        recent = client.get(
            "/api/v2/simulations", params={"limit": 100}, headers=headers
        )
        recent.raise_for_status()
        recent_ids = {item["id"] for item in recent.json()["simulations"]}

    checks = [
        {
            "name": "completed_record_survives_restart",
            "passed": completed_body["status"] == "completed"
            and completed_body["input_hash"] == expected_input_hash
            and completed_body["result"] is not None,
            "detail": "completed input, hash, and result remain queryable after process restart",
            "actual": {
                "status": completed_body["status"],
                "input_hash": completed_body["input_hash"],
                "has_result": completed_body["result"] is not None,
            },
        },
        {
            "name": "unfinished_record_marked_interrupted",
            "passed": interrupted_body["status"] == "interrupted"
            and "automatic continuation is not implemented" in (interrupted_body["error"] or ""),
            "detail": "a pre-existing running row becomes interrupted on coordinator startup",
            "actual": {
                "status": interrupted_body["status"],
                "error": interrupted_body["error"],
            },
        },
        {
            "name": "restart_records_are_listed",
            "passed": {accepted_id, PENDING_ID}.issubset(recent_ids),
            "detail": "both completed and interrupted records remain discoverable",
            "actual": {
                "accepted_listed": accepted_id in recent_ids,
                "interrupted_listed": PENDING_ID in recent_ids,
            },
        },
    ]
    payload = {
        "schema_version": 1,
        "task": "C",
        "scope": "same state directory after coordinator restart",
        "tested_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "accepted_run_id": accepted_id,
        "interrupted_run_id": PENDING_ID,
        "checks": checks,
        "passed": all(item["passed"] for item in checks),
        "notes": [
            "The interrupted probe represents a run present before restart.",
            "The service persists evidence but does not provide a durable execution queue.",
            "Completed simulation success remains distinct from business job success.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output.resolve()),
        "checks": len(checks),
        "passed": payload["passed"],
    }, ensure_ascii=False))
    return 0 if payload["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument(
        "--acceptance",
        type=Path,
        default=ROOT / "evidence/task_c_1103/http_acceptance.json",
    )
    prepare_parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "var/task_c_1103/simulations.sqlite3",
    )

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    verify_parser.add_argument(
        "--acceptance",
        type=Path,
        default=ROOT / "evidence/task_c_1103/http_acceptance.json",
    )
    verify_parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "evidence/task_c_1103/restart_acceptance.json",
    )

    args = parser.parse_args()
    if args.command == "prepare":
        print(json.dumps(prepare(args.acceptance, args.database), ensure_ascii=False))
        return 0
    return verify(args.base_url, args.acceptance, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
