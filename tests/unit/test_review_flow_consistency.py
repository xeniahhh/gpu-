from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from aggregator import merge_worker_results
from evaluator import evaluate_shard
from fabric_domain import (
    EvaluationConfig,
    RailAwarePlacementStrategy,
    RevisionContent,
    evaluate_placement,
)
from planner import DistributedEvaluationCoordinator


ROOT = Path(__file__).resolve().parents[2]


class GlobalFlowConsistencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads(
            (ROOT / "tests/fixtures/topologies/rail-aware-comparison.json").read_text()
        )

    def assert_conservation(self, evaluation: dict) -> None:
        for link in evaluation["links"]:
            carried = sum(
                flow["allocated_gbps"]
                for flow in evaluation["flows"]
                if link["link_id"] in flow["path_link_ids"]
            )
            self.assertAlmostEqual(link["allocated_gbps"], carried, places=5)
            self.assertLessEqual(carried, link["capacity_gbps"] + 1e-5)
        for job in evaluation["jobs"]:
            flows = [flow for flow in evaluation["flows"] if flow["job_id"] == job["job_id"]]
            expected = min(flow["per_unit_gbps"] for flow in flows) if flows else None
            self.assertEqual(job["throughput_proxy_gbps"], expected)

    def test_remote_bottleneck_leaves_downstream_capacity_unused(self) -> None:
        content = RevisionContent.from_dict(self.payload)
        placement = RailAwarePlacementStrategy().place(content.topology, content.jobs)
        evaluation = evaluate_placement(
            content.topology, content.jobs, placement, EvaluationConfig(flow_unit_gbps=200.0)
        ).to_dict()
        self.assert_conservation(evaluation)
        node_link = next(link for link in evaluation["links"] if link["link_id"] == "leaf-0--node-0")
        self.assertAlmostEqual(node_link["allocated_gbps"], 333.333333, places=5)
        self.assertLess(node_link["allocated_gbps"], node_link["capacity_gbps"])

    def test_worker_count_and_failure_preserve_routes_and_rates(self) -> None:
        for demand in (10.0, 200.0):
            baseline = DistributedEvaluationCoordinator(1).evaluate(self.payload, "rail_aware", demand)
            for workers, failures in ((2, set()), (3, set()), (2, {"worker-1"})):
                with self.subTest(demand=demand, workers=workers, failures=failures):
                    actual = DistributedEvaluationCoordinator(workers).evaluate(
                        self.payload, "rail_aware", demand, failed_workers=failures
                    )
                    self.assertEqual(actual.evaluation, baseline.evaluation)
                    self.assert_conservation(actual.evaluation)

    def test_aggregation_reallocates_shared_bottleneck_for_all_flow_fields(self) -> None:
        # Independently produced routes both use spine-0. The aggregator must
        # correct every flow, even when receiving these legacy shard payloads.
        shards = [
            evaluate_shard({
                "worker_id": f"worker-{index}",
                "job_ids": [job["id"]],
                "revision_content": self.payload,
                "strategy": "rail_aware",
                "flow_unit_gbps": 200.0,
            })
            for index, job in enumerate(self.payload["jobs"], start=1)
        ]
        merged = merge_worker_results(shards)
        self.assert_conservation(merged)
        for flow in merged["flows"]:
            self.assertAlmostEqual(flow["per_unit_gbps"], 200 / 3, places=5)

    def test_duplicate_partition_is_rejected(self) -> None:
        shard = evaluate_shard({
            "worker_id": "worker-1", "job_ids": [self.payload["jobs"][0]["id"]],
            "revision_content": self.payload,
        })
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            merge_worker_results([shard, copy.deepcopy(shard)])

    def test_unknown_failed_worker_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown failed workers"):
            DistributedEvaluationCoordinator(2).evaluate(
                self.payload, "rail_aware", 10.0, failed_workers={"worker-3"}
            )


if __name__ == "__main__":
    unittest.main()
