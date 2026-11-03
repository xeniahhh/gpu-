from __future__ import annotations

import json
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from gateway import create_app


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "topologies" / "micro-2x2.json"


class S7GatewayApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app())
        self.content = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def create_revision(self) -> tuple[str, str]:
        project_response = self.client.post(
            "/api/projects",
            json={"name": "API test"},
        )
        self.assertEqual(project_response.status_code, 201)
        project = project_response.json()

        revision_response = self.client.post(
            f"/api/projects/{project['id']}/revisions",
            json={"content": self.content},
        )
        self.assertEqual(revision_response.status_code, 201)
        return project["id"], revision_response.json()["id"]

    def test_health_and_project_crud(self) -> None:
        self.assertEqual(
            self.client.get("/api/health").json()["status"],
            "ok",
        )
        project_id, revision_id = self.create_revision()

        self.assertEqual(
            self.client.get(f"/api/projects/{project_id}").status_code,
            200,
        )
        revisions = self.client.get(
            f"/api/projects/{project_id}/revisions"
        ).json()
        self.assertEqual(revisions[0]["id"], revision_id)
        self.assertEqual(revisions[0]["status"], "draft")

    def test_demo_bootstrap_creates_validated_revision(self) -> None:
        response = self.client.post("/api/demo/bootstrap")

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertTrue(payload["validation"]["valid"])
        self.assertEqual(payload["revision"]["status"], "validated")

    def test_validate_commit_and_run(self) -> None:
        _, revision_id = self.create_revision()
        validation = self.client.post(
            f"/api/revisions/{revision_id}/validate"
        )
        self.assertEqual(validation.status_code, 200)
        self.assertTrue(validation.json()["validation"]["valid"])

        committed = self.client.post(
            f"/api/revisions/{revision_id}/commit"
        )
        self.assertEqual(committed.status_code, 200)
        self.assertEqual(committed.json()["status"], "committed")

        run = self.client.post(
            "/api/runs",
            json={
                "revision_id": revision_id,
                "strategy": "rail_aware",
                "flow_unit_gbps": 10,
            },
        )
        self.assertEqual(run.status_code, 201)
        run_payload = run.json()
        self.assertEqual(run_payload["status"], "completed")
        self.assertIn("placement", run_payload["result"])
        self.assertIn("evaluation", run_payload["result"])

        fetched = self.client.get(f"/api/runs/{run_payload['id']}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["id"], run_payload["id"])

    def test_draft_cannot_run(self) -> None:
        _, revision_id = self.create_revision()

        response = self.client.post(
            "/api/runs",
            json={"revision_id": revision_id},
        )

        self.assertEqual(response.status_code, 409)

    def test_revision_diff_endpoint(self) -> None:
        project_id, first_id = self.create_revision()
        changed = json.loads(json.dumps(self.content))
        changed["jobs"][0]["priority"] = 20
        second = self.client.post(
            f"/api/projects/{project_id}/revisions",
            json={"content": changed},
        ).json()

        comparison = self.client.get(
            f"/api/revisions/{first_id}/diff/{second['id']}"
        )

        self.assertEqual(comparison.status_code, 200)
        self.assertEqual(comparison.json()["changed_count"], 1)

    def test_sse_events_are_available(self) -> None:
        _, revision_id = self.create_revision()
        self.client.post(f"/api/revisions/{revision_id}/validate")
        run = self.client.post(
            "/api/runs",
            json={"revision_id": revision_id},
        ).json()

        events = self.client.get(f"/api/runs/{run['id']}/events")

        self.assertEqual(events.status_code, 200)
        self.assertIn("event: completed", events.text)

    def test_fault_scenario_returns_replanning_result(self) -> None:
        fault_fixture = (
            ROOT / "tests" / "fixtures" / "topologies" / "fault-replan.json"
        )
        content = json.loads(fault_fixture.read_text(encoding="utf-8"))
        project = self.client.post(
            "/api/projects",
            json={"name": "Fault test"},
        ).json()
        revision = self.client.post(
            f"/api/projects/{project['id']}/revisions",
            json={"content": content},
        ).json()
        self.client.post(f"/api/revisions/{revision['id']}/validate")

        run = self.client.post(
            "/api/runs",
            json={
                "revision_id": revision["id"],
                "faults": [
                    {"type": "node_down", "target_id": "node-1"}
                ],
            },
        )

        self.assertEqual(run.status_code, 201)
        result = run.json()["result"]
        self.assertIn("fault_replan", result)
        self.assertTrue(result["fault_replan"]["migrations"])
        self.assertTrue(result["fault_replan"]["complete"])

    def test_failed_worker_is_reassigned_in_api_run(self) -> None:
        _, revision_id = self.create_revision()
        self.client.post(f"/api/revisions/{revision_id}/validate")

        run = self.client.post(
            "/api/runs",
            json={
                "revision_id": revision_id,
                "worker_count": 2,
                "failed_workers": ["worker-1"],
            },
        ).json()

        distributed = run["result"]["distributed_evaluation"]
        self.assertEqual(distributed["failed_workers"], ["worker-1"])
        self.assertEqual(distributed["reassigned"], {"worker-1": "worker-2"})


if __name__ == "__main__":
    unittest.main()
