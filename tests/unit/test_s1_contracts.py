from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from fabric_contracts import EventEnvelope
from fabric_domain import (
    CommunicationMode,
    ContractError,
    RevisionContent,
    load_revision_file,
    revision_to_json,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "topologies" / "micro-2x2.json"
SCHEMAS = ROOT / "packages" / "event-contracts" / "schemas"


class S1DomainContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = load_revision_file(FIXTURE)

    def test_micro_fixture_has_expected_shape(self) -> None:
        topology = self.content.topology

        self.assertEqual(self.content.schema_version, 1)
        self.assertEqual(self.content.revision_id, "fixture-micro-2x2-v1")
        self.assertEqual(topology.id, "micro-2x2")
        self.assertEqual(len(topology.spines), 2)
        self.assertEqual(len(topology.racks), 2)
        self.assertEqual(len(topology.leaves), 2)
        self.assertEqual(len(topology.nodes), 2)
        self.assertEqual(topology.gpu_count, 8)
        self.assertEqual(len(topology.links), 6)

    def test_gpu_rails_are_explicit(self) -> None:
        for node in self.content.topology.nodes:
            self.assertEqual([gpu.slot for gpu in node.gpus], [0, 1, 2, 3])
            self.assertEqual([gpu.rail_id for gpu in node.gpus], [0, 1, 2, 3])

    def test_jobs_use_supported_communication_modes(self) -> None:
        self.assertEqual(len(self.content.jobs), 2)
        self.assertEqual(
            self.content.jobs[0].communication_mode,
            CommunicationMode.RING_ALL_REDUCE,
        )
        self.assertEqual(
            self.content.jobs[1].communication_mode,
            CommunicationMode.ALL_TO_ALL,
        )
        self.assertEqual(
            sum(job.requested_gpus for job in self.content.jobs),
            self.content.topology.gpu_count,
        )

    def test_revision_json_round_trip_is_lossless(self) -> None:
        encoded = revision_to_json(self.content)
        decoded = RevisionContent.from_dict(json.loads(encoded))

        self.assertEqual(decoded, self.content)
        self.assertIn('"schema_version": 1', encoded)
        self.assertIn('"rail_id"', encoded)

    def test_unsupported_schema_version_is_rejected(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["schema_version"] = 2

        with self.assertRaisesRegex(ContractError, "schema_version"):
            RevisionContent.from_dict(payload)

    def test_event_envelope_round_trip(self) -> None:
        event = EventEnvelope(
            event_id="event-1",
            event_type="partition.result",
            schema_version=1,
            trace_id="trace-1",
            run_id="run-1",
            revision_id="fixture-micro-2x2-v1",
            sequence=1,
            occurred_at="2026-10-26T00:00:00+08:00",
            payload={"partition_id": "leaf-0"},
        )

        self.assertEqual(EventEnvelope.from_dict(event.to_dict()), event)

    def test_all_schema_files_are_valid_json(self) -> None:
        expected_files = (
            "event-envelope-v1.schema.json",
            "job-v1.schema.json",
            "revision-v1.schema.json",
            "topology-v1.schema.json",
        )
        schema_files = [SCHEMAS / name for name in expected_files]

        for schema_path in schema_files:
            self.assertTrue(schema_path.is_file(), f"Missing schema: {schema_path}")
            payload = json.loads(schema_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["$schema"],
                "https://json-schema.org/draft/2020-12/schema",
            )

    def test_inspection_command_prints_summary(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/inspect_s1.py"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            env={**os.environ, "PYTHONUTF8": "1"},
            timeout=15,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("GPUs   : 8", completed.stdout)
        self.assertIn("JSON round-trip: PASS", completed.stdout)


if __name__ == "__main__":
    unittest.main()
