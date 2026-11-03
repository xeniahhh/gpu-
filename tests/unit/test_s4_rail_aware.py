from __future__ import annotations

import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from fabric_domain import (
    FirstFitPlacementStrategy,
    Job,
    RailAwarePlacementStrategy,
    load_revision_file,
    rail_alignment_score,
    validate_revision,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "topologies" / "rail-aware-comparison.json"


class S4RailAwarePlacementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = load_revision_file(FIXTURE)
        cls.first_fit = FirstFitPlacementStrategy().place(
            cls.content.topology,
            cls.content.jobs,
        )
        cls.rail_aware = RailAwarePlacementStrategy().place(
            cls.content.topology,
            cls.content.jobs,
        )

    def test_fixture_is_valid(self) -> None:
        self.assertTrue(validate_revision(self.content).valid)

    def test_rail_aware_balances_the_multi_node_job(self) -> None:
        assignment = self.rail_aware.assignments[0]

        self.assertEqual(assignment.job_id, "job-multi-01")
        self.assertEqual(assignment.node_ids, ("node-0", "node-1"))
        self.assertEqual(
            [gpu.gpu_id for gpu in assignment.gpus],
            [
                "node-0-gpu-0",
                "node-0-gpu-1",
                "node-0-gpu-2",
                "node-1-gpu-0",
                "node-1-gpu-1",
                "node-1-gpu-2",
            ],
        )
        self.assertIsNotNone(assignment.quality)
        self.assertEqual(assignment.quality.rail_alignment, 1.0)

    def test_rail_aware_improves_alignment_over_first_fit(self) -> None:
        first_assignment = self.first_fit.assignments[0]
        rail_assignment = self.rail_aware.assignments[0]
        first_score = rail_alignment_score(first_assignment)
        rail_score = rail_alignment_score(rail_assignment)

        self.assertLess(first_score, rail_score)
        self.assertEqual(rail_score, 1.0)

    def test_both_strategies_place_all_gpus_without_overlap(self) -> None:
        for result in (self.first_fit, self.rail_aware):
            self.assertTrue(result.complete)
            self.assertEqual(result.assigned_gpu_count, 8)
            gpu_ids = [
                gpu.gpu_id
                for assignment in result.assignments
                for gpu in assignment.gpus
            ]
            self.assertEqual(len(gpu_ids), len(set(gpu_ids)))

    def test_single_node_job_stays_on_one_node(self) -> None:
        single_node_job = Job(
            id="job-single",
            requested_gpus=4,
            communication_mode=self.content.jobs[0].communication_mode,
            priority=10,
        )

        result = RailAwarePlacementStrategy().place(
            self.content.topology,
            (single_node_job,),
        )

        self.assertEqual(result.assignments[0].node_ids, ("node-0",))
        self.assertEqual(result.assignments[0].quality.score, 1.0)

    def test_oversized_job_is_not_partially_placed(self) -> None:
        oversized = replace(self.content.jobs[0], requested_gpus=9)

        result = RailAwarePlacementStrategy().place(
            self.content.topology,
            (oversized,),
        )

        self.assertEqual(result.assignments, ())
        self.assertEqual(result.unplaced_job_ids, (oversized.id,))
        self.assertEqual(result.findings[0].code, "INSUFFICIENT_GPUS")

    def test_repeated_rail_aware_placements_are_identical(self) -> None:
        repeated = RailAwarePlacementStrategy().place(
            self.content.topology,
            self.content.jobs,
        )

        self.assertEqual(repeated, self.rail_aware)

    def test_inspection_command_compares_strategies(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/inspect_s4.py"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            env={**os.environ, "PYTHONUTF8": "1"},
            timeout=15,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("first-fit baseline", completed.stdout)
        self.assertIn("rail-aware placement", completed.stdout)
        self.assertIn("Average rail-alignment delta: +", completed.stdout)


if __name__ == "__main__":
    unittest.main()
