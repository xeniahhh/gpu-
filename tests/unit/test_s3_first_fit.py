from __future__ import annotations

import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from fabric_domain import (
    CommunicationMode,
    FirstFitPlacementStrategy,
    Job,
    load_revision_file,
    validate_revision,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "topologies" / "micro-2x2.json"


class S3FirstFitPlacementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = load_revision_file(FIXTURE)
        cls.strategy = FirstFitPlacementStrategy()

    def test_micro_jobs_fill_nodes_in_deterministic_order(self) -> None:
        result = self.strategy.place(self.content.topology, self.content.jobs)

        self.assertTrue(result.complete)
        self.assertEqual(result.strategy, "first_fit")
        self.assertEqual(result.job_order, ("job-ring-01", "job-a2a-02"))
        self.assertEqual(result.assigned_gpu_count, 8)
        self.assertEqual(result.utilization, 1.0)

        ring, all_to_all = result.assignments
        self.assertEqual(ring.node_ids, ("node-0",))
        self.assertEqual(
            [gpu.gpu_id for gpu in ring.gpus],
            [f"node-0-gpu-{index}" for index in range(4)],
        )
        self.assertEqual(all_to_all.node_ids, ("node-1",))
        self.assertEqual(
            [gpu.gpu_id for gpu in all_to_all.gpus],
            [f"node-1-gpu-{index}" for index in range(4)],
        )

    def test_jobs_are_ordered_by_priority_then_id(self) -> None:
        low_priority = Job(
            id="job-z",
            requested_gpus=4,
            communication_mode=CommunicationMode.RING_ALL_REDUCE,
            priority=1,
        )
        high_priority = Job(
            id="job-a",
            requested_gpus=4,
            communication_mode=CommunicationMode.ALL_TO_ALL,
            priority=20,
        )

        result = self.strategy.place(
            self.content.topology,
            (low_priority, high_priority),
        )

        self.assertEqual(result.job_order, ("job-a", "job-z"))
        self.assertEqual(result.assignments[0].job_id, "job-a")
        self.assertEqual(result.assignments[0].node_ids, ("node-0",))

    def test_job_that_does_not_fit_is_entirely_unplaced(self) -> None:
        oversized = replace(self.content.jobs[0], requested_gpus=9)

        result = self.strategy.place(self.content.topology, (oversized,))

        self.assertFalse(result.complete)
        self.assertEqual(result.assignments, ())
        self.assertEqual(result.unplaced_job_ids, (oversized.id,))
        self.assertEqual(result.findings[0].code, "INSUFFICIENT_GPUS")

    def test_placement_does_not_overlap_gpu_ids(self) -> None:
        result = self.strategy.place(self.content.topology, self.content.jobs)
        gpu_ids = [
            gpu.gpu_id
            for assignment in result.assignments
            for gpu in assignment.gpus
        ]

        self.assertEqual(len(gpu_ids), len(set(gpu_ids)))

    def test_repeated_placements_are_identical(self) -> None:
        first = self.strategy.place(self.content.topology, self.content.jobs)
        second = self.strategy.place(self.content.topology, self.content.jobs)

        self.assertEqual(first, second)

    def test_fixture_is_valid_before_placement(self) -> None:
        self.assertTrue(validate_revision(self.content).valid)

    def test_inspection_command_prints_assignments(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/inspect_s3.py"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            env={**os.environ, "PYTHONUTF8": "1"},
            timeout=15,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("job-ring-01", completed.stdout)
        self.assertIn("Allocated GPUs : 8/8", completed.stdout)
        self.assertIn("Utilization    : 100.0%", completed.stdout)


if __name__ == "__main__":
    unittest.main()
