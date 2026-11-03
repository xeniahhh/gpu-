from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Any

from fabric_domain import (
    EvaluationResult,
    FlowEvaluation,
    JobEvaluation,
    LeafOversubscription,
    LinkEvaluation,
)


def merge_worker_results(worker_results: list[dict[str, Any]]) -> dict[str, Any]:
    if not worker_results:
        raise ValueError("At least one worker result is required")

    evaluations = [item["evaluation"] for item in worker_results]
    base = evaluations[0]
    flow_unit_gbps = float(base["flow_unit_gbps"])
    expected_capacities = {
        str(link["link_id"]): float(link["capacity_gbps"])
        for link in base["links"]
    }
    seen_jobs: set[str] = set()
    for result in worker_results:
        evaluation = result["evaluation"]
        for key in ("topology_id", "placement_strategy", "flow_unit_gbps"):
            if evaluation[key] != base[key]:
                raise ValueError(f"Worker results disagree on {key}")
        capacities = {
            str(link["link_id"]): float(link["capacity_gbps"])
            for link in evaluation["links"]
        }
        if capacities != expected_capacities:
            raise ValueError("Worker results disagree on link capacities")
        assigned_jobs = {str(job_id) for job_id in result["job_ids"]}
        if seen_jobs & assigned_jobs:
            raise ValueError("Duplicate job partition in worker results")
        seen_jobs.update(assigned_jobs)
        if any(str(flow["job_id"]) not in assigned_jobs for flow in evaluation["flows"]):
            raise ValueError("Worker flow references a job outside its partition")
    flows = sorted(
        (
            FlowEvaluation(
                job_id=str(flow["job_id"]),
                source_node_id=str(flow["source_node_id"]),
                target_node_id=str(flow["target_node_id"]),
                traffic_units=int(flow["traffic_units"]),
                path_link_ids=tuple(str(link) for link in flow["path_link_ids"]),
                allocated_gbps=float(flow["allocated_gbps"]),
                per_unit_gbps=float(flow["per_unit_gbps"]),
                unroutable=bool(flow["unroutable"]),
            )
            for evaluation in evaluations
            for flow in evaluation["flows"]
        ),
        key=lambda flow: (
            flow.job_id,
            flow.source_node_id,
            flow.target_node_id,
        ),
    )

    load_units: Counter[str] = Counter()
    link_template = base["links"]
    capacity_by_link = {
        str(link["link_id"]): float(link["capacity_gbps"])
        for link in link_template
    }
    for flow in flows:
        if flow.unroutable != (not flow.path_link_ids):
            raise ValueError("Worker flow route and unreachable flag disagree")
        if flow.traffic_units <= 0:
            raise ValueError("Worker flow traffic units must be positive")
        for link_id in flow.path_link_ids:
            if link_id not in capacity_by_link:
                raise ValueError(f"Worker flow references unknown link {link_id!r}")
            load_units[link_id] += flow.traffic_units

    # Shard-local rates ignore traffic in other partitions. Recompute every
    # flow from merged loads before deriving either link or job metrics.
    globally_allocated: list[FlowEvaluation] = []
    carried_by_link: Counter[str] = Counter()
    for flow in flows:
        rate = (
            0.0
            if flow.unroutable
            else min(
                flow_unit_gbps,
                min(capacity_by_link[link_id] / load_units[link_id] for link_id in flow.path_link_ids),
            )
        )
        updated = replace(
            flow,
            per_unit_gbps=round(rate, 6),
            allocated_gbps=round(rate * flow.traffic_units, 6),
        )
        globally_allocated.append(updated)
        for link_id in updated.path_link_ids:
            carried_by_link[link_id] += updated.allocated_gbps
    flows = globally_allocated

    links = tuple(
        LinkEvaluation(
            link_id=str(link["link_id"]),
            capacity_gbps=float(link["capacity_gbps"]),
            traffic_units=load_units[str(link["link_id"])],
            requested_gbps=round(
                load_units[str(link["link_id"])] * flow_unit_gbps,
                6,
            ),
            allocated_gbps=round(
                carried_by_link[str(link["link_id"])],
                6,
            ),
            utilization=round(
                load_units[str(link["link_id"])] * flow_unit_gbps
                / float(link["capacity_gbps"]),
                6,
            ),
            oversubscribed=(
                load_units[str(link["link_id"])] * flow_unit_gbps
                > float(link["capacity_gbps"])
            ),
        )
        for link in link_template
    )
    links_by_id = {link.link_id: link for link in links}

    job_ids = sorted(
        {
            flow.job_id
            for flow in flows
        }
        | {
            str(job_id)
            for result in worker_results
            for job_id in result["job_ids"]
        }
    )
    jobs: list[JobEvaluation] = []
    for job_id in job_ids:
        job_flows = [flow for flow in flows if flow.job_id == job_id]
        if not job_flows:
            jobs.append(
                JobEvaluation(
                    job_id=job_id,
                    flow_count=0,
                    traffic_units=0,
                    throughput_proxy_gbps=None,
                    bottleneck_link_ids=(),
                )
            )
            continue

        per_unit = min(flow.per_unit_gbps for flow in job_flows)
        path_links = {
            link_id for flow in job_flows for link_id in flow.path_link_ids
        }
        if path_links:
            highest = max(links_by_id[link_id].utilization for link_id in path_links)
            bottlenecks = tuple(
                sorted(
                    link_id
                    for link_id in path_links
                    if links_by_id[link_id].utilization == highest
                )
            )
        else:
            bottlenecks = ()
        jobs.append(
            JobEvaluation(
                job_id=job_id,
                flow_count=len(job_flows),
                traffic_units=sum(flow.traffic_units for flow in job_flows),
                throughput_proxy_gbps=round(per_unit, 6),
                bottleneck_link_ids=bottlenecks,
            )
        )

    leaf_oversubscription = tuple(
        LeafOversubscription(
            leaf_id=str(item["leaf_id"]),
            downlink_capacity_gbps=float(item["downlink_capacity_gbps"]),
            uplink_capacity_gbps=float(item["uplink_capacity_gbps"]),
            ratio=None if item["ratio"] is None else float(item["ratio"]),
        )
        for item in base["leaf_oversubscription"]
    )
    result = EvaluationResult(
        topology_id=str(base["topology_id"]),
        placement_strategy=str(base["placement_strategy"]),
        flow_unit_gbps=flow_unit_gbps,
        flows=tuple(flows),
        links=links,
        jobs=tuple(jobs),
        leaf_oversubscription=leaf_oversubscription,
        balanced_leaf_bisection_proxy_gbps=float(
            base["balanced_leaf_bisection_proxy_gbps"]
        ),
    )
    return result.to_dict()
