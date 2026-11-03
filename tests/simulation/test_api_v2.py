from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient

from fabric_sim.ddp import demo_ddp
from fabric_sim.validation import canonical_hash
from gateway.api import create_app
from gateway.security import AccessPolicy
from gateway.simulation_api import SimulationBusy, SimulationService, explain_record

EDITOR = "course-editor-" + "e" * 28
VIEWER = "course-viewer-" + "v" * 28


def finish(service, simulation_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        record = service.get(simulation_id)
        if record["status"] not in {"queued", "running"}:
            return record
        time.sleep(.01)
    raise AssertionError("simulation did not terminate")


class SimulationApiTests(unittest.TestCase):
    def setUp(self):
        self.service = SimulationService(":memory:")
        self.client = TestClient(create_app(access_policy=AccessPolicy(VIEWER, EDITOR), simulation_service=self.service))
        self.editor = {"Authorization": "Bearer " + EDITOR}
        self.viewer = {"Authorization": "Bearer " + VIEWER}

    def tearDown(self):
        self.client.close()
        self.service.close()

    def test_submit_poll_evidence_and_read_only_explain(self):
        spec = demo_ddp("compact")
        response = self.client.post("/api/v2/simulations", json=spec, headers=self.editor)
        self.assertEqual(response.status_code, 202, response.text)
        item = response.json()
        record = finish(self.service, item["id"])
        self.assertEqual(record["status"], "completed", record.get("error"))
        self.assertEqual(record["input_hash"], canonical_hash(spec))
        result = self.client.get(f"/api/v2/simulations/{item['id']}", headers=self.viewer)
        self.assertEqual(result.status_code, 200)
        events = self.client.get(f"/api/v2/simulations/{item['id']}/events?limit=2", headers=self.viewer).json()
        self.assertLessEqual(len(events["events"]), 2)
        self.assertEqual(events["mode"], "completed_simulation_replay")
        explain = self.client.post(f"/api/v2/simulations/{item['id']}/explain", headers=self.viewer)
        self.assertEqual(explain.status_code, 200, explain.text)
        self.assertEqual(explain.json()["input_hash"], record["input_hash"])
        self.assertTrue(all("source" in o for o in explain.json()["observations"]))

    def test_viewer_cannot_mutate_and_unknown_token_rejected(self):
        spec = demo_ddp("compact")
        self.assertEqual(self.client.post("/api/v2/simulations", json=spec, headers=self.viewer).status_code, 403)
        self.assertEqual(self.client.get("/api/v2/scenarios").status_code, 401)
        self.assertEqual(self.client.get("/api/v2/simulations/missing", headers=self.viewer).status_code, 404)

    def test_invalid_input_and_demo_page(self):
        spec = demo_ddp("compact")
        spec["horizon_ms"] = -1
        self.assertEqual(self.client.post("/api/v2/simulations", json=spec, headers=self.editor).status_code, 422)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("工作负载与故障分析", page.text)
        scenarios = self.client.get("/api/v2/scenarios", headers=self.viewer)
        self.assertEqual(scenarios.status_code, 200, scenarios.text)
        self.assertGreaterEqual(len(scenarios.json()["scenarios"]), 7)

    def test_business_rejection_is_a_completed_evaluation(self):
        from fabric_sim.tp import demo_tp
        response = self.client.post("/api/v2/simulations", json=demo_tp("kv_reject"), headers=self.editor)
        self.assertEqual(response.status_code, 202)
        record = finish(self.service, response.json()["id"])
        self.assertEqual(record["status"], "completed", record.get("error"))
        self.assertGreater(record["result"]["summary"]["rejected"], 0)


class SimulationServiceTests(unittest.TestCase):
    def test_http_busy_and_status_queries_while_computing(self):
        release, entered = Event(), Event()

        def runner(spec):
            entered.set()
            release.wait(5)
            return {"summary": {}}

        service = SimulationService(":memory:", max_pending=0, runner=runner)
        client = TestClient(create_app(access_policy=AccessPolicy(VIEWER, EDITOR), simulation_service=service))
        headers = {"Authorization": "Bearer " + EDITOR}
        try:
            response = client.post("/api/v2/simulations", json=demo_ddp("compact"), headers=headers)
            self.assertTrue(entered.wait(2))
            started = time.monotonic()
            status = client.get("/api/v2/simulations/" + response.json()["id"], headers=headers)
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["status"], "running")
            self.assertLess(time.monotonic() - started, 1.0)
            busy = client.post("/api/v2/simulations", json=demo_ddp("compact"), headers=headers)
            self.assertEqual(busy.status_code, 429)
            self.assertIn("Retry-After", busy.headers)
        finally:
            release.set()
            client.close()
            service.close()

    def test_queue_is_bounded_and_input_is_frozen(self):
        release, entered = Event(), Event()
        seen = []

        def runner(spec):
            entered.set()
            release.wait(5)
            seen.append(spec)
            return {"summary": {}, "events": [], "assumptions": [], "invariant_checks": []}

        service = SimulationService(":memory:", max_pending=0, runner=runner)
        try:
            spec = demo_ddp("compact")
            original = json.loads(json.dumps(spec))
            item = service.submit(spec)
            self.assertTrue(entered.wait(2))
            spec["horizon_ms"] = 1
            with self.assertRaises(SimulationBusy):
                service.submit(original)
            self.assertEqual(service.get(item["id"])["spec"], original)
            release.set()
            self.assertEqual(finish(service, item["id"])["status"], "completed")
            self.assertEqual(seen[0], original)
        finally:
            release.set()
            service.close()

    def test_restart_preserves_completed_and_marks_unfinished_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.sqlite3"
            service = SimulationService(path, runner=lambda spec: {"summary": {"count": 1}})
            item = service.submit(demo_ddp("compact"))
            finish(service, item["id"])
            with service.lock, service.db:
                service.db.execute("INSERT INTO simulations_v2 (id,status,input_hash,spec_json,created_at) VALUES (?,?,?,?,?)",
                                   ("orphan", "running", "hash", "{}", "test"))
            service.close()
            service = SimulationService(path)
            try:
                self.assertEqual(service.get(item["id"])["result"]["summary"]["count"], 1)
                orphan = service.get("orphan")
                self.assertEqual(orphan["status"], "interrupted")
                self.assertIn("not implemented", orphan["error"])
            finally:
                service.close()

    def test_explain_never_invents_completed_results(self):
        record = {"id": "s", "input_hash": "h", "status": "running", "result": None}
        answer = explain_record(record)
        self.assertEqual(answer["observations"][0]["source"], "status")
        self.assertEqual(answer["status"], "running")


if __name__ == "__main__":
    unittest.main()
