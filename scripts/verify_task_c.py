"""Exercise the C-owner HTTP, permission, snapshot, and replay acceptance path."""
from __future__ import annotations

import argparse
import copy
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


def wait_for_terminal(
    client: httpx.Client,
    record_id: str,
    headers: dict[str, str],
    timeout_seconds: float = 30.0,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/api/v2/simulations/{record_id}", headers=headers)
        response.raise_for_status()
        record = response.json()
        if record["status"] not in {"queued", "running"}:
            return record
        time.sleep(0.05)
    raise TimeoutError(f"Simulation {record_id} did not finish in {timeout_seconds:.0f} seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path, default=Path("evidence/task_c_1103/http_acceptance.json"))
    parser.add_argument("--scenario-id", default="ddp:competition")
    parser.add_argument("--rejection-scenario-id", default="ddp:memory_reject")
    args = parser.parse_args()

    editor_token = os.environ.get("TASK_C_EDITOR_TOKEN", "")
    viewer_token = os.environ.get("TASK_C_VIEWER_TOKEN", "")
    if not editor_token or not viewer_token:
        parser.error("TASK_C_EDITOR_TOKEN and TASK_C_VIEWER_TOKEN must be set")

    editor_headers = {"Authorization": f"Bearer {editor_token}"}
    viewer_headers = {"Authorization": f"Bearer {viewer_token}"}
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str, actual: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail, "actual": actual})

    with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
        health = client.get("/api/health")
        check("health_without_token", health.status_code == 200, "health endpoint is public", {
            "status_code": health.status_code, "body": health.json()
        })

        page = client.get("/workload")
        check("workload_page_served", page.status_code == 200 and "/api/v2/scenarios" in page.text,
              "standalone workload page is served by Gateway", {
                  "status_code": page.status_code, "content_type": page.headers.get("content-type")
              })

        anonymous = client.get("/api/v2/simulations")
        check("anonymous_read_rejected", anonymous.status_code == 401,
              "bearer mode rejects an unauthenticated read", {"status_code": anonymous.status_code})
        bad_token = client.get("/api/v2/simulations", headers={"Authorization": "Bearer invalid"})
        check("unknown_token_rejected", bad_token.status_code == 401,
              "an unknown bearer token never inherits local editor access", {
                  "status_code": bad_token.status_code
              })

        scenarios_response = client.get("/api/v2/scenarios", headers=viewer_headers)
        scenarios_response.raise_for_status()
        scenarios = scenarios_response.json()["scenarios"]
        check("viewer_can_read_scenarios", len(scenarios) >= 2,
              "viewer can read scenario definitions", {"count": len(scenarios)})
        evidence_response = client.get("/api/v2/evidence", headers=viewer_headers)
        evidence_response.raise_for_status()
        evidence_body = evidence_response.json()
        evidence_ids = {item["id"] for item in evidence_body["contributors"]}
        check(
            "viewer_can_read_integrated_evidence",
            evidence_ids == {"A", "B", "C"} and len(evidence_body["implementation_hash"]) == 64,
            "viewer can read the integrated A/B/C evidence summary",
            {
                "contributors": sorted(evidence_ids),
                "implementation_hash": evidence_body["implementation_hash"],
            },
        )
        selected = next(item for item in scenarios if item["id"] == args.scenario_id)
        rejection = next(
            item for item in scenarios if item["id"] == args.rejection_scenario_id
        )
        frozen_request = copy.deepcopy(selected["spec"])

        viewer_write = client.post(
            "/api/v2/simulations", headers=viewer_headers, json=selected["spec"]
        )
        check("viewer_write_rejected", viewer_write.status_code == 403,
              "viewer may read and explain but may not create a run", {
                  "status_code": viewer_write.status_code
              })

        invalid = client.post("/api/v2/simulations", headers=editor_headers, json={})
        check("invalid_input_rejected", invalid.status_code == 422,
              "malformed simulation input is controlled by the API", {
                  "status_code": invalid.status_code, "detail": invalid.json().get("detail")
              })

        submitted = client.post("/api/v2/simulations", headers=editor_headers, json=frozen_request)
        check("editor_submit_accepted", submitted.status_code == 202,
              "editor receives an asynchronous run identifier", {
                  "status_code": submitted.status_code, "body": submitted.json()
              })
        if submitted.status_code != 202:
            raise RuntimeError(f"Valid submission failed: {submitted.status_code} {submitted.text}")
        submission = submitted.json()
        record_id = submission["id"]

        # Mutating the client-side object must not affect the persisted server snapshot.
        item_key = "jobs" if "jobs" in frozen_request else "requests"
        frozen_request[item_key][0]["id"] = "client-mutated-after-submit"
        completed = wait_for_terminal(client, record_id, viewer_headers)
        check("service_run_completed", completed["status"] == "completed",
              "the service completed its simulation evaluation", {
                  "status": completed["status"], "error": completed.get("error")
              })
        check("submission_hash_preserved",
              completed["input_hash"] == submission["input_hash"] and len(completed["input_hash"]) == 64,
              "the returned and persisted hashes identify the frozen input", {
                  "submitted_hash": submission["input_hash"], "stored_hash": completed["input_hash"]
              })
        check("stored_snapshot_not_mutated", completed["spec"] == selected["spec"],
              "changing the client request object after submission does not change stored input",
              {"stored_item_id": completed["spec"][item_key][0]["id"]})

        events = client.get(f"/api/v2/simulations/{record_id}/events", headers=viewer_headers)
        events.raise_for_status()
        events_body = events.json()
        check("completed_event_replay_available",
              events_body["mode"] == "completed_simulation_replay" and events_body["total"] > 0,
              "events are exposed as a completed-simulation replay", {
                  "mode": events_body["mode"], "total": events_body["total"],
                  "returned": len(events_body["events"])
              })

        before_explain = client.get(f"/api/v2/simulations/{record_id}", headers=viewer_headers)
        before_explain.raise_for_status()
        explanation = client.post(f"/api/v2/simulations/{record_id}/explain", headers=viewer_headers)
        explanation.raise_for_status()
        explain_body = explanation.json()
        after_explain = client.get(f"/api/v2/simulations/{record_id}", headers=viewer_headers)
        after_explain.raise_for_status()
        check("viewer_explain_is_read_only",
              explain_body["mode"] == "rules_based_read_only"
              and len(explain_body["observations"]) > 0
              and before_explain.json() == after_explain.json(),
              "rules explanation cites the run and leaves stored state unchanged", {
                  "mode": explain_body["mode"],
                  "observations": len(explain_body["observations"]),
                  "first_source": explain_body["observations"][0]["source"]
              })

        rejected_submission = client.post(
            "/api/v2/simulations", headers=editor_headers, json=rejection["spec"]
        )
        rejected_submission.raise_for_status()
        rejected_record = wait_for_terminal(
            client, rejected_submission.json()["id"], viewer_headers
        )
        rejected_result = rejected_record.get("result", {})
        business_items = rejected_result.get("jobs", rejected_result.get("requests", []))
        job_states = [item["state"] for item in business_items]
        check("business_rejection_is_completed_service_run",
              rejected_record["status"] == "completed"
              and any(state not in {"completed", "running", "waiting"} for state in job_states),
              "HTTP/service completion is distinct from business job success", {
                  "service_status": rejected_record["status"], "job_states": job_states
              })

        recent = client.get("/api/v2/simulations", params={"limit": 20}, headers=viewer_headers)
        recent.raise_for_status()
        recent_ids = {item["id"] for item in recent.json()["simulations"]}
        check("accepted_runs_are_listed", record_id in recent_ids,
              "persisted run is discoverable through the query endpoint", {
                  "recent_count": len(recent_ids), "record_id": record_id
              })

    passed = all(item["passed"] for item in checks)
    payload = {
        "schema_version": 1,
        "task": "C",
        "scope": "HTTP, permissions, immutable snapshot, event replay, read-only explain, persistence",
        "scenario_id": args.scenario_id,
        "rejection_scenario_id": args.rejection_scenario_id,
        "tested_at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "python": os.sys.version.split()[0],
        "accepted_run_id": record_id,
        "business_rejection_run_id": rejected_record["id"],
        "checks": checks,
        "passed": passed,
        "notes": [
            "Synthetic simulation parameters are not GPU measurements.",
            "Tokens are intentionally omitted from this evidence file.",
            "A service-completed run may still contain rejected, failed, or unschedulable business work.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output.resolve()),
        "accepted_run_id": record_id,
        "business_rejection_run_id": rejected_record["id"],
        "checks": len(checks),
        "passed": passed,
    }, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
