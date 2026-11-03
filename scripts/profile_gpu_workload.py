#!/usr/bin/env python3
"""Optional, explicitly invoked GPU measurements. Simulation never imports torch.

Run --help without PyTorch. GPT-2 is constructed from configuration, so no model
weights, tokenizer, dataset, or account credentials are downloaded.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time


class Unavailable(RuntimeError):
    pass


def summary(values):
    ordered = sorted(values)
    return {"count": len(values), "median_ms": statistics.median(values),
            "min_ms": min(values), "max_ms": max(values),
            "mean_ms": statistics.mean(values),
            "stdev_ms": statistics.stdev(values) if len(values) > 1 else 0.0,
            "p95_ms": ordered[max(0, int((len(values) - 1) * .95))]}


def gpu_metadata(torch, device):
    props = torch.cuda.get_device_properties(device)
    return {"logical_device": device, "name": props.name,
            "total_memory_bytes": props.total_memory,
            "uuid": str(getattr(props, "uuid", "unavailable")),
            "compute_capability": [props.major, props.minor]}


def metadata(torch):
    info = {"python": platform.python_version(), "platform": platform.platform(),
            "torch": torch.__version__, "cuda_runtime": torch.version.cuda,
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", "UNSET"),
            "nccl_environment": {k: os.environ[k] for k in
                ("NCCL_ALGO", "NCCL_PROTO", "NCCL_P2P_DISABLE", "NCCL_SHM_DISABLE") if k in os.environ}}
    try:
        info["transformers"] = importlib.metadata.version("transformers")
    except importlib.metadata.PackageNotFoundError:
        info["transformers"] = "not installed"
    for command, key in ((["nvidia-smi", "-L"], "gpu_inventory"),
                         (["nvidia-smi", "topo", "-m"], "topology")):
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=5)
            info[key] = result.stdout if result.returncode == 0 else "unavailable"
        except (OSError, subprocess.TimeoutExpired):
            info[key] = "unavailable"
    return info


def load_torch():
    try:
        import torch
    except ImportError as exc:
        raise Unavailable("PyTorch is not installed in this interpreter; GPU measurements were not run") from exc
    if not torch.cuda.is_available():
        raise Unavailable("CUDA is not available; GPU measurements were not run")
    return torch


def measure_profile(args):
    torch = load_torch()
    try:
        from transformers import GPT2Config, GPT2LMHeadModel
    except ImportError as exc:
        raise Unavailable("Transformers with GPT2LMHeadModel is required; no weights are downloaded") from exc
    if args.device >= torch.cuda.device_count():
        raise ValueError("--device must identify a visible CUDA device")
    torch.cuda.set_device(args.device)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    config = GPT2Config(vocab_size=50257, n_positions=1024, n_ctx=1024,
                        n_embd=768, n_layer=12, n_head=12, n_inner=3072,
                        tie_word_embeddings=True, use_cache=False)
    model = GPT2LMHeadModel(config).to(device="cuda", dtype=torch.float32).train()
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4, foreach=False)
    inputs = torch.randint(0, config.vocab_size, (args.batch_size, args.sequence_length), device="cuda")
    parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    expected = 124439808
    if parameters != expected:
        raise RuntimeError(f"Model parameter count {parameters} differs from fixed GPT-2 profile {expected}")

    def one_step():
        optimizer.zero_grad(set_to_none=True)
        start, backward_done, end = [torch.cuda.Event(enable_timing=True) for _ in range(3)]
        torch.cuda.synchronize()
        wall_start = time.perf_counter()
        start.record()
        loss = model(input_ids=inputs, labels=inputs).loss
        loss.backward()
        backward_done.record()
        optimizer.step()
        end.record()
        torch.cuda.synchronize()
        return {"forward_backward_ms": start.elapsed_time(backward_done),
                "optimizer_ms": backward_done.elapsed_time(end),
                "cuda_step_ms": start.elapsed_time(end),
                "wall_step_ms": (time.perf_counter() - wall_start) * 1000}

    for _ in range(args.warmup):
        one_step()
    torch.cuda.reset_peak_memory_stats()
    samples = [one_step() for _ in range(args.repeat)]
    gradient_bytes = sum(p.grad.numel() * p.grad.element_size()
                         for p in model.parameters() if p.grad is not None)
    if gradient_bytes != 4 * parameters:
        raise RuntimeError("Observed FP32 gradient payload does not cover all trainable parameters")
    stats = {key: summary([s[key] for s in samples]) for key in samples[0]}
    allocated = torch.cuda.max_memory_allocated()
    reserved = torch.cuda.max_memory_reserved()
    return {"status": "COMPLETED", "source": "MEASURED", "kind": "gpt2_compute_profile",
            "model": "GPT-2 Small, random initialization", "config": config.to_dict(),
            "parameter_count": parameters, "gradient_bytes": gradient_bytes,
            "parameter_dtype": "float32", "gradient_dtype": "float32", "optimizer": "AdamW(foreach=False)",
            "local_batch": args.batch_size, "sequence_length": args.sequence_length,
            "seed": args.seed, "warmup": args.warmup, "repeat": args.repeat,
            "memory": {"peak_allocated_bytes": allocated, "peak_reserved_bytes": reserved,
                       "configured_margin_bytes": int(args.memory_margin_gib * 2**30),
                       "admission_budget_bytes": reserved + int(args.memory_margin_gib * 2**30)},
            "timing": stats, "samples": samples, "metadata": metadata(torch),
            "device": gpu_metadata(torch, args.device),
            "limitations": ["Synthetic tokens and random weights test system load, not model quality.",
                            "No DDP gradient buckets are allocated in this single-GPU measurement.",
                            "PyTorch reserved memory does not include all framework-external/NCCL allocations; the margin is a stated policy, not a measurement.",
                            "Do not replace measured DDP iteration time by a sum of these times and standalone AllReduce; overlap differs."]}


def measure_collective(args):
    torch = load_torch()
    import torch.distributed as dist
    world = int(os.environ.get("WORLD_SIZE", "1"))
    rank = int(os.environ.get("RANK", "0"))
    local = int(os.environ.get("LOCAL_RANK", "0"))
    if world < 2:
        raise Unavailable("Launch collective mode using torchrun --nproc-per-node=2 (or 4/8)")
    dtypes = {"float32": (torch.float32, 4), "float16": (torch.float16, 2), "bfloat16": (torch.bfloat16, 2)}
    dtype, width = dtypes[args.dtype]
    if args.bytes % width:
        raise ValueError("--bytes must be divisible by the selected dtype size")
    torch.cuda.set_device(local)
    dist.init_process_group("nccl", timeout=timedelta(seconds=args.timeout_seconds))
    try:
        tensor = torch.empty(args.bytes // width, device="cuda", dtype=dtype)
        samples = []
        for index in range(args.warmup + args.repeat):
            tensor.fill_(1)
            dist.barrier()
            torch.cuda.synchronize()
            start, end = [torch.cuda.Event(enable_timing=True) for _ in range(2)]
            start.record()
            dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
            end.record()
            torch.cuda.synchronize()
            if index >= args.warmup:
                samples.append(start.elapsed_time(end))
        correct = bool(torch.all(tensor == world).item())
        observations = [None] * world
        dist.all_gather_object(observations, {"rank": rank, "device": gpu_metadata(torch, local),
                                              "samples_ms": samples, "correct": correct})
        if rank != 0:
            return None
        slowest = [max(r["samples_ms"][i] for r in observations) for i in range(args.repeat)]
        median = statistics.median(slowest)
        algbw = args.bytes / (median / 1000) / 1e9
        return {"status": "COMPLETED" if all(r["correct"] for r in observations) else "FAILED",
                "source": "MEASURED", "kind": "nccl_allreduce", "world_size": world,
                "payload_bytes_per_rank": args.bytes, "dtype": args.dtype,
                "warmup": args.warmup, "repeat": args.repeat,
                "timing_slowest_rank": summary(slowest), "slowest_rank_samples_ms": slowest,
                "algorithm_bandwidth_GBps": algbw,
                "normalized_bus_bandwidth_GBps": algbw * 2 * (world - 1) / world,
                "ranks": observations, "metadata": metadata(torch),
                "limitations": ["This is standalone collective timing, not a training-throughput result.",
                                "The actual NCCL algorithm/transport is not inferred from topology; inspect logs separately when needed.",
                                "Single-host GPU measurements do not calibrate cross-server Leaf-Spine links.",
                                "Slowest-rank durations are compared within each repetition; clocks are not used for cross-rank timestamp subtraction."]}
    finally:
        dist.destroy_process_group()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="mode", required=True)
    for name in ("profile", "collective"):
        q = sub.add_parser(name)
        q.add_argument("--output", type=Path, required=True)
        q.add_argument("--warmup", type=int, default=5)
        q.add_argument("--repeat", type=int, default=20)
    q = sub.choices["profile"]
    q.add_argument("--device", type=int, default=0)
    q.add_argument("--batch-size", type=int, default=1)
    q.add_argument("--sequence-length", type=int, default=512)
    q.add_argument("--seed", type=int, default=20261102)
    q.add_argument("--memory-margin-gib", type=float, default=1.0)
    q = sub.choices["collective"]
    q.add_argument("--bytes", type=int, default=497759232)
    q.add_argument("--dtype", choices=("float32", "float16", "bfloat16"), default="float32")
    q.add_argument("--timeout-seconds", type=int, default=120)
    return p


def main():
    p = parser()
    args = p.parse_args()
    if not (1 <= args.warmup <= 100 and 1 <= args.repeat <= 1000):
        p.error("warmup must be 1..100 and repeat 1..1000")
    if args.mode == "profile" and not (1 <= args.sequence_length <= 1024 and 1 <= args.batch_size <= 64
                                       and args.device >= 0 and 0 <= args.memory_margin_gib <= 16):
        p.error("Invalid profile size, device, or memory margin")
    if args.mode == "collective" and not (0 < args.bytes <= 8 * 2**30 and args.timeout_seconds > 0):
        p.error("Payload must be positive and at most 8 GiB; timeout must be positive")
    try:
        result = measure_profile(args) if args.mode == "profile" else measure_collective(args)
        if result is None:
            return 0
    except Exception as exc:
        if int(os.environ.get("RANK", "0")) != 0:
            print(str(exc), file=sys.stderr)
            return 2
        result = {"status": "NOT_RUN" if isinstance(exc, Unavailable) else "FAILED",
                  "source": "UNAVAILABLE", "kind": args.mode,
                  "reason": str(exc), "error_type": type(exc).__name__}
    result["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if result["status"] == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
