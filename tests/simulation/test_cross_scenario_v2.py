"""Independent scenario-level checks across both workload adapters."""
import copy
import json
import unittest

from fabric_sim import simulate
from fabric_sim.ddp import demo_ddp
from fabric_sim.tp import demo_tp


class CrossScenarioTests(unittest.TestCase):
    def test_input_snapshot_unchanged_and_replay_deterministic(self):
        for spec in (demo_ddp("compact"), demo_tp("tp2")):
            original = copy.deepcopy(spec)
            first = simulate(spec)
            second = simulate(spec)
            self.assertEqual(spec, original)
            self.assertEqual(first["input_hash"], second["input_hash"])
            self.assertEqual(first["events"], second["events"])
            self.assertEqual(first["summary"], second["summary"])
            changed = copy.deepcopy(spec)
            changed["topology"]["node_bandwidth_gbps"] /= 2
            self.assertNotEqual(first["input_hash"], simulate(changed)["input_hash"])

    def test_different_gpu_owners_still_compete_on_network(self):
        paired = demo_ddp("competition")
        combined = simulate(paired)
        both = {job["id"]: job for job in combined["jobs"]}
        self.assertFalse(set(paired["jobs"][0]["gpu_ids"]) & set(paired["jobs"][1]["gpu_ids"]))
        for job in paired["jobs"]:
            solo = copy.deepcopy(paired)
            solo["jobs"] = [copy.deepcopy(job)]
            isolated = simulate(solo)["jobs"][0]
            self.assertEqual(isolated["state"], "completed")
            self.assertGreater(both[job["id"]]["completion_ms"], isolated["completion_ms"])
            self.assertEqual(both[job["id"]]["completed_steps"], isolated["completed_steps"])

    def test_same_capacity_contract_in_both_scenarios(self):
        for spec in (demo_ddp("competition"), demo_tp("competition")):
            result = simulate(spec)
            self.assertTrue(result["resource_samples"])
            for sample in result["resource_samples"]:
                self.assertLessEqual(sample["allocated_gbps"], sample["capacity_gbps"] + 1e-7)
                self.assertGreaterEqual(sample["allocated_gbps"], 0)
            self.assertTrue(all(check["passed"] for check in result["invariant_checks"]))
            json.dumps(result, allow_nan=False)

    def test_tp_comparisons_keep_requested_generation_work(self):
        observed = []
        for variant in ("tp1", "tp2", "tp4"):
            spec = demo_tp(variant)
            result = simulate(spec)
            self.assertEqual(spec["profile"]["source"], "SYNTHETIC")
            self.assertTrue(all(r["state"] == "completed" for r in result["requests"]))
            observed.append([(r["prompt_tokens"], r["max_new_tokens"], r["generated_tokens"])
                             for r in result["requests"]])
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[1], observed[2])

    def test_domain_failure_is_not_a_failed_simulation(self):
        ddp = simulate(demo_ddp("memory_reject"))
        tp = simulate(demo_tp("kv_reject"))
        self.assertTrue(all(j["state"] == "unschedulable" for j in ddp["jobs"]))
        self.assertTrue(any(r["state"] == "rejected" for r in tp["requests"]))
        self.assertTrue(all(c["passed"] for c in ddp["invariant_checks"]))
        self.assertTrue(all(c["passed"] for c in tp["invariant_checks"]))


if __name__ == "__main__":
    unittest.main()
