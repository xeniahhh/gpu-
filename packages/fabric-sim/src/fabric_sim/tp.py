"""Tensor-parallel inference planning with explicit KV and failure semantics.

This is a single-request-per-replica, non-overlapped Megatron-style model,
not a vLLM emulator. Compute timings in demos are synthetic inputs.
"""
from __future__ import annotations

from copy import deepcopy
from math import ceil, isfinite
from typing import Any

from .core import Engine, Network, ring_allreduce

GIB = 1024 ** 3


def _number(value: Any, name: str, minimum: float = 0, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    value = float(value)
    if not isfinite(value) or value < minimum or (positive and value <= 0):
        raise ValueError(f"{name} is out of range")
    return value


def _integer(value: Any, name: str, minimum: int = 1, maximum: int = 10**12) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def qwen_model() -> dict:
    return {"name": "Qwen2.5-7B-Instruct", "parameter_count": 7_610_000_000,
            "parameter_source": "MODEL_CARD_ROUNDED", "layers": 28,
            "hidden_size": 3584, "query_heads": 28, "kv_heads": 4,
            "head_dim": 128, "dtype_bytes": 2, "max_context_tokens": 32768}


def demo_tp(variant: str = "baseline") -> dict:
    if variant not in {"baseline", "tp1", "tp2", "tp4", "competition", "kv_reject", "fault"}:
        raise ValueError(f"Unknown TP demo: {variant}")
    spec = {
        "schema_version": 2, "scenario": "tp", "horizon_ms": 5000,
        "topology": {"nodes": [{"id": f"n{i}", "rack_id": f"r{i//2}",
                                   "gpu_count": 2, "memory_gib": 24} for i in range(4)],
                     "node_bandwidth_gbps": 100, "uplink_bandwidth_gbps": 50,
                     "local_bandwidth_gbps": 200, "link_latency_us": 5,
                     "local_latency_us": 2},
        "model": qwen_model(),
        "profile": {"source": "SYNTHETIC", "workspace_gib": 2,
                    "prefill_ms_per_token_by_tp": {"1": .30, "2": .20, "4": .16},
                    "decode_ms_per_token_by_tp": {"1": 12., "2": 8., "4": 7.},
                    "model_reload_ms": 40.},
        "replicas": [{"id": "replica-0", "tp_size": 2,
                      "gpu_ids": ["n0:gpu0", "n0:gpu1"]}],
        "requests": [{"id": f"request-{i+1}", "arrival_ms": i*2,
                      "prompt_tokens": 64, "max_new_tokens": 4} for i in range(2)],
        "faults": [],
    }
    if variant == "tp1":
        spec["replicas"][0].update(tp_size=1, gpu_ids=["n0:gpu0"])
    if variant == "tp4":
        spec["replicas"][0].update(tp_size=4,
                                     gpu_ids=["n0:gpu0", "n0:gpu1", "n1:gpu0", "n1:gpu1"])
    if variant in {"competition", "fault"}:
        spec["replicas"] = [{"id": "replica-0", "tp_size": 2,
                             "gpu_ids": ["n0:gpu0", "n2:gpu0"]},
                            {"id": "replica-1", "tp_size": 2,
                             "gpu_ids": ["n1:gpu0", "n3:gpu0"]}]
        spec["requests"] = [{"id": f"request-{i+1}", "arrival_ms": 0,
                             "prompt_tokens": 128, "max_new_tokens": 4} for i in range(4)]
    if variant == "kv_reject":
        spec["replicas"][0].update(tp_size=1, gpu_ids=["n0:gpu0"])
        spec["topology"]["nodes"][0]["memory_gib"] = 16.5
        spec["requests"][0].update(prompt_tokens=16000, max_new_tokens=8)
    if variant == "fault":
        spec["faults"] = [{"at_ms": 5, "type": "node_down", "target_id": "n0"},
                          {"at_ms": 80, "type": "node_up", "target_id": "n0"}]
    return spec


def simulate_tp(spec: dict) -> dict:
    """Run a deterministic inference workload; return JSON-safe observations."""
    spec = deepcopy(spec)
    network = Network(spec["topology"])
    engine = Engine(network)
    model = spec.get("model", qwen_model())
    for key in ("parameter_count", "layers", "hidden_size", "query_heads", "kv_heads",
                "head_dim", "dtype_bytes", "max_context_tokens"):
        model[key] = _integer(model.get(key), f"model.{key}")
    if model["hidden_size"] != model["query_heads"] * model["head_dim"]:
        raise ValueError("hidden_size must equal query_heads * head_dim")
    if model["query_heads"] % model["kv_heads"]:
        raise ValueError("query_heads must be divisible by kv_heads")
    profile = spec.get("profile", {})
    source = profile.get("source", "SYNTHETIC")
    if source not in {"SYNTHETIC", "MEASURED", "DERIVED"}:
        raise ValueError("profile.source must identify SYNTHETIC, MEASURED or DERIVED")
    workspace = _number(profile.get("workspace_gib", 2), "workspace_gib") * GIB
    reload_ms = _number(profile.get("model_reload_ms", 40), "model_reload_ms")
    horizon = _number(spec.get("horizon_ms", 5000), "horizon_ms", positive=True)
    if horizon > 60000:
        raise ValueError("horizon_ms must be at most 60000")
    compute_tables = {}
    for key, defaults in (("prefill_ms_per_token_by_tp", {"1": .3, "2": .2, "4": .16}),
                          ("decode_ms_per_token_by_tp", {"1": 12., "2": 8., "4": 7.})):
        values = profile.get(key, defaults)
        compute_tables[key] = {str(k): _number(v, f"{key}.{k}") for k,v in values.items()}
    kv_per_token = 2 * model["layers"] * model["kv_heads"] * model["head_dim"] * model["dtype_bytes"]
    replicas = []
    gpu_owners = {}
    replica_ids = set()
    replica_specs = spec.get("replicas", [])
    if not isinstance(replica_specs, list) or not 1 <= len(replica_specs) <= 16:
        raise ValueError("replicas must contain 1 to 16 deployment entries")
    for raw in replica_specs:
        ident = raw.get("id")
        if not isinstance(ident, str) or not ident or ident in replica_ids:
            raise ValueError("replica IDs must be nonempty and unique")
        replica_ids.add(ident)
        tp = _integer(raw.get("tp_size"), "tp_size", maximum=4)
        if tp not in {1,2,4} or model["query_heads"] % tp or model["kv_heads"] % tp:
            raise ValueError("TP size must be 1/2/4 and divide query/KV head counts")
        if any(str(tp) not in values for values in compute_tables.values()):
            raise ValueError(f"Compute profile missing TP={tp}")
        ids = raw.get("gpu_ids", [])
        if not isinstance(ids, list) or len(ids) != tp or len(set(ids)) != tp:
            raise ValueError("Each replica needs exactly tp_size distinct GPU IDs")
        for gid in ids:
            if gid not in network.gpus:
                raise ValueError(f"Unknown GPU {gid}")
            if gid in gpu_owners:
                raise ValueError(f"GPU {gid} is assigned to more than one replica")
            gpu_owners[gid] = ident
        minimum_capacity = min(network.gpus[gid].memory_gib * GIB for gid in ids)
        weight = ceil(model["parameter_count"] * model["dtype_bytes"] / tp)
        base = weight + workspace
        replica = {"id": ident, "tp_size": tp, "gpu_ids": list(ids), "epoch": 0,
                   "state": "ready" if base <= minimum_capacity else "infeasible",
                   "reason": "" if base <= minimum_capacity else "weights_and_workspace_exceed_memory",
                   "weight_bytes_per_gpu": weight, "workspace_bytes_per_gpu": int(workspace),
                   "base_memory_bytes_per_gpu": base, "capacity_bytes_per_gpu": minimum_capacity,
                   "reserved_kv_bytes_per_gpu": 0, "current_kv_bytes_per_gpu": 0,
                   "active_request": None, "max_observed_memory_bytes_per_gpu": base if base <= minimum_capacity else 0}
        replicas.append(replica)
        engine.record("replica_deployed", subject_id=ident, state=replica["state"],
                      gpu_ids=ids, reason=replica["reason"], tp_size=tp)
    request_specs = spec.get("requests", [])
    if not isinstance(request_specs, list) or not 1 <= len(request_specs) <= 64:
        raise ValueError("requests must contain 1 to 64 entries")
    requests = []
    seen = set()
    for raw in request_specs:
        ident = raw.get("id")
        if not isinstance(ident, str) or not ident or ident in seen:
            raise ValueError("request IDs must be nonempty and unique")
        seen.add(ident)
        requests.append({"id": ident, "arrival_ms": _number(raw.get("arrival_ms",0), "arrival_ms"),
                         # Match the public input contract. Whether the requested
                         # length fits this model is a business admission result.
                         "prompt_tokens": _integer(raw.get("prompt_tokens"), "prompt_tokens", maximum=262144),
                         "max_new_tokens": _integer(raw.get("max_new_tokens"), "max_new_tokens", maximum=256),
                         "state": "scheduled", "reason": "", "replica_id": None,
                         "started_ms": None, "completed_ms": None, "first_token_ms": None,
                         "generated_tokens": 0, "token_times_ms": [], "wait_ms": None,
                         "ttft_ms": None, "latency_ms": None, "inter_token_ms": [],
                         "kv_peak_bytes_per_gpu": 0, "reserved_kv_bytes_per_gpu": 0,
                         "admission_memory_bytes_per_gpu": 0, "communication_calls": 0})
    request_by_id = {r["id"]:r for r in requests}
    observed = {"memory_checks": 0, "memory_violations": 0, "token_checks": 0,
                "token_violations": 0, "stale_callbacks_ignored": 0,
                "failed_requests": 0, "admitted": 0, "allreduce_calls": 0,
                "kv_growth_checks": 0, "kv_growth_violations": 0}

    def healthy(replica: dict) -> bool:
        ids = replica["gpu_ids"]
        if any(network.gpus[g].node_id in network.down_nodes for g in ids):
            return False
        for i in range(len(ids)):
            for resource in network.path(ids[i], ids[(i+1)%len(ids)]):
                if network.capacities[resource] <= 0:
                    return False
        return True

    def fits(req: dict, replica: dict) -> bool:
        tokens = req["prompt_tokens"] + req["max_new_tokens"]
        return (tokens <= model["max_context_tokens"] and
                replica["base_memory_bytes_per_gpu"] + tokens*kv_per_token/replica["tp_size"]
                <= replica["capacity_bytes_per_gpu"])

    def check_memory(replica: dict) -> None:
        observed["memory_checks"] += 1
        memory = replica["base_memory_bytes_per_gpu"] + replica["reserved_kv_bytes_per_gpu"]
        replica["max_observed_memory_bytes_per_gpu"] = max(replica["max_observed_memory_bytes_per_gpu"], memory)
        if memory > replica["capacity_bytes_per_gpu"] + 1e-6:
            observed["memory_violations"] += 1
            raise AssertionError("TP admission exceeded GPU memory")
        if replica["current_kv_bytes_per_gpu"] > replica["reserved_kv_bytes_per_gpu"]:
            raise AssertionError("Current KV exceeds reservation")

    def set_state(req: dict, state: str, message: str, **extra) -> None:
        old = req["state"]
        req["state"] = state
        engine.record("request_state", subject_id=req["id"], state=state,
                      before=old, message=message, replica_id=req["replica_id"], **extra)

    def valid(replica: dict, req: dict, epoch: int) -> bool:
        ok = (replica["epoch"] == epoch and replica["active_request"] == req["id"]
              and req["state"] in {"prefill", "decoding"})
        if not ok:
            observed["stale_callbacks_ignored"] += 1
            engine.record("stale_event_ignored", subject_id=req["id"], epoch=epoch,
                          current_epoch=replica["epoch"], message="过期计算事件不推进请求")
        return ok

    def release(replica: dict) -> None:
        replica["reserved_kv_bytes_per_gpu"] = 0
        replica["current_kv_bytes_per_gpu"] = 0
        replica["active_request"] = None

    def token_finished(replica: dict, req: dict, epoch: int, phase: str) -> None:
        if not valid(replica, req, epoch): return
        previous = replica["current_kv_bytes_per_gpu"]
        # A generated token is cached on its NEXT forward pass, not when sampled.
        kv_tokens = req["prompt_tokens"] + req["generated_tokens"]
        current = kv_tokens * kv_per_token // replica["tp_size"]
        replica["current_kv_bytes_per_gpu"] = current
        req["kv_peak_bytes_per_gpu"] = max(req["kv_peak_bytes_per_gpu"], current)
        observed["kv_growth_checks"] += 1
        if current < previous: observed["kv_growth_violations"] += 1
        check_memory(replica)
        req["generated_tokens"] += 1
        req["token_times_ms"].append(engine.now_ms)
        if req["first_token_ms"] is None:
            req["first_token_ms"] = engine.now_ms
            req["ttft_ms"] = engine.now_ms - req["arrival_ms"]
        elif len(req["token_times_ms"]) > 1:
            req["inter_token_ms"].append(req["token_times_ms"][-1] - req["token_times_ms"][-2])
        observed["token_checks"] += 1
        if req["generated_tokens"] > req["max_new_tokens"]:
            observed["token_violations"] += 1
            raise AssertionError("Generated too many tokens")
        engine.record("token_generated", subject_id=req["id"], replica_id=replica["id"],
                      token_index=req["generated_tokens"], phase=phase, epoch=epoch,
                      kv_tokens=kv_tokens, current_kv_bytes_per_gpu=current,
                      reserved_kv_bytes_per_gpu=replica["reserved_kv_bytes_per_gpu"])
        if req["generated_tokens"] == req["max_new_tokens"]:
            req["completed_ms"] = engine.now_ms
            req["latency_ms"] = engine.now_ms - req["arrival_ms"]
            set_state(req, "completed", "全部输出 token 已生成，释放请求 KV 预留")
            release(replica)
            replica["state"] = "ready"
            engine.schedule(engine.now_ms, dispatch)
        else:
            if req["state"] != "decoding":
                set_state(req, "decoding", "首 token 来自 prefill；后续每次前向生成一个 token")
            forward(replica, req, epoch, "decode", 0)

    def forward(replica: dict, req: dict, epoch: int, phase: str, half_layer: int) -> None:
        if not valid(replica, req, epoch): return
        if half_layer == 2*model["layers"]:
            token_finished(replica, req, epoch, phase)
            return
        tp = replica["tp_size"]
        tokens = req["prompt_tokens"] if phase == "prefill" else 1
        table = compute_tables["prefill_ms_per_token_by_tp" if phase == "prefill" else "decode_ms_per_token_by_tp"]
        compute_ms = table[str(tp)] * tokens / (2*model["layers"])
        def computed():
            if not valid(replica, req, epoch): return
            if not healthy(replica):
                abort(replica, "replica_path_unavailable")
                return
            payload = tokens * model["hidden_size"] * model["dtype_bytes"]
            if tp > 1:
                req["communication_calls"] += 1
                observed["allreduce_calls"] += 1
            def reduced():
                if valid(replica, req, epoch): forward(replica, req, epoch, phase, half_layer+1)
            ring_allreduce(engine, replica["id"], replica["gpu_ids"], payload, reduced,
                           meta={"subject_id": req["id"], "epoch": epoch, "phase": phase,
                                 "layer": half_layer//2, "block": "attention" if half_layer%2==0 else "mlp"})
        engine.after(compute_ms, computed)

    def dispatch() -> None:
        queued = sorted((r for r in requests if r["state"] == "queued"), key=lambda r:(r["arrival_ms"],r["id"]))
        for req in queued:
            candidates = [r for r in replicas if r["state"] != "infeasible" and fits(req,r)]
            if not candidates:
                req["reason"] = ("context_length_exceeded" if req["prompt_tokens"]+req["max_new_tokens"] > model["max_context_tokens"]
                                 else "kv_or_model_memory_exceeded")
                set_state(req, "rejected", "请求的上下文或最坏 KV 预留超过部署能力", reason=req["reason"])
                continue
            ready = next((r for r in candidates if r["state"] == "ready" and healthy(r)), None)
            if ready is None:
                req["reason"] = "waiting_for_healthy_free_replica"
                continue
            replica = ready
            req["reason"] = ""
            req["replica_id"] = replica["id"]
            req["started_ms"] = engine.now_ms
            req["wait_ms"] = engine.now_ms - req["arrival_ms"]
            req["reserved_kv_bytes_per_gpu"] = ((req["prompt_tokens"]+req["max_new_tokens"])*kv_per_token//replica["tp_size"])
            req["admission_memory_bytes_per_gpu"] = replica["base_memory_bytes_per_gpu"] + req["reserved_kv_bytes_per_gpu"]
            replica["reserved_kv_bytes_per_gpu"] = req["reserved_kv_bytes_per_gpu"]
            replica["active_request"] = req["id"]
            replica["state"] = "busy"
            replica["epoch"] += 1
            observed["admitted"] += 1
            check_memory(replica)
            set_state(req, "prefill", "副本预留完整请求的最坏 KV 空间后开始处理输入",
                      epoch=replica["epoch"], reserved_kv_bytes_per_gpu=req["reserved_kv_bytes_per_gpu"])
            forward(replica, req, replica["epoch"], "prefill", 0)

    def abort(replica: dict, reason: str) -> None:
        engine.cancel_owner(replica["id"])
        replica["epoch"] += 1
        if replica["active_request"] is not None:
            req = request_by_id[replica["active_request"]]
            req["reason"] = reason
            req["failed_ms"] = engine.now_ms
            set_state(req, "failed", "并行副本中断；当前 KV 丢弃，本版本不承诺无损续写", reason=reason)
            observed["failed_requests"] += 1
        release(replica)
        replica["state"] = "failed"
        replica["reason"] = reason
        engine.record("replica_failed", subject_id=replica["id"], state="failed", reason=reason,
                      allocated_gpu_ids=[], released_request_memory=True, epoch=replica["epoch"])

    for req in requests:
        def arrive(req=req):
            set_state(req, "queued", "请求到达")
            dispatch()
        engine.schedule(req["arrival_ms"], arrive)
    faults = spec.get("faults", [])
    if not isinstance(faults, list) or len(faults) > 64:
        raise ValueError("faults must be an array with at most 64 entries")
    for fault in faults:
        at = _number(fault.get("at_ms"), "fault.at_ms")
        kind = fault.get("type")
        target = fault.get("target_id")
        if kind not in {"node_down","node_up","resource_down","resource_up"}:
            raise ValueError("Unknown fault type")
        if not isinstance(target, str): raise ValueError("fault.target_id must be a string")
        if kind.startswith("node_") and target not in {g.node_id for g in network.gpus.values()}:
            raise ValueError(f"Unknown fault node {target}")
        if kind.startswith("resource_") and target not in network.capacities:
            raise ValueError(f"Unknown fault resource {target}")
        def change(kind=kind, target=target):
            {"node_down":network.fail_node,"node_up":network.repair_node,
             "resource_down":network.fail_resource,"resource_up":network.repair_resource}[kind](target)
            engine.record("fault", fault_type=kind, target_id=target,
                          message="故障注入仅作用于虚拟拓扑")
            for replica in replicas:
                if replica["state"] == "infeasible": continue
                if not healthy(replica):
                    if replica["state"] != "failed": abort(replica, kind+":"+target)
                elif replica["state"] == "failed":
                    replica["state"] = "loading"
                    replica["epoch"] += 1
                    epoch = replica["epoch"]
                    def loaded(replica=replica, epoch=epoch):
                        if replica["epoch"] != epoch or not healthy(replica): return
                        replica["state"] = "ready"; replica["reason"] = ""
                        engine.record("replica_reloaded", subject_id=replica["id"], state="ready", epoch=epoch)
                        dispatch()
                    engine.after(reload_ms, loaded)
            dispatch()
        engine.schedule(at, change, priority=-10)
    engine.run(horizon)
    for req in requests:
        if req["state"] in {"scheduled","queued","prefill","decoding"}:
            prior = req["state"]
            if prior == "scheduled":
                req["reason"] = "arrival_after_horizon"
            elif engine.now_ms >= horizon:
                req["reason"] = "simulation_horizon_reached"
            else:
                # With all replicas down and no future repair, the event queue
                # can drain before the requested horizon. Do not report a time
                # limit that the simulation never reached.
                req["reason"] = "no_future_replica_recovery"
            set_state(req, "incomplete", "仿真停止时请求未完成，不能计入成功",
                      previous_state=prior, reason=req["reason"])
    stats = engine.stats()
    completed = [r for r in requests if r["state"] == "completed"]
    total_output = sum(r["generated_tokens"] for r in completed)
    first_arrival = min(r["arrival_ms"] for r in requests)
    last_completion = max((r["completed_ms"] for r in completed), default=first_arrival)
    span = last_completion-first_arrival
    checks = [
        {"name":"replica_gpu_exclusivity", "passed":len(gpu_owners)==sum(r["tp_size"] for r in replicas),
         "detail":f"validated {len(gpu_owners)} distinct assigned GPUs"},
        {"name":"memory_capacity", "passed":observed["memory_violations"]==0,
         "detail":f"{observed['memory_checks']} admission/token checks; {observed['memory_violations']} violations"},
        {"name":"output_token_limit", "passed":observed["token_violations"]==0,
         "detail":f"{observed['token_checks']} observed token commits"},
        {"name":"kv_growth", "passed":observed["kv_growth_violations"]==0,
         "detail":f"{observed['kv_growth_checks']} KV updates checked"},
        {"name":"link_capacity", "passed":stats.get("max_capacity_ratio",0)<=1+1e-8,
         "detail":f"maximum observed ratio={stats.get('max_capacity_ratio',0)}"},
        {"name":"failed_requests_not_completed", "passed":all(r.get("failed_ms") is None or r["state"]=="failed" for r in requests),
         "detail":f"{observed['failed_requests']} interrupted requests; no retry/resume implemented"},
    ]
    for replica in replicas:
        replica["allocated_gpu_ids"] = list(replica["gpu_ids"]) if replica["state"] in {"ready","busy","loading"} else []
        replica["resident_memory_bytes_per_gpu"] = (replica["base_memory_bytes_per_gpu"]+replica["reserved_kv_bytes_per_gpu"]
                                                       if replica["allocated_gpu_ids"] else 0)
    return {"schema_version":2,"scenario":"tp","model":model,"profile":profile,
            "summary":{"requests":len(requests),"completed":len(completed),
                       "failed":sum(r["state"]=="failed" for r in requests),
                       "rejected":sum(r["state"]=="rejected" for r in requests),
                       "incomplete":sum(r["state"]=="incomplete" for r in requests),
                       "simulated_end_ms":engine.now_ms,"successful_output_tokens":total_output,
                       "successful_output_throughput_window_ms":span,
                       "successful_output_tokens_per_second":total_output*1000/span if span>0 else 0,
                       "mean_ttft_ms":sum(r["ttft_ms"] for r in completed)/len(completed) if completed else None,
                       "kv_bytes_per_token_whole_model":kv_per_token,"allreduce_calls":observed["allreduce_calls"],
                       "stale_callbacks_ignored":observed["stale_callbacks_ignored"],"profile_source":source},
            "requests":requests,"replicas":replicas,"events":engine.events,
            "resource_samples":engine.resource_samples,"engine_stats":stats,
            "invariant_checks":checks,
            "assumptions":["Megatron-style two AllReduce operations per transformer-layer forward; no vLLM/continuous batching emulation.",
                           "One active request per replica; independent replicas may compete for network resources.",
                           "No computation/communication overlap; decode compute timing is a configured constant per generated-token forward.",
                           "Uniform weight sharding plus explicit workspace is a memory estimate, not exact framework allocation.",
                           "Only transformer-layer activation collectives are modeled; embedding/logit collectives, input transport and sampling compute are omitted.",
                           "KV admission reserves prompt+max_new_tokens conservatively; last sampled token need not yet be cached.",
                           "First token is produced by prefill; each subsequent token requires one decode forward.",
                           "Replica failure fails its active request and discards KV; recovery reloads the model for queued/new requests.",
                           "Successful-output throughput uses first request arrival to last successful completion; failures/rejections are reported separately.",
                           "Synthetic leaf-spine topology is not the supplied single-host eight-3090 NUMA topology.",
                           f"Compute profile source: {source}; Qwen demo parameter count is model-card rounded, not exact tensor enumeration."]}
