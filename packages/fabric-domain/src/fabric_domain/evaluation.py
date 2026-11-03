from __future__ import annotations

import math
from collections import Counter, defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass

from fabric_domain.models import Job, LinkKind, Topology
from fabric_domain.placement import JobPlacement, PlacementResult


class EvaluationError(ValueError):
    """Raised when placement or topology cannot be evaluated."""


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    flow_unit_gbps: float = 10.0
    max_candidate_paths: int = 64

    def validate(self) -> None:
        if (
            isinstance(self.flow_unit_gbps, bool)
            or not isinstance(self.flow_unit_gbps, (int, float))
            or not math.isfinite(self.flow_unit_gbps)
            or self.flow_unit_gbps <= 0
        ):
            raise EvaluationError("flow_unit_gbps must be a finite positive number")
        if (
            isinstance(self.max_candidate_paths, bool)
            or not isinstance(self.max_candidate_paths, int)
            or self.max_candidate_paths <= 0
        ):
            raise EvaluationError("max_candidate_paths must be positive")


@dataclass(frozen=True, slots=True)
class FlowEvaluation:
    job_id: str
    source_node_id: str
    target_node_id: str
    traffic_units: int
    path_link_ids: tuple[str, ...]
    allocated_gbps: float
    per_unit_gbps: float
    unroutable: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "traffic_units": self.traffic_units,
            "path_link_ids": list(self.path_link_ids),
            "allocated_gbps": self.allocated_gbps,
            "per_unit_gbps": self.per_unit_gbps,
            "unroutable": self.unroutable,
        }


@dataclass(frozen=True, slots=True)
class LinkEvaluation:
    link_id: str
    capacity_gbps: float
    traffic_units: int
    requested_gbps: float
    allocated_gbps: float
    utilization: float
    oversubscribed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "link_id": self.link_id,
            "capacity_gbps": self.capacity_gbps,
            "traffic_units": self.traffic_units,
            "requested_gbps": self.requested_gbps,
            "allocated_gbps": self.allocated_gbps,
            "utilization": self.utilization,
            "oversubscribed": self.oversubscribed,
        }


@dataclass(frozen=True, slots=True)
class JobEvaluation:
    job_id: str
    flow_count: int
    traffic_units: int
    throughput_proxy_gbps: float | None
    bottleneck_link_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "flow_count": self.flow_count,
            "traffic_units": self.traffic_units,
            "throughput_proxy_gbps": self.throughput_proxy_gbps,
            "bottleneck_link_ids": list(self.bottleneck_link_ids),
        }


@dataclass(frozen=True, slots=True)
class LeafOversubscription:
    leaf_id: str
    downlink_capacity_gbps: float
    uplink_capacity_gbps: float
    ratio: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "leaf_id": self.leaf_id,
            "downlink_capacity_gbps": self.downlink_capacity_gbps,
            "uplink_capacity_gbps": self.uplink_capacity_gbps,
            "ratio": self.ratio,
        }


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    topology_id: str
    placement_strategy: str
    flow_unit_gbps: float
    flows: tuple[FlowEvaluation, ...]
    links: tuple[LinkEvaluation, ...]
    jobs: tuple[JobEvaluation, ...]
    leaf_oversubscription: tuple[LeafOversubscription, ...]
    balanced_leaf_bisection_proxy_gbps: float

    @property
    def unroutable_flow_count(self) -> int:
        return sum(flow.unroutable for flow in self.flows)

    @property
    def oversubscribed_link_ids(self) -> tuple[str, ...]:
        return tuple(
            link.link_id for link in self.links if link.oversubscribed
        )

    def top_bottlenecks(self, limit: int = 5) -> tuple[LinkEvaluation, ...]:
        active = sorted(
            (link for link in self.links if link.traffic_units > 0),
            key=lambda link: (-link.utilization, link.link_id),
        )
        return tuple(active[:limit])

    def to_dict(self) -> dict[str, object]:
        return {
            "topology_id": self.topology_id,
            "placement_strategy": self.placement_strategy,
            "flow_unit_gbps": self.flow_unit_gbps,
            "unroutable_flow_count": self.unroutable_flow_count,
            "oversubscribed_link_ids": list(self.oversubscribed_link_ids),
            "balanced_leaf_bisection_proxy_gbps": (
                self.balanced_leaf_bisection_proxy_gbps
            ),
            "flows": [flow.to_dict() for flow in self.flows],
            "links": [link.to_dict() for link in self.links],
            "jobs": [job.to_dict() for job in self.jobs],
            "leaf_oversubscription": [
                item.to_dict() for item in self.leaf_oversubscription
            ],
        }


