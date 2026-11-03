"""Domain tests for TP inference, not GPU performance claims."""
from copy import deepcopy
import unittest

from fabric_sim.tp import demo_tp, simulate_tp


def small_spec(tp=1, output=3):
    s = demo_tp(f"tp{tp}")
    s["model"] = {"name":"Tiny-test-transformer", "parameter_count":128,
                  "parameter_source":"DERIVED", "layers":1,"hidden_size":8,
                  "query_heads":4,"kv_heads":4,"head_dim":2,"dtype_bytes":2,
                  "max_context_tokens":64}
    s["profile"]["workspace_gib"] = 0
    s["requests"] = [{"id":"r1", "arrival_ms":0,"prompt_tokens":4,"max_new_tokens":output}]
    return s


class TensorParallelTests(unittest.TestCase):
    def test_prefill_produces_first_token_without_extra_decode(self):
        result = simulate_tp(small_spec(output=1))
        req = result["requests"][0]
        self.assertEqual(req["generated_tokens"],1)
        self.assertAlmostEqual(req["ttft_ms"], 4*.3)
        self.assertAlmostEqual(req["latency_ms"], req["ttft_ms"])
        self.assertEqual(req["inter_token_ms"], [])
        self.assertEqual(result["summary"]["allreduce_calls"],0)

    def test_decode_and_kv_have_no_off_by_one(self):
        result = simulate_tp(small_spec(output=3))
        req = result["requests"][0]
        self.assertAlmostEqual(req["latency_ms"], 4*.3+2*12)
        self.assertEqual(len(req["inter_token_ms"]),2)
        kv = result["summary"]["kv_bytes_per_token_whole_model"]
        self.assertEqual(req["reserved_kv_bytes_per_gpu"], (4+3)*kv)
        self.assertEqual(req["kv_peak_bytes_per_gpu"], (4+3-1)*kv)
        self.assertEqual(result["replicas"][0]["current_kv_bytes_per_gpu"],0)

    def test_each_forward_has_two_collectives_per_layer(self):
        result = simulate_tp(small_spec(tp=2,output=3))
        self.assertEqual(result["summary"]["allreduce_calls"],2*1*3)
        self.assertGreater(result["engine_stats"]["total_sent_bytes"],0)
        self.assertTrue(all(c["passed"] for c in result["invariant_checks"]))

    def test_qwen_kv_formula(self):
        s=demo_tp("tp1"); s["requests"]=s["requests"][:1]
        s["requests"][0]["max_new_tokens"]=1
        result=simulate_tp(s)
        self.assertEqual(result["summary"]["kv_bytes_per_token_whole_model"],57344)

    def test_long_request_is_rejected_without_blocking_small_request(self):
        result = simulate_tp(demo_tp("kv_reject"))
        self.assertEqual(result["requests"][0]["state"], "rejected")
        self.assertEqual(result["requests"][0]["reason"], "kv_or_model_memory_exceeded")
        self.assertEqual(result["requests"][1]["state"], "completed")
        self.assertEqual(result["requests"][0]["generated_tokens"],0)

    def test_gpu_and_head_constraints_reject_invalid_deployment(self):
        s=small_spec(tp=2)
        s["replicas"][0]["gpu_ids"]=["n0:gpu0","n0:gpu0"]
        with self.assertRaises(ValueError): simulate_tp(s)
        s=small_spec(tp=2);s["replicas"][0]["tp_size"]=8
        with self.assertRaises(ValueError): simulate_tp(s)
        s=small_spec(tp=2);s["replicas"].append(deepcopy(s["replicas"][0]))
        s["replicas"][1]["id"]="other"
        with self.assertRaises(ValueError): simulate_tp(s)

    def test_replica_failure_discards_active_request_and_keeps_others(self):
        result = simulate_tp(demo_tp("fault"))
        self.assertEqual(result["summary"]["failed"],1)
        self.assertEqual(result["summary"]["completed"],3)
        failed=next(r for r in result["requests"] if r["state"]=="failed")
        self.assertIsNone(failed["completed_ms"])
        self.assertGreaterEqual(result["summary"]["stale_callbacks_ignored"],1)
        self.assertTrue(any(e["kind"]=="replica_reloaded" for e in result["events"]))

    def test_resource_failure_during_compute_aborts_future_collective(self):
        s=small_spec(tp=2)
        s["replicas"][0]["gpu_ids"]=["n0:gpu0","n2:gpu0"]
        s["faults"]=[{"at_ms":.1,"type":"resource_down","target_id":"rack:r0:tx"}]
        result=simulate_tp(s)
        self.assertEqual(result["requests"][0]["state"],"failed")
        self.assertEqual(result["requests"][0]["generated_tokens"],0)
        self.assertEqual(result["replicas"][0]["resident_memory_bytes_per_gpu"],0)

    def test_failure_after_first_token_does_not_commit_late_decode(self):
        spec = small_spec(output=3)
        spec["faults"] = [{"at_ms":1.5, "type":"node_down", "target_id":"n0"}]
        result = simulate_tp(spec)
        request = result["requests"][0]
        self.assertEqual(request["state"], "failed")
        self.assertEqual(request["generated_tokens"], 1)
        self.assertAlmostEqual(request["ttft_ms"], 1.2)
        self.assertEqual(request["inter_token_ms"], [])
        self.assertGreater(result["summary"]["stale_callbacks_ignored"], 0)
        self.assertEqual(result["replicas"][0]["reserved_kv_bytes_per_gpu"], 0)
        self.assertFalse(any(e["kind"] == "token_generated" and e["time_ms"] >= 1.5
                             for e in result["events"]))

    def test_failure_during_transfer_startup_cancels_pending_traffic(self):
        spec = small_spec(tp=2)
        spec["topology"]["local_latency_us"] = 1000
        # First half-layer computation ends at 0.4 ms; transfer begins at 1.4 ms.
        spec["faults"] = [{"at_ms":.5, "type":"node_down", "target_id":"n0"}]
        result = simulate_tp(spec)
        self.assertEqual(result["requests"][0]["state"], "failed")
        self.assertEqual(result["requests"][0]["generated_tokens"], 0)
        self.assertEqual(result["engine_stats"]["total_sent_bytes"], 0)
        self.assertEqual(result["engine_stats"]["pending_flows"], 0)
        self.assertGreater(result["engine_stats"]["cancelled_flows"], 0)

    def test_busy_replica_queues_then_releases_request_memory(self):
        s=small_spec()
        s["requests"].append({"id":"r2","arrival_ms":0,"prompt_tokens":4,"max_new_tokens":1})
        result=simulate_tp(s)
        self.assertAlmostEqual(result["requests"][1]["started_ms"],result["requests"][0]["completed_ms"])
        self.assertEqual(result["replicas"][0]["reserved_kv_bytes_per_gpu"],0)

    def test_horizon_is_not_success(self):
        s=small_spec();s["horizon_ms"]=.1
        result=simulate_tp(s)
        self.assertEqual(result["summary"]["completed"],0)
        self.assertEqual(result["requests"][0]["state"],"incomplete")
        self.assertEqual(result["requests"][0]["reason"],"simulation_horizon_reached")

    def test_oversized_model_context_is_business_rejection(self):
        from fabric_sim import simulate
        spec = demo_tp("tp1")
        spec["requests"][0]["prompt_tokens"] = 40000
        result = simulate(spec)
        request = result["requests"][0]
        self.assertEqual(request["state"], "rejected")
        self.assertEqual(request["reason"], "context_length_exceeded")
        self.assertEqual(result["requests"][1]["state"], "completed")

    def test_no_repair_is_not_reported_as_reaching_horizon(self):
        spec = small_spec()
        spec["requests"].append({"id":"r2", "arrival_ms":0,
                                 "prompt_tokens":4, "max_new_tokens":1})
        spec["faults"] = [{"at_ms":.1, "type":"node_down", "target_id":"n0"}]
        result = simulate_tp(spec)
        self.assertEqual(result["requests"][0]["state"], "failed")
        waiting = result["requests"][1]
        self.assertEqual(waiting["state"], "incomplete")
        self.assertEqual(waiting["reason"], "no_future_replica_recovery")
        self.assertLess(result["summary"]["simulated_end_ms"], spec["horizon_ms"])
        self.assertEqual(result["replicas"][0]["reserved_kv_bytes_per_gpu"], 0)

    def test_deterministic_and_input_immutable(self):
        s=small_spec(tp=2); before=deepcopy(s)
        one=simulate_tp(s);two=simulate_tp(s)
        self.assertEqual(one,two)
        self.assertEqual(s,before)

    def test_nan_and_unknown_fault_rejected(self):
        s=small_spec();s["profile"]["workspace_gib"]=float('nan')
        with self.assertRaises(ValueError):simulate_tp(s)
        s=small_spec();s["faults"]=[{"at_ms":1,"type":"resource_down","target_id":"missing"}]
        with self.assertRaises(ValueError):simulate_tp(s)


if __name__ == '__main__': unittest.main()
