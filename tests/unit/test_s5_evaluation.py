from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from fabric_domain import (
    CommunicationMode,
    EvaluationConfig,
    FirstFitPlacementStrategy,
    Job,
    PlacementResult,
    RailAwarePlacementStrategy,
    evaluate_placement,
    generate_topology,
    load_revision_file,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "topologies" / "rail-aware-comparison.json"


class S5EvaluationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = load_revision_file(FIXTURE)
        cls.placement = RailAwarePlacementStrategy().place(
            cls.content.topology,
            cls.content.jobs,
        )
        cls.evaluation = evaluate_placement(
            cls.content.topology,
            cls.content.jobs,
            cls.placement,
        )

    def test_cross_node_flows_are_routed_through_leaf_links(self) -> None:
        path_links = {
            link_id
            for flow in self.evaluation.flows
            for link_id in flow.path_link_ids
        }

        self.assertGreater(len(self.evaluation.flows), 0)
        self.assertTrue(any(link_id.startswith("leaf-") for link_id in path_links))
        self.assertTrue(any(link_id.startswith("spine-") for link_id in path_links))
        self.assertEqual(self.evaluation.unroutable_flow_count, 0)

    def test_ring_and_all_to_all_generate_expected_traffic_units(self) -> None:
        jobs = {job.job_id: job for job in self.evaluation.jobs}

        self.assertEqual(jobs["job-multi-01"].traffic_units, 2)
        self.assertEqual(jobs["job-small-02"].traffic_units, 1)
        self.assertEqual(jobs["job-multi-01"].flow_count, 1)
        self.assertEqual(jobs["job-small-02"].flow_count, 1)

    def test_uncontended_throughput_equals_flow_unit_demand(self) -> None:
        for job in self.evaluation.jobs:
            self.assertEqual(job.throughput_proxy_gbps, 10.0)
        self.assertEqual(self.evaluation.oversubscribed_link_ids, ())

    def test_high_demand_marks_bottlenecks(self) -> None:
        evaluation = evaluate_placement(
            self.content.topology,
            self.content.jobs,
            self.placement,
            EvaluationConfig(flow_unit_gbps=200.0),
        )
        jobs = {job.job_id: job for job in evaluation.jobs}

        self.assertTrue(evaluation.oversubscribed_link_ids)
        self.assertLess(jobs["job-multi-01"].throughput_proxy_gbps, 200.0)
        self.assertTrue(jobs["job-multi-01"].bottleneck_link_ids)

    def test_micro_leaf_oversubscription_and_bisection_proxy(self) -> None:
        leaf_ratios = {
            leaf.leaf_id: leaf.ratio
            for leaf in self.evaluation.leaf_oversubscription
        }

        self.assertEqual(leaf_ratios, {"leaf-0": 1.0, "leaf-1": 1.0})
        self.assertEqual(
            self.evaluation.balanced_leaf_bisection_proxy_gbps,
            400.0,
        )

    def test_generated_topology_has_eight_to_one_oversubscription(self) -> None:
        topology = generate_topology()
        empty_placement = PlacementResult(
            strategy="none",
            topology_id=topology.id,
            job_order=(),
            total_gpus=topology.gpu_count,
            assignments=(),
            unplaced_job_ids=(),
            findings=(),
        )

        evaluation = evaluate_placement(topology, (), empty_placement)

        self.assertEqual(len(evaluation.leaf_oversubscription), 16)
        self.assertTrue(
            all(
                leaf.ratio == 8.0
                for leaf in evaluation.leaf_oversubscription
            )
        )
        self.assertEqual(
            evaluation.balanced_leaf_bisection_proxy_gbps,
            25600.0,
        )

    def test_single_node_job_has_no_fabric_throughput_proxy(self) -> None:
        job = Job(
            id="single-node",
            requested_gpus=2,
            communication_mode=CommunicationMode.RING_ALL_REDUCE,
            priority=1,
        )
        placement = FirstFitPlacementStrategy().place(
            self.content.topology,
            (job,),
        )
        evaluation = evaluate_placement(
            self.content.topology,
            (job,),
            placement,
        )

        self.assertEqual(evaluation.flows, ())
        self.assertIsNone(evaluation.jobs[0].throughput_proxy_gbps)

    def test_evaluation_is_deterministic(self) -> None:
        repeated = evaluate_placement(
            self.content.topology,
            self.content.jobs,
            self.placement,
        )

        self.assertEqual(repeated, self.evaluation)

    def test_inspection_command_prints_paths_and_metrics(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/inspect_s5.py"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
            env={**os.environ, "PYTHONUTF8": "1"},
            timeout=15,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("FabricSandbox S5 path and bandwidth evaluation", completed.stdout)
        self.assertIn("throughput=", completed.stdout)
        self.assertIn("Leaf oversubscription", completed.stdout)


if __name__ == "__main__":
    unittest.main()
