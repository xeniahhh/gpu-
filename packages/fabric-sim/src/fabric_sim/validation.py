"""Bounded, versioned simulation inputs; business feasibility stays in adapters."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any


def canonical_hash(spec: dict[str, Any]) -> str:
    try:
        raw = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Input must be finite JSON") from exc
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _num(value: Any, name: str, low: float, high: float, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    if integer and not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be in [{low}, {high}]")
    return value


def _id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError(f"{name} must use 1–64 letters, numbers, '_' or '-'")
    return value


def _list(value: Any, name: str, low: int, high: int) -> list:
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise ValueError(f"{name} must contain {low}–{high} items")
    return value


def validate_spec(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Simulation specification must be an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Simulation specification must be finite JSON") from exc
    if len(encoded.encode("utf-8")) > 131072:
        raise ValueError("Simulation specification exceeds 128 KiB")
    spec = deepcopy(value)
    if type(spec.get("schema_version")) is not int or spec["schema_version"] != 2:
        raise ValueError("schema_version must be 2")
    case = spec.get("scenario")
    if not isinstance(case, str) or case not in {"ddp", "tp"}:
        raise ValueError("scenario must be ddp or tp")
    horizon = _num(spec.get("horizon_ms", 20000), "horizon_ms", 1, 60000)
    top = spec.get("topology")
    if not isinstance(top, dict):
        raise ValueError("topology must be an object")
    nodes = _list(top.get("nodes"), "topology.nodes", 1, 16)
    ids: set[str] = set(); gpu_ids: set[str] = set(); resources: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("Each node must be an object")
        nid = _id(node.get("id"), "node.id"); rid = _id(node.get("rack_id"), "node.rack_id")
        if nid in ids:
            raise ValueError(f"Duplicate node: {nid}")
        ids.add(nid)
        count = int(_num(node.get("gpu_count", 2), "gpu_count", 1, 8, True))
        _num(node.get("memory_gib", 24), "memory_gib", 0.01, 1024)
        gpu_ids.update(f"{nid}:gpu{i}" for i in range(count))
        resources.update(f"node:{nid}:{d}" for d in ("tx", "rx", "local"))
        resources.update(f"rack:{rid}:{d}" for d in ("tx", "rx"))
    if len(gpu_ids) > 64:
        raise ValueError("At most 64 GPUs are supported")
    for key in ("node_bandwidth_gbps", "uplink_bandwidth_gbps", "local_bandwidth_gbps"):
        if key in top:
            _num(top[key], key, 0.001, 100000)
    for key in ("link_latency_us", "local_latency_us"):
        if key in top:
            _num(top[key], key, 0, 1000000)
    for fault in _list(spec.get("faults", []), "faults", 0, 32):
        if not isinstance(fault, dict):
            raise ValueError("Each fault must be an object")
        _num(fault.get("at_ms"), "fault.at_ms", 0, horizon)
        typ = fault.get("type"); target = fault.get("target_id")
        if not isinstance(typ, str) or not isinstance(target, str):
            raise ValueError("Fault type and target_id must be strings")
        allowed = ids if typ in {"node_down", "node_up"} else resources
        if typ not in {"node_down", "node_up", "resource_down", "resource_up"} or target not in allowed:
            raise ValueError(f"Unknown fault type/target: {typ}/{target}")
    profile = spec.get("profile", {})
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    if not isinstance(profile.get("source", "SYNTHETIC"), str) or profile.get("source", "SYNTHETIC") not in {"SYNTHETIC", "MEASURED", "DERIVED"}:
        raise ValueError("profile.source must be SYNTHETIC, MEASURED or DERIVED")
    for key in ("compute_ms", "optimizer_ms", "checkpoint_save_ms", "restore_ms", "group_init_ms", "model_reload_ms"):
        if key in profile:
            _num(profile[key], f"profile.{key}", 0, 60000)
    for key in ("peak_memory_gib", "workspace_gib"):
        if key in profile:
            _num(profile[key], f"profile.{key}", 0, 1024)
    if case == "ddp":
        if not isinstance(spec.get("placement_policy", "compact"), str) or spec.get("placement_policy", "compact") not in {"compact", "spread", "first_fit"}:
            raise ValueError("Unknown placement_policy")
        items = _list(spec.get("jobs"), "jobs", 1, 12)
        seen = set(); transfers = 0
        for job in items:
            if not isinstance(job, dict):
                raise ValueError("Each job must be an object")
            jid = _id(job.get("id"), "job.id")
            if jid in seen:
                raise ValueError("Duplicate job id")
            seen.add(jid)
            world = job.get("world_size", 4)
            _num(world, "world_size", 1, 8, True)
            if world not in (1, 2, 4, 8):
                raise ValueError("DDP world_size must be 1, 2, 4 or 8")
            steps = _num(job.get("steps", 30), "steps", 1, 200, True)
            _num(job.get("arrival_ms", 0), "arrival_ms", 0, horizon)
            _num(job.get("priority", 0), "priority", -1000, 1000, True)
            _num(job.get("checkpoint_interval", 10), "checkpoint_interval", 0, 200, True)
            if "gpu_ids" in job:
                fixed = _list(job["gpu_ids"], "job.gpu_ids", world, world)
                if any(not isinstance(g, str) for g in fixed) or len(set(fixed)) != world or any(g not in gpu_ids for g in fixed):
                    raise ValueError("job.gpu_ids must be distinct known GPUs")
            transfers += int(steps) * 2 * (world - 1) * world
        if transfers > 35000:
            raise ValueError("DDP scenario exceeds the course simulator transfer budget; reduce jobs/steps")
        from .ddp import training_profile
        training_profile(profile)  # domain preflight without advancing the simulation
    else:
        model = spec.get("model", {})
        if not isinstance(model, dict):
            raise ValueError("model must be an object")
        from .tp import qwen_model
        model = {**qwen_model(), **model}
        spec["model"] = model
        for key, default, maximum in (("parameter_count", 7610000000, 1000000000000), ("layers", 28, 128),
                                     ("hidden_size", 3584, 32768), ("query_heads", 28, 256), ("kv_heads", 4, 256),
                                     ("head_dim", 128, 512), ("dtype_bytes", 2, 8), ("max_context_tokens", 32768, 262144)):
            _num(model.get(key, default), f"model.{key}", 1, maximum, True)
        if model["hidden_size"] != model["query_heads"] * model["head_dim"] or model["query_heads"] % model["kv_heads"]:
            raise ValueError("Model attention dimensions are inconsistent")
        if profile.get("source") == "MEASURED" and (not profile.get("measurement_id") or
                any(k not in profile for k in ("prefill_ms_per_token_by_tp", "decode_ms_per_token_by_tp", "workspace_gib"))):
            raise ValueError("MEASURED TP profile needs measurement_id and explicit compute/memory measurements")
        reps = _list(spec.get("replicas"), "replicas", 1, 8)
        seen = set(); assigned = set(); max_tp = 1
        for replica in reps:
            if not isinstance(replica, dict):
                raise ValueError("Each replica must be an object")
            rid = _id(replica.get("id"), "replica.id")
            if rid in seen:
                raise ValueError("Duplicate replica id")
            seen.add(rid)
            tp = replica.get("tp_size", 1)
            replica.setdefault("tp_size", tp)
            _num(tp, "tp_size", 1, 4, True)
            if tp not in (1, 2, 4):
                raise ValueError("This TP model supports TP=1/2/4")
            if model.get("query_heads", 28) % tp or model.get("kv_heads", 4) % tp:
                raise ValueError("TP must divide query_heads and kv_heads in this uniform-sharding model")
            fixed = _list(replica.get("gpu_ids"), "replica.gpu_ids", tp, tp)
            if any(not isinstance(g, str) for g in fixed) or len(set(fixed)) != tp or any(g not in gpu_ids for g in fixed) or assigned.intersection(fixed):
                raise ValueError("Replica GPUs must be known, distinct and exclusive")
            assigned.update(fixed); max_tp = max(max_tp, tp)
        requests = _list(spec.get("requests"), "requests", 1, 64)
        seen = set(); passes = 0
        for req in requests:
            if not isinstance(req, dict):
                raise ValueError("Each request must be an object")
            rid = _id(req.get("id"), "request.id")
            if rid in seen:
                raise ValueError("Duplicate request id")
            seen.add(rid)
            _num(req.get("arrival_ms", 0), "request.arrival_ms", 0, horizon)
            _num(req.get("prompt_tokens", 128), "prompt_tokens", 1, 262144, True)
            passes += int(_num(req.get("max_new_tokens", 8), "max_new_tokens", 1, 256, True))
            req.setdefault("prompt_tokens", 128)
            req.setdefault("max_new_tokens", 8)
        stages = 2 * model.get("layers", 28) * passes
        if stages > 50000 or stages * 2 * (max_tp - 1) * max_tp > 35000:
            raise ValueError("TP scenario exceeds the course simulator transfer budget; reduce requests/tokens")
        for key in ("prefill_ms_per_token_by_tp", "decode_ms_per_token_by_tp"):
            if key in profile:
                if not isinstance(profile[key], dict):
                    raise ValueError(f"{key} must be a TP-to-time mapping")
                for k, v in profile[key].items():
                    if str(k) not in {"1", "2", "4"}:
                        raise ValueError(f"Unsupported TP profile key: {k}")
                    _num(v, f"{key}.{k}", 0, 60000)
                if any(str(replica["tp_size"]) not in profile[key] for replica in reps):
                    raise ValueError(f"{key} must specify every deployed TP size")
    return spec