@dataclass(frozen=True, slots=True)
class _FlowSeed:
    job_id: str
    source_node_id: str
    target_node_id: str
    traffic_units: int
    path_link_ids: tuple[str, ...] = ()


def _shortest_paths(
    graph: dict[str, list[tuple[str, str]]],
    source: str,
    target: str,
    limit: int,
) -> tuple[tuple[str, ...], ...]:
    if source == target:
        return ((),)
    if source not in graph or target not in graph:
        return ()

    distances = {source: 0}
    parents: dict[str, list[tuple[str, str]]] = defaultdict(list)
    queue = deque([source])
    while queue:
        current = queue.popleft()
        for neighbor, link_id in graph[current]:
            next_distance = distances[current] + 1
            if neighbor not in distances:
                distances[neighbor] = next_distance
                parents[neighbor].append((current, link_id))
                queue.append(neighbor)
            elif distances.get(neighbor) == next_distance:
                parents[neighbor].append((current, link_id))

    if target not in distances:
        return ()

    paths: list[tuple[str, ...]] = []

    def walk(node: str, suffix: tuple[str, ...]) -> None:
        if len(paths) >= limit:
            return
        if node == source:
            paths.append(suffix)
            return
        for parent, link_id in sorted(parents[node]):
            walk(parent, (link_id, *suffix))

    walk(target, ())
    return tuple(sorted(set(paths))[:limit])


def _network_flow_units(assignment: JobPlacement, job: Job) -> tuple[tuple[str, str, int], ...]:
    node_counts: Counter[str] = Counter(gpu.node_id for gpu in assignment.gpus)
    node_ids = sorted(node_counts)
    pair_units: Counter[tuple[str, str]] = Counter()

    if job.communication_mode.value == "ring_all_reduce":
        ranks = [gpu.node_id for gpu in assignment.gpus]
        if len(ranks) > 1:
            for index, source in enumerate(ranks):
                target = ranks[(index + 1) % len(ranks)]
                if source != target:
                    pair_units[tuple(sorted((source, target)))] += 1
    elif job.communication_mode.value == "all_to_all":
        for left_index, left in enumerate(node_ids):
            for right in node_ids[left_index + 1 :]:
                pair_units[(left, right)] += node_counts[left] * node_counts[right]
    else:
        raise EvaluationError(
            f"Unsupported communication mode: {job.communication_mode.value}"
        )

    return tuple(
        (source, target, units)
        for (source, target), units in sorted(pair_units.items())
    )


