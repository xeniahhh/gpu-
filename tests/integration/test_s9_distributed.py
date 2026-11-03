from __future__ import annotations

import json
import unittest
from pathlib import Path

from planner import DistributedEvaluationCoordinator, partition_job_ids


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "topologies" / "rail-aware-comparison.json"


class S9DistributedEvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.content = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.coordinator = DistributedEvaluationCoordinator(worker_count=2)

    def test_jobs_are_partitioned_deterministically(self) -> None:
        partitions = partition_job_ids(
            ["job-c", "job-a", "job-b"],
            ("worker-1", "worker-2"),
        )

        self.assertEqual(
            partitions,
            {
                "worker-1": ["job-a", "job-c"],
                "worker-2": ["job-b"],
            },
        )

    def test_worker_results_merge_to_complete_evaluation(self) -> None:
        result = self.coordinator.evaluate(
            self.content,
            strategy="rail_aware",
            flow_unit_gbps=10.0,
        )

        self.assertEqual(set(result.assignments), {"worker-1", "worker-2"})
        self.assertEqual(
            {job["job_id"] for job in result.evaluation["jobs"]},
            {"job-multi-01", "job-small-02"},
        )
        self.assertEqual(result.evaluation["unroutable_flow_count"], 0)
        self.assertEqual(result.reassigned, {})

    def test_failed_worker_work_is_reassigned(self) -> None:
        healthy = self.coordinator.evaluate(
            self.content,
            strategy="rail_aware",
            flow_unit_gbps=10.0,
        )
        failed = self.coordinator.evaluate(
            self.content,
            strategy="rail_aware",
            flow_unit_gbps=10.0,
            failed_workers={"worker-1"},
        )

        self.assertEqual(failed.failed_workers, ("worker-1",))
        self.assertEqual(failed.reassigned, {"worker-1": "worker-2"})
        self.assertEqual(
            {job["job_id"] for job in failed.evaluation["jobs"]},
            {job["job_id"] for job in healthy.evaluation["jobs"]},
        )
        self.assertIn(
            "worker_failed",
            {event["type"] for event in failed.events},
        )

    def test_all_workers_down_fails_cleanly(self) -> None:
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.coordinator.evaluate(
                self.content,
                strategy="rail_aware",
                flow_unit_gbps=10.0,
                failed_workers={"worker-1", "worker-2"},
            )


if __name__ == "__main__":
    unittest.main()
