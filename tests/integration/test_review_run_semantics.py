from __future__ import annotations

import json
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from gateway.api import create_app
from gateway.runtime import GatewayRuntime


ROOT = Path(__file__).resolve().parents[2]


class ReviewRunSemanticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = GatewayRuntime()
        self.client = TestClient(create_app(self.runtime))
        self.payload = json.loads(
            (ROOT / "tests/fixtures/topologies/rail-aware-comparison.json").read_text()
        )

    def revision(self, payload: dict) -> str:
        project = self.runtime.create_project("Run semantics regression")
        draft = self.runtime.create_draft(project.id, payload)
        checked = self.runtime.validate_revision(draft.id)
        self.assertTrue(checked.report.valid)
        return draft.id

    def test_unplaced_job_is_degraded_even_when_all_created_flows_are_routable(self) -> None:
        self.payload["jobs"][1]["requested_gpus"] = 6
        revision_id = self.revision(self.payload)
        response = self.client.post("/api/runs", json={"revision_id": revision_id})
        self.assertEqual(response.status_code, 201)
        run = response.json()
        self.assertEqual(run["status"], "degraded")
        self.assertFalse(run["result"]["placement"]["complete"])
        self.assertTrue(run["result"]["placement"]["unplaced_job_ids"])
        self.assertEqual(run["result"]["evaluation"]["unroutable_flow_count"], 0)

    def test_combined_failure_modes_are_explicitly_rejected(self) -> None:
        revision_id = self.revision(self.payload)
        response = self.client.post("/api/runs", json={
            "revision_id": revision_id,
            "faults": [{"type": "node_down", "target_id": "node-0"}],
            "failed_workers": ["worker-1"],
        })
        self.assertEqual(response.status_code, 409)
        self.assertIn("separately", response.json()["detail"])
        self.assertFalse(self.runtime.runs)


if __name__ == "__main__":
    unittest.main()
