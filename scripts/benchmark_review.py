"""Reproducible CPU model benchmarks; no GPU or network services are used.

Run with Python >=3.12:
  python scripts/benchmark_review.py --repeats 5 --output docs/benchmark_review.json
  python scripts/benchmark_review.py --compare-root /path/to/original/gpu
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter


def configure(root: Path) -> None:
    for path in ("packages/fabric-domain/src", "packages/event-contracts/src",
                 "services/planner/src", "services/aggregator/src", "services/evaluator/src"):
        sys.path.insert(0, str(root / path))


def summary(samples: list[float]) -> dict:
    return {"median_ms": round(statistics.median(samples), 4),
            "min_ms": round(min(samples), 4), "max_ms": round(max(samples), 4),
            "samples_ms": [round(v, 4) for v in samples]}


def measure(fn, repeats: int):
    fn()  # One excluded warmup per stage.
    samples = []
    result = None
    for _ in range(repeats):
        start = perf_counter()
        result = fn()
        samples.append((perf_counter() - start) * 1000)
    return result, summary(samples)


def canonical(value, field: str = ""):
    if isinstance(value, dict):
        return {k: canonical(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        result = [canonical(v) for v in value]
        # Collections are unordered, but a route's path_link_ids retains order.
        if field in {"flows", "links", "jobs", "leaf_oversubscription", "oversubscribed_link_ids"}:
            return sorted(result, key=lambda item: json.dumps(item, sort_keys=True))
        return result
    return value


def consistency(evaluation: dict) -> dict:
    flows = evaluation["flows"]
    violations = []
    mismatches = []
    for link in evaluation["links"]:
        total = sum(f["allocated_gbps"] for f in flows if link["link_id"] in f["path_link_ids"])
        if total > link["capacity_gbps"] + 1e-8:
            violations.append({"link_id": link["link_id"], "capacity_gbps": link["capacity_gbps"],
                               "flow_sum_gbps": round(total, 6)})
        if abs(total - link["allocated_gbps"]) > 1e-8:
            mismatches.append({"link_id": link["link_id"], "reported_allocated_gbps": link["allocated_gbps"],
                               "flow_sum_gbps": round(total, 6)})
    return {"capacity_violations": violations, "link_flow_mismatches": mismatches}


def fixture_case(root: Path, repeats: int) -> dict:
    from fabric_domain import (load_revision_file, FirstFitPlacementStrategy,
                               RailAwarePlacementStrategy, rail_alignment_score,
                               evaluate_placement, EvaluationConfig)
    from planner import DistributedEvaluationCoordinator

    content = load_revision_file(root / "tests/fixtures/topologies/rail-aware-comparison.json")
    comparison = []
    for strategy in (FirstFitPlacementStrategy(), RailAwarePlacementStrategy()):
        placement = strategy.place(content.topology, content.jobs)
        evaluation = evaluate_placement(content.topology, content.jobs, placement,
                                        EvaluationConfig(flow_unit_gbps=200.0))
        comparison.append({"strategy": strategy.name,
            "job_alignment": {p.job_id: rail_alignment_score(p) for p in placement.assignments},
            "job_throughput_proxy_gbps": {j.job_id: j.throughput_proxy_gbps for j in evaluation.jobs},
            "flow_count": len(evaluation.flows)})

    worker_results = []
    reference = None
    for workers, failed in ((1, set()), (2, set()), (3, set()), (2, {"worker-1"})):
        coordinator = DistributedEvaluationCoordinator(worker_count=workers)
        result, timing = measure(lambda: coordinator.evaluate(content.to_dict(), "rail_aware", 200.0, failed), repeats)
        normalized = canonical(result.evaluation)
        if reference is None:
            reference = normalized
        worker_results.append({"worker_count": workers, "failed_workers": sorted(failed),
            "equal_to_one_worker": normalized == reference,
            "cpu_end_to_end_time": timing,
            "job_throughput_proxy_gbps": {j["job_id"]: j["throughput_proxy_gbps"] for j in result.evaluation["jobs"]},
            "assignments": result.assignments, "reassigned": result.reassigned,
            **consistency(result.evaluation)})
    return {"fixture": "rail-aware-comparison.json", "flow_unit_gbps": 200.0,
            "strategy_comparison": comparison, "worker_results": worker_results}


def scaling_case(repeats: int) -> list[dict]:
    from fabric_domain import (TopologyGenerationConfig, generate_topology, validate_revision,
                               Job, CommunicationMode, RevisionContent,
                               RailAwarePlacementStrategy, evaluate_placement, EvaluationConfig)
    rows = []
    for gpu_count, leaves, nodes_per_leaf in ((16, 2, 1), (64, 4, 2), (256, 8, 4), (1024, 16, 8)):
        config = TopologyGenerationConfig(spine_count=8, leaf_count=leaves,
            nodes_per_leaf=nodes_per_leaf, gpus_per_node=8, topology_id=f"bench-{gpu_count}")
        topology, generation = measure(lambda: generate_topology(config), repeats)
        jobs = (Job("benchmark-job", 16, CommunicationMode.RING_ALL_REDUCE, 10),)
        content = RevisionContent(1, "bench-revision", topology, jobs)
        validation, validation_time = measure(lambda: validate_revision(content), repeats)
        placement, placement_time = measure(lambda: RailAwarePlacementStrategy().place(topology, jobs), repeats)
        evaluation, evaluation_time = measure(lambda: evaluate_placement(topology, jobs, placement,
                                              EvaluationConfig(flow_unit_gbps=10)), repeats)
        assert topology.gpu_count == gpu_count and validation.valid and placement.complete
        rows.append({"gpus": gpu_count, "spines": 8, "leaves": leaves,
                     "nodes": len(topology.nodes), "links": len(topology.links),
                     "jobs": 1, "requested_gpus": 16, "communication_mode": "ring_all_reduce",
                     "cross_node_flows": len(evaluation.flows),
                     "generation": generation, "validation": validation_time,
                     "placement": placement_time, "evaluation": evaluation_time})
    return rows


def workload_case(repeats: int) -> list[dict]:
    from fabric_domain import (generate_topology, Job, CommunicationMode,
                               RailAwarePlacementStrategy, evaluate_placement, EvaluationConfig)
    topology = generate_topology()
    rows = []
    for requested in (16, 32, 64, 128):
        for mode in (CommunicationMode.RING_ALL_REDUCE, CommunicationMode.ALL_TO_ALL):
            jobs = (Job("benchmark-job", requested, mode, 10),)
            placement = RailAwarePlacementStrategy().place(topology, jobs)
            evaluation, timing = measure(lambda: evaluate_placement(topology, jobs, placement,
                                        EvaluationConfig(flow_unit_gbps=10)), repeats)
            rows.append({"topology_gpus": 1024, "requested_gpus": requested,
                         "communication_mode": mode.value,
                         "cross_node_flows": len(evaluation.flows), "evaluation": timing})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--compare-root", type=Path)
    parser.add_argument("--fixture-only", action="store_true")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    root = args.root.resolve()
    configure(root)
    result = {"measured_at": datetime.now(timezone.utc).isoformat(),
              "environment": {"python": platform.python_version(), "platform": platform.platform(),
                              "machine": platform.machine(), "processor": platform.processor()},
              "method": {"timer": "time.perf_counter", "repeats": args.repeats,
                         "warmups_per_stage": 1, "runtime_mode": "single-process sequential CPU model",
                         "timings_exclude": ["HTTP", "browser", "database", "GPU", "real network", "startup"],
                         "interpretation": "Model computation latency only; not real GPU throughput or parallel speedup.",
                         "scaling_control": "1 ring job requesting 16 GPUs; 8 spines and 8 GPUs/node held constant. Nodes and leaves increase.",
                         "workload_control": "Default 1024-GPU topology; one job requests 16/32/64/128 GPUs."},
              "source_sha256": {path: hashlib.sha256((root/path).read_bytes()).hexdigest() for path in
                                ["packages/fabric-domain/src/fabric_domain/evaluation.py", "services/planner/src/planner/coordinator.py",
                                 "services/aggregator/src/aggregator/merge.py"]},
              "fixture": fixture_case(root, args.repeats)}
    if not args.fixture_only:
        result["topology_scaling"] = scaling_case(args.repeats)
        result["workload_scaling"] = workload_case(args.repeats)
    if args.compare_root:
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--root", str(args.compare_root.resolve()),
                               "--fixture-only", "--repeats", str(args.repeats)],
                              capture_output=True, text=True, check=True)
        result["original_baseline"] = json.loads(proc.stdout)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
