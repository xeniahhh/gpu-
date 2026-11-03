from __future__ import annotations

from typing import Any

from fabric_domain import (
    EvaluationConfig,
    FirstFitPlacementStrategy,
    PlacementResult,
    RailAwarePlacementStrategy,
    RevisionContent,
    evaluate_placement,
)


def evaluate_shard(payload: dict[str, Any]) -> dict[str, Any]:
    worker_id = str(payload["worker_id"])
    content = RevisionContent.from_dict(payload["revision_content"])
    job_ids = {str(job_id) for job_id in payload["job_ids"]}
    strategy_name = str(payload.get("strategy", "rail_aware"))
    flow_unit_gbps = float(payload.get("flow_unit_gbps", 10.0))
    job_by_id = {job.id: job for job in content.jobs}
    jobs = tuple(
        job_by_id[job_id]
        for job_id in sorted(job_ids)
        if job_id in job_by_id
    )
    unknown = job_ids - set(job_by_id)
    if unknown:
        raise ValueError(f"Unknown job IDs: {sorted(unknown)}")

    strategy = (
        RailAwarePlacementStrategy()
        if strategy_name == "rail_aware"
        else FirstFitPlacementStrategy()
    )
    full_placement = strategy.place(content.topology, content.jobs)
    assignments = tuple(
        assignment
        for assignment in full_placement.assignments
        if assignment.job_id in job_ids
    )
    findings = tuple(
        finding
        for finding in full_placement.findings
        if finding.job_id in job_ids
    )
    unplaced = tuple(
        job_id
        for job_id in full_placement.unplaced_job_ids
        if job_id in job_ids
    )
    placement = PlacementResult(
        strategy=full_placement.strategy,
        topology_id=full_placement.topology_id,
        job_order=tuple(
            job_id for job_id in full_placement.job_order if job_id in job_ids
        ),
        total_gpus=full_placement.total_gpus,
        assignments=assignments,
        unplaced_job_ids=unplaced,
        findings=findings,
    )
    evaluation = evaluate_placement(
        content.topology,
        jobs,
        placement,
        EvaluationConfig(flow_unit_gbps=flow_unit_gbps),
        fixed_paths=(
            {
                (str(route["job_id"]), str(route["source_node_id"]), str(route["target_node_id"])):
                tuple(str(link_id) for link_id in route["path_link_ids"])
                for route in payload["routing_plan"]
            }
            if "routing_plan" in payload
            else None
        ),
    )
    return {
        "worker_id": worker_id,
        "job_ids": [job.id for job in jobs],
        "evaluation": evaluation.to_dict(),
    }
