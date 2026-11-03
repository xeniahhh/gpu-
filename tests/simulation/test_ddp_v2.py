"""Behavioral/physical regressions, not snapshots of implementation details."""
from copy import deepcopy
import unittest

from fabric_sim.ddp import demo_ddp, simulate_ddp, training_profile


class DDPScenarios(unittest.TestCase):
    def job(self, result, name="A"):
        return next(j for j in result["jobs"] if j["id"] == name)

    def test_profile_bytes_and_input_not_mutated(self):
        p = training_profile()
        self.assertEqual(p["parameter_count"], 124439808)
        self.assertEqual(p["gradient_bytes"], 497759232)
        self.assertEqual(p["base_training_storage_bytes"], 1991036928)
        s = demo_ddp("compact")
        before = deepcopy(s)
        r = simulate_ddp(s)
        self.assertEqual(s, before)
        self.assertTrue(all(c["passed"] for c in r["invariant_checks"]))

    def test_model_profile_cannot_claim_wrong_gradient_or_impossible_memory(self):
        with self.assertRaises(ValueError):
            training_profile({"gradient_bytes": 123})
        with self.assertRaises(ValueError):
            training_profile({"peak_memory_gib": 1})
        with self.assertRaises(ValueError):
            training_profile({"source": "MEASURED"})
        for change in ({"dtype": "float16"}, {"optimizer": "SGD"},
                       {"source": "MEASURED", "measurement_id": "m1"}):
            with self.assertRaises(ValueError):
                training_profile(change)

    def test_profile_provenance_does_not_label_default_recovery_as_measured(self):
        p = training_profile({"source": "MEASURED", "measurement_id": "m1", "compute_ms": 88,
                              "optimizer_ms": 3, "peak_memory_gib": 4})
        self.assertEqual(p["field_sources"]["compute_ms"], "MEASURED")
        self.assertEqual(p["field_sources"]["restore_ms"], "SYNTHETIC")

    def test_disjoint_gpus_compete_on_shared_network(self):
        s = demo_ddp("competition")
        concurrent = simulate_ddp(s)
        isolated = simulate_ddp(s | {"jobs": s["jobs"][:1]})
        self.assertGreater(self.job(concurrent)["completion_ms"], self.job(isolated)["completion_ms"])
        self.assertEqual(concurrent["engine_stats"]["max_capacity_ratio"], 1)
        self.assertEqual(concurrent["engine_stats"]["completed_transfer_bytes"],
                         2 * 30 * 2 * 3 * 497759232)

    def test_physical_placement_changes_time_not_logical_bytes(self):
        compact = simulate_ddp(demo_ddp("compact"))
        spread = simulate_ddp(demo_ddp("competition"))
        self.assertLess(self.job(compact)["completion_ms"], self.job(spread)["completion_ms"])
        self.assertEqual(compact["engine_stats"]["completed_transfer_bytes"],
                         spread["engine_stats"]["completed_transfer_bytes"])

    def test_gang_admission_waits_without_partial_gpu_ownership(self):
        result = simulate_ddp(demo_ddp("admission"))
        c = self.job(result, "C")
        self.assertGreater(c["waiting_ms"], 0)
        self.assertGreaterEqual(c["started_at_ms"], self.job(result, "B")["completed_at_ms"])
        self.assertTrue(all(len(a["gpu_ids"]) == 4 for a in c["allocation_history"]))
        self.assertEqual(c["state"], "completed")

    def test_insufficient_single_gpu_memory_is_permanent_not_queue(self):
        result = simulate_ddp(demo_ddp("memory_reject"))
        self.assertEqual(result["summary"]["unschedulable_jobs"], 2)
        self.assertEqual(result["summary"]["unfinished_jobs"], 0)
        self.assertFalse(any(j["allocation_history"] for j in result["jobs"]))

    def test_bad_fixed_placement_memory_cannot_wait_forever(self):
        spec = demo_ddp("compact")
        spec["topology"]["nodes"][0]["memory_gib"] = 3
        result = simulate_ddp(spec)
        self.assertEqual(self.job(result)["state"], "unschedulable")
        self.assertEqual(self.job(result, "B")["state"], "completed")

    def test_failure_rollback_keeps_world_size_and_unaffected_job(self):
        result = simulate_ddp(demo_ddp("fault_spare"))
        a, b = self.job(result), self.job(result, "B")
        self.assertEqual(a["restart_count"], 1)
        self.assertGreater(a["lost_steps"], 0)
        self.assertEqual(a["completed_steps"], a["target_steps"])
        self.assertEqual(a["physical_completed_steps"], a["target_steps"]+a["lost_steps"])
        self.assertEqual(a["recoveries"][0]["checkpoint_step"], 10)
        self.assertEqual(a["recovery_ms"], 400)
        self.assertGreater(a["ignored_old_events"], 0)
        self.assertEqual(b["restart_count"], 0)
        self.assertTrue(all(len(x["gpu_ids"]) == 4 for x in a["allocation_history"]))
        self.assertFalse(any(g.startswith("n0:") for g in a["allocation_history"][-1]["gpu_ids"]))

    def test_no_spare_waits_for_other_job_instead_of_shrinking(self):
        result = simulate_ddp(demo_ddp("fault_no_spare"))
        a, b = self.job(result), self.job(result, "B")
        self.assertGreater(a["waiting_ms"], 2000)
        self.assertGreaterEqual(a["allocation_history"][1]["time_ms"], b["completed_at_ms"])
        self.assertEqual(a["world_size"], 4)
        self.assertTrue(all(len(x["gpu_ids"]) == 4 for x in a["allocation_history"]))

    def test_failure_during_restore_does_not_erase_prior_downtime(self):
        spec = demo_ddp("fault_spare")
        spec["faults"].append({"at_ms": 3200, "type": "node_down", "target_id": "n1"})
        a = self.job(simulate_ddp(spec))
        self.assertEqual(a["state"], "completed")
        self.assertEqual(a["restart_count"], 2)
        self.assertEqual(a["recoveries"][0]["superseded_at_ms"], 3200)
        resumed = a["recoveries"][-1]["resume_ms"]
        self.assertAlmostEqual(a["recovery_ms"], resumed - 3000)
        self.assertAlmostEqual(a["recovery_ms"], a["waiting_ms"] + a["restoring_ms"])
        self.assertEqual(a["recovery_pending_ms"], 0)

    def test_censored_restore_reports_observed_pending_downtime(self):
        spec = demo_ddp("fault_spare")
        spec["horizon_ms"] = 3200
        a = self.job(simulate_ddp(spec))
        self.assertEqual(a["state"], "restoring")
        self.assertEqual(a["recovery_ms"], 0)
        self.assertEqual(a["recovery_pending_ms"], 200)
        self.assertIsNone(a["recoveries"][0]["resume_ms"])

    def test_failure_during_checkpoint_does_not_commit_partial_save(self):
        result = simulate_ddp(demo_ddp("checkpoint"))
        a = self.job(result)
        self.assertEqual(a["recoveries"][0]["from_step"], 10)
        self.assertEqual(a["recoveries"][0]["checkpoint_step"], 0)
        self.assertEqual(a["lost_steps"], 10)
        self.assertGreater(a["ignored_old_events"], 0)

    def test_link_failure_during_compute_also_invalidates_future_ring(self):
        spec = demo_ddp("competition")
        spec["faults"] = [
            {"at_ms": 50, "type": "resource_down", "target_id": "rack:r0:tx"},
            {"at_ms": 500, "type": "resource_up", "target_id": "rack:r0:tx"}]
        result = simulate_ddp(spec)
        self.assertEqual(result["summary"]["restarts"], 2)
        self.assertTrue(all(j["state"] == "completed" for j in result["jobs"]))
        self.assertTrue(any(e["kind"] == "checkpoint_rollback" and e["time_ms"] == 50
                            for e in result["events"]))

    def test_horizon_does_not_falsely_complete_or_partially_admit(self):
        spec = demo_ddp("fault_no_spare")
        spec["horizon_ms"] = 3500
        result = simulate_ddp(spec)
        a = self.job(result)
        self.assertEqual(a["state"], "waiting")
        self.assertIsNone(a["completion_ms"])
        self.assertEqual(a["censored_at_ms"], 3500)
        self.assertEqual(a["gpu_ids"], [])

    def test_replay_is_deterministic_and_counterfactual_is_controlled(self):
        spec = demo_ddp("fault_spare")
        a, b = simulate_ddp(spec), simulate_ddp(spec)
        self.assertEqual(a, b)

    def test_rank_compute_slowdown_increases_step_cost(self):
        normal = simulate_ddp(demo_ddp("compact"))
        slow = simulate_ddp(demo_ddp("slow_rank"))
        self.assertGreater(self.job(slow)["completion_ms"], self.job(normal)["completion_ms"])
        self.assertEqual(self.job(slow)["computing_ms"], 6000)

    def test_one_rank_has_no_collective_wire_traffic(self):
        spec = demo_ddp("compact")
        spec["jobs"] = [{"id": "A", "world_size": 1, "steps": 3, "checkpoint_interval": 0}]
        result = simulate_ddp(spec)
        self.assertEqual(result["engine_stats"]["total_sent_bytes"], 0)
        self.assertEqual(self.job(result)["completion_ms"], 315)

    def test_simultaneous_arrivals_use_priority_not_json_list_order(self):
        spec = demo_ddp("compact")
        spec["jobs"] = [
            {"id": "A", "world_size": 4, "steps": 1, "priority": 0},
            {"id": "B", "world_size": 8, "steps": 1, "priority": 10}]
        result = simulate_ddp(spec)
        self.assertEqual(self.job(result, "B")["started_at_ms"], 0)
        self.assertGreater(self.job(result, "A")["waiting_ms"], 0)


if __name__ == "__main__":
    unittest.main()
