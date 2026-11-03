from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from aggregator import merge_worker_results
from evaluator import evaluate_shard
from fabric_domain import (
    EvaluationConfig,
    FirstFitPlacementStrategy,
    RailAwarePlacementStrategy,
    RevisionContent,
    evaluate_placement,
)


def partition_job_ids(job_ids: list[str], worker_ids: tuple[str, ...]) -> dict[str, list[str]]:
    if not worker_ids:
        raise ValueError("At least one worker is required")
    partitions = {worker_id: [] for worker_id in worker_ids}
    for index, job_id in enumerate(sorted(job_ids)):
        partitions[worker_ids[index % len(worker_ids)]].append(job_id)
    return partitions


@dataclass(frozen=True, slots=True)
class DistributedEvaluation:
    evaluation: dict[str, Any]
    assignments: dict[str, list[str]]
    failed_workers: tuple[str, ...]
    reassigned: dict[str, str]
    recovery_ms: float
    events: tuple[dict[str, object], ...]


class DistributedEvaluationCoordinator:
    def __init__(self, worker_count: int = 2) -> None:
        if worker_count <= 0:
            raise ValueError("worker_count must be positive")
        self.worker_ids = tuple(f"worker-{index + 1}" for index in range(worker_count))

    def evaluate(
        self,
        revision_content: dict[str, Any],
        strategy: str,
        flow_unit_gbps: float,
        failed_workers: set[str] | None = None,
    ) -> DistributedEvaluation:
        started = perf_counter()
        job_ids = [str(job["id"]) for job in revision_content["jobs"]]
        initial = partition_job_ids(job_ids, self.worker_ids)
        failed = tuple(sorted(failed_workers or set()))
        unknown_workers = set(failed) - set(self.worker_ids)
        if unknown_workers:
            raise ValueError(f"Unknown failed workers: {sorted(unknown_workers)}")
        healthy = tuple(worker for worker in self.worker_ids if worker not in failed)
        if not healthy:
            raise ValueError("All evaluation workers are unavailable")

        # Routing uses shared link loads, so it cannot be independently decided
        # per shard. Freeze one canonical route plan before partition evaluation.
        # This remains an in-process protocol demo, not a parallel speedup.
        content = RevisionContent.from_dict(revision_content)
        if strategy not in {"rail_aware", "first_fit"}:
            raise ValueError(f"Unknown placement strategy: {strategy}")
        placement_strategy = (
            RailAwarePlacementStrategy() if strategy == "rail_aware" else FirstFitPlacementStrategy()
        )
        global_placement = placement_strategy.place(content.topology, content.jobs)
        global_evaluation = evaluate_placement(
            content.topology,
            content.jobs,
            global_placement,
            EvaluationConfig(flow_unit_gbps=flow_unit_gbps),
        )
        routing_plan = [
            {
                "job_id": flow.job_id,
                "source_node_id": flow.source_node_id,
                "target_node_id": flow.target_node_id,
                "path_link_ids": list(flow.path_link_ids),
            }
            for flow in global_evaluation.flows
        ]

        assignments = {worker: [] for worker in healthy}
        reassigned: dict[str, str] = {}
        for worker_id, assigned_jobs in initial.items():
            target = worker_id if worker_id in healthy else healthy[0]
            if target != worker_id:
                reassigned[worker_id] = target
            assignments[target].extend(assigned_jobs)

        events: list[dict[str, object]] = [
            {
                "type": "worker_failed",
                "worker_id": worker_id,
                "occurred_at": datetime.now(UTC).isoformat(),
            }
            for worker_id in failed
        ]
        results: list[dict[str, Any]] = []
        for worker_id in healthy:
            assigned_jobs = sorted(assignments[worker_id])
            events.append(
                {
                    "type": "partition_assigned",
                    "worker_id": worker_id,
                    "job_ids": assigned_jobs,
                    "occurred_at": datetime.now(UTC).isoformat(),
                }
            )
            if not assigned_jobs:
                continue
            results.append(
                evaluate_shard(
                    {
                        "worker_id": worker_id,
                        "job_ids": assigned_jobs,
                        "revision_content": revision_content,
                        "strategy": strategy,
                        "flow_unit_gbps": flow_unit_gbps,
                        "routing_plan": routing_plan,
                    }
                )
            )

        if not results:
            raise ValueError("No work was assigned to healthy workers")
        merged = merge_worker_results(results)
        elapsed_ms = (perf_counter() - started) * 1000
        events.append(
            {
                "type": "results_merged",
                "worker_count": len(results),
                "occurred_at": datetime.now(UTC).isoformat(),
            }
        )
        return DistributedEvaluation(
            evaluation=merged,
            assignments={worker: sorted(jobs) for worker, jobs in assignments.items()},
            failed_workers=failed,
            reassigned=reassigned,
            recovery_ms=round(elapsed_ms, 3),
            events=tuple(events),
        )