def evaluate_placement(
    topology: Topology,
    jobs: Sequence[Job],
    placement: PlacementResult,
    config: EvaluationConfig | None = None,
    *,
    fixed_paths: dict[tuple[str, str, str], tuple[str, ...]] | None = None,
) -> EvaluationResult:
    effective = config or EvaluationConfig()
    effective.validate()

    jobs_by_id = {job.id: job for job in jobs}
    assignments = {assignment.job_id: assignment for assignment in placement.assignments}
    if len(jobs_by_id) != len(jobs):
        raise EvaluationError("Job IDs must be unique for evaluation")

    links_by_id = {link.id: link for link in topology.links}
    graph: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for link in topology.links:
        if (
            not math.isfinite(link.capacity_gbps)
            or link.capacity_gbps <= 0
        ):
            raise EvaluationError(f"Link {link.id!r} has invalid capacity")
        graph[link.endpoint_a].append((link.endpoint_b, link.id))
        graph[link.endpoint_b].append((link.endpoint_a, link.id))
    for edges in graph.values():
        edges.sort()

    load_units: Counter[str] = Counter()
    seeds: list[_FlowSeed] = []

    for job_id in placement.job_order:
        assignment = assignments.get(job_id)
        if assignment is None:
            continue
        job = jobs_by_id.get(job_id)
        if job is None:
            raise EvaluationError(f"Placement references unknown job {job_id!r}")
        for source, target, units in _network_flow_units(assignment, job):
            if fixed_paths is None:
                paths = _shortest_paths(
                    graph,
                    source,
                    target,
                    effective.max_candidate_paths,
                )
            else:
                key = (job_id, source, target)
                if key not in fixed_paths:
                    raise EvaluationError(f"Missing global route for {key!r}")
                route = fixed_paths[key]
                # Empty routes represent unreachable flows. Nonempty routes must
                # actually connect this pair, not merely reference valid links.
                current = source
                for link_id in route:
                    link = links_by_id.get(link_id)
                    if link is None or current not in (link.endpoint_a, link.endpoint_b):
                        raise EvaluationError(f"Invalid global route for {key!r}")
                    current = link.endpoint_b if current == link.endpoint_a else link.endpoint_a
                if route and current != target:
                    raise EvaluationError(f"Global route does not reach {target!r}")
                paths = (route,) if route else ()
            if not paths:
                seeds.append(
                    _FlowSeed(
                        job_id=job_id,
                        source_node_id=source,
                        target_node_id=target,
                        traffic_units=units,
                    )
                )
                continue

            def path_cost(path: tuple[str, ...]) -> tuple[float, float, tuple[str, ...]]:
                projected = [
                    (load_units[link_id] + units) / links_by_id[link_id].capacity_gbps
                    for link_id in path
                ]
                return max(projected), sum(projected), path

            selected_path = min(paths, key=path_cost)
            for link_id in selected_path:
                load_units[link_id] += units
            seeds.append(
                _FlowSeed(
                    job_id=job_id,
                    source_node_id=source,
                    target_node_id=target,
                    traffic_units=units,
                    path_link_ids=selected_path,
                )
            )

    flows: list[FlowEvaluation] = []
    for seed in seeds:
        if not seed.path_link_ids:
            flows.append(
                FlowEvaluation(
                    job_id=seed.job_id,
                    source_node_id=seed.source_node_id,
                    target_node_id=seed.target_node_id,
                    traffic_units=seed.traffic_units,
                    path_link_ids=(),
                    allocated_gbps=0.0,
                    per_unit_gbps=0.0,
                    unroutable=True,
                )
            )
            continue
        available_unit_rate = min(
            effective.flow_unit_gbps,
            min(
                links_by_id[link_id].capacity_gbps / load_units[link_id]
                for link_id in seed.path_link_ids
            ),
        )
        flows.append(
            FlowEvaluation(
                job_id=seed.job_id,
                source_node_id=seed.source_node_id,
                target_node_id=seed.target_node_id,
                traffic_units=seed.traffic_units,
                path_link_ids=seed.path_link_ids,
                allocated_gbps=round(
                    available_unit_rate * seed.traffic_units,
                    6,
                ),
                per_unit_gbps=round(available_unit_rate, 6),
                unroutable=False,
            )
        )

    # A remote bottleneck can leave this link below its own capacity. Report
    # carried flow traffic, rather than min(local demand, local capacity).
    carried_by_link: Counter[str] = Counter()
    for flow in flows:
        for link_id in flow.path_link_ids:
            carried_by_link[link_id] += flow.allocated_gbps

    link_evaluations = tuple(
        LinkEvaluation(
            link_id=link.id,
            capacity_gbps=link.capacity_gbps,
            traffic_units=load_units[link.id],
            requested_gbps=round(
                load_units[link.id] * effective.flow_unit_gbps,
                6,
            ),
            allocated_gbps=round(
                carried_by_link[link.id],
                6,
            ),
            utilization=round(
                load_units[link.id]
                * effective.flow_unit_gbps
                / link.capacity_gbps,
                6,
            ),
            oversubscribed=(
                load_units[link.id] * effective.flow_unit_gbps
                > link.capacity_gbps
            ),
        )
        for link in topology.links
    )
    links_eval_by_id = {link.link_id: link for link in link_evaluations}

    job_evaluations: list[JobEvaluation] = []
    for job_id in placement.job_order:
        job_flows = [flow for flow in flows if flow.job_id == job_id]
        if not job_flows:
            job_evaluations.append(
                JobEvaluation(
                    job_id=job_id,
                    flow_count=0,
                    traffic_units=0,
                    throughput_proxy_gbps=None,
                    bottleneck_link_ids=(),
                )
            )
            continue

        path_links = {
            link_id for flow in job_flows for link_id in flow.path_link_ids
        }
        if path_links:
            highest_utilization = max(
                links_eval_by_id[link_id].utilization for link_id in path_links
            )
            bottleneck_ids = tuple(
                sorted(
                    link_id
                    for link_id in path_links
                    if links_eval_by_id[link_id].utilization == highest_utilization
                )
            )
        else:
            bottleneck_ids = ()
        job_evaluations.append(
            JobEvaluation(
                job_id=job_id,
                flow_count=len(job_flows),
                traffic_units=sum(flow.traffic_units for flow in job_flows),
                throughput_proxy_gbps=min(
                    flow.per_unit_gbps for flow in job_flows
                ),
                bottleneck_link_ids=bottleneck_ids,
            )
        )

    ordered_leaves = sorted(topology.leaves, key=lambda item: item.id)
    leaf_ids = {leaf.id for leaf in ordered_leaves}
    uplink_by_leaf: Counter[str] = Counter()
    downlink_by_leaf: Counter[str] = Counter()
    for link in topology.links:
        if link.kind is LinkKind.SPINE_LEAF:
            leaf_id = (
                link.endpoint_b
                if link.endpoint_b in leaf_ids
                else link.endpoint_a
            )
            uplink_by_leaf[leaf_id] += link.capacity_gbps
        elif link.kind is LinkKind.LEAF_NODE:
            leaf_id = (
                link.endpoint_a
                if link.endpoint_a in leaf_ids
                else link.endpoint_b
            )
            downlink_by_leaf[leaf_id] += link.capacity_gbps

    oversubscription = tuple(
        LeafOversubscription(
            leaf_id=leaf.id,
            downlink_capacity_gbps=downlink_by_leaf[leaf.id],
            uplink_capacity_gbps=uplink_by_leaf[leaf.id],
            ratio=(
                downlink_by_leaf[leaf.id] / uplink_by_leaf[leaf.id]
                if uplink_by_leaf[leaf.id] > 0
                else None
            ),
        )
        for leaf in ordered_leaves
    )

    split_index = len(ordered_leaves) // 2
    left_leaves = ordered_leaves[:split_index]
    right_leaves = ordered_leaves[split_index:]
    left_capacity = sum(uplink_by_leaf[leaf.id] for leaf in left_leaves)
    right_capacity = sum(uplink_by_leaf[leaf.id] for leaf in right_leaves)
    bisection_proxy = min(left_capacity, right_capacity)

    return EvaluationResult(
        topology_id=topology.id,
        placement_strategy=placement.strategy,
        flow_unit_gbps=float(effective.flow_unit_gbps),
        flows=tuple(flows),
        links=link_evaluations,
        jobs=tuple(job_evaluations),
        leaf_oversubscription=oversubscription,
        balanced_leaf_bisection_proxy_gbps=round(bisection_proxy, 6),
    )
