from __future__ import annotations

import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from fabric_domain import (
    TopologyGenerationConfig,
    TopologyGenerationError,
    generate_topology,
    load_revision_file,
    validate_jobs,
    validate_revision,
    validate_topology,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "topologies" / "micro-2x2.json"


class S2TopologyGenerationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.generated = generate_topology()

    def test_default_generator_matches_mvp_scale(self) -> None:
        topology = self.generated

        self.assertEqual(len(topology.spines), 8)
        self.assertEqual(len(topology.leaves), 16)
        self.assertEqual(len(topology.racks), 16)
        self.assertEqual(len(topology.nodes), 128)
        self.assertEqual(topology.gpu_count, 1024)
        self.assertEqual(len(topology.links), 256)

    def test_generated_rails_are_deterministic(self) -> None:
        node = self.generated.nodes[0]

        self.assertEqual(node.id, "node-00-00")
        self.assertEqual([gpu.slot for gpu in node.gpus], list(range(8)))
        self.assertEqual([gpu.rail_id for gpu in node.gpus], list(range(8)))

    def test_generated_topology_is_valid(self) -> None:
        report = validate_topology(
            self.generated,
            expected_gpus_per_node=8,
        )

        self.assertTrue(report.valid, report.to_dict())
        self.assertEqual(report.findings, ())

    def test_invalid_generator_parameters_are_rejected(self) -> None:
        with self.assertRaisesRegex(TopologyGenerationError, "spine_count"):
            generate_topology(TopologyGenerationConfig(spine_count=0))


class S2TopologyValidatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = load_revision_file(FIXTURE)
        cls.topology = cls.content.topology

    def test_micro_fixture_and_jobs_are_valid(self) -> None:
        report = validate_revision(self.content)

        self.assertTrue(report.valid, report.to_dict())
        self.assertEqual(report.findings, ())

    def test_duplicate_entity_id_is_reported(self) -> None:
        duplicate_leaf = self.topology.leaves[0]
        topology = replace(
            self.topology,
            leaves=(*self.topology.leaves, duplicate_leaf),
        )

        report = validate_topology(topology)

        self.assertIn("DUPLICATE_ENTITY_ID", report.codes())

    def test_missing_reference_is_reported(self) -> None:
        bad_node = replace(self.topology.nodes[0], leaf_id="missing-leaf")
        topology = replace(
            self.topology,
            nodes=(bad_node, *self.topology.nodes[1:]),
        )

        report = validate_topology(topology)

        self.assertIn("MISSING_NODE_LEAF", report.codes())

    def test_node_rack_leaf_mismatch_is_reported(self) -> None:
        bad_node = replace(self.topology.nodes[0], rack_id="rack-1")
        topology = replace(
            self.topology,
            nodes=(bad_node, *self.topology.nodes[1:]),
        )

        report = validate_topology(topology)

        self.assertIn("NODE_RACK_LEAF_MISMATCH", report.codes())

    def test_link_kind_mismatch_is_reported(self) -> None:
        bad_link = replace(
            self.topology.links[0],
            endpoint_b="node-0",
        )
        topology = replace(
            self.topology,
            links=(bad_link, *self.topology.links[1:]),
        )

        report = validate_topology(topology)

        self.assertIn("LINK_KIND_MISMATCH", report.codes())

    def test_duplicate_rail_is_reported(self) -> None:
        node = self.topology.nodes[0]
        bad_gpus = (
            node.gpus[0],
            replace(node.gpus[1], rail_id=node.gpus[0].rail_id),
            *node.gpus[2:],
        )
        bad_node = replace(node, gpus=bad_gpus)
        topology = replace(
            self.topology,
            nodes=(bad_node, *self.topology.nodes[1:]),
        )

        report = validate_topology(topology)

        self.assertIn("DUPLICATE_RAIL_ID", report.codes())

    def test_disconnected_topology_is_reported(self) -> None:
        connected_links = tuple(
            link
            for link in self.topology.links
            if link.endpoint_a not in {"leaf-1", "node-1"}
            and link.endpoint_b not in {"leaf-1", "node-1"}
        )
        topology = replace(self.topology, links=connected_links)

        report = validate_topology(topology)

        self.assertIn("TOPOLOGY_DISCONNECTED", report.codes())

    def test_job_larger_than_topology_is_reported(self) -> None:
        oversized = replace(self.content.jobs[0], requested_gpus=9)

        report = validate_jobs((oversized,), self.topology)

        self.assertIn("JOB_EXCEEDS_TOPOLOGY", report.codes())

    def test_inspection_command_prints_mvp_result(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/inspect_s2.py"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            env={**os.environ, "PYTHONUTF8": "1"},
            timeout=15,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("GPUs   : 1024", completed.stdout)
        self.assertIn("Validation: PASS", completed.stdout)


if __name__ == "__main__":
    unittest.main()
