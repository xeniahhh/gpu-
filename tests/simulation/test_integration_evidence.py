from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from gateway.api import create_app
from gateway.runtime import GatewayRuntime
from gateway.security import AccessPolicy


VIEWER = "viewer-evidence-test-000000000001"
EDITOR = "editor-evidence-test-000000000002"


class IntegrationEvidenceTest(unittest.TestCase):
    def setUp(self):
        runtime = GatewayRuntime()
        self.client = TestClient(create_app(runtime, AccessPolicy(VIEWER, EDITOR)))

    def test_integration_evidence_requires_authentication(self):
        self.assertEqual(self.client.get("/api/v2/evidence").status_code, 401)

    def test_integration_evidence_exposes_a_b_c_without_tokens(self):
        response = self.client.get(
            "/api/v2/evidence",
            headers={"Authorization": f"Bearer {VIEWER}"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(
            payload["implementation_hash"],
            "d844734439ed524fe81d843d0065a17510ebf161c5efc036f31cf4bb061cd4e3",
        )
        by_id = {item["id"]: item for item in payload["contributors"]}
        self.assertEqual(set(by_id), {"A", "B", "C"})
        self.assertEqual(by_id["A"]["status"], "COMPLETE")
        self.assertEqual(by_id["B"]["metrics"][0]["value"], "19/19 PASS")
        self.assertEqual(by_id["C"]["metrics"][0]["value"], "180/180 PASS")
        encoded = response.text.lower()
        self.assertNotIn("authorization", encoded)
        self.assertNotIn("bearer", encoded)


if __name__ == "__main__":
    unittest.main()
