from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from fabric_domain.evaluation import EvaluationConfig, EvaluationResult, evaluate_placement
from fabric_domain.models import RevisionContent, Topology
from fabric_domain.placement import (
    FirstFitPlacementStrategy,
    JobPlacement,
    PlacementResult,
    RailAwarePlacementStrategy,
)


class FaultError(ValueError):
    """Raised when a fault references an unknown fabric entity."""


class FaultType(StrEnum):
    LINK_DOWN = "link_down"
    SWITCH_DOWN = "switch_down"
    NODE_DOWN = "node_down"


@dataclass(frozen=True, slots=True)
class Fault:
    type: FaultType
    target_id: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Fault:
        try:
            return cls(
                type=FaultType(str(value["type"])),
                target_id=str(value["target_id"]),
            )
        except (KeyError, ValueError) as exc:
            raise FaultError(f"Invalid fault: {value!r}") from exc

    def to_dict(self) -> dict[str, str]:
        return {"type": self.type.value, "target_id": self.target_id}


@dataclass(frozen=True, slots=True)
class FaultApplication:
    topology: Topology
    removed_link_ids: tuple[str, ...]
    removed_node_ids: tuple[str, ...]
    disconnected_node_ids: tuple[str, ...]

    @property
    def unavailable_node_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(set(self.removed_node_ids) | set(self.disconnected_node_ids))
        )


@dataclass(frozen=True, slots=True)
class Migration:
    job_id: str
    from_node_ids: tuple[str, ...]
    to_node_ids: tuple[str, ...]
    moved_gpu_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "from_node_ids": list(self.from_node_ids),
            "to_node_ids": list(self.to_node_ids),
            "moved_gpu_count": self.moved_gpu_count,
        }


@dataclass(frozen=True, slots=True)
class ReplanResult:
    faults: tuple[Fault, ...]
    application: FaultApplication
    original_placement: PlacementResult
    placement: PlacementResult
    evaluation: EvaluationResult
    affected_job_ids: tuple[str, ...]
    unaffected_job_ids: tuple[str, ...]
    migrations: tuple[Migration, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "faults": [fault.to_dict() for fault in self.faults],
            "removed_link_ids": list(self.application.removed_link_ids),
            "removed_node_ids": list(self.application.removed_node_ids),
            "disconnected_node_ids": list(
                self.application.disconnected_node_ids
            ),
            "unavailable_node_ids": list(
                self.application.unavailable_node_ids
            ),
            "original_placement": self.original_placement.to_dict(),
            "placement": self.placement.to_dict(),
            "evaluation": self.evaluation.to_dict(),
            "affected_job_ids": list(self.affected_job_ids),
            "unaffected_job_ids": list(self.unaffected_job_ids),
            "migrations": [migration.to_dict() for migration in self.migrations],
            "complete": self.placement.complete,
        }


def apply_faults(topology: Topology, faults: tuple[Fault, ...]) -> FaultApplication:
    link_ids = {link.id for link in topology.links}
    spine_ids = {spine.id for spine in topology.spines}
    leaf_ids = {leaf.id for leaf in topology.leaves}
    node_ids = {node.id for node in topology.nodes}
    removed_links: set[str] = set()
    removed_nodes: set[str] = set()

    for fault in faults:
        if fault.type is FaultType.LINK_DOWN:
            if fault.target_id not in link_ids:
                raise FaultError(f"Unknown link: {fault.target_id}")
            removed_links.add(fault.target_id)
        elif fault.type is FaultType.SWITCH_DOWN:
            if fault.target_id not in spine_ids | leaf_ids:
                raise FaultError(f"Unknown switch: {fault.target_id}")
            removed_links.update(
                link.id
                for link in topology.links
                if fault.target_id
                in {link.endpoint_a, link.endpoint_b}
            )
        elif fault.type is FaultType.NODE_DOWN:
            if fault.target_id not in node_ids:
                raise FaultError(f"Unknown node: {fault.target_id}")
            removed_nodes.add(fault.target_id)
            removed_links.update(
                link.id
                for link in topology.links
                if fault.target_id
                in {link.endpoint_a, link.endpoint_b}
            )

    candidate_nodes = tuple(
        node for node in topology.nodes if node.id not in removed_nodes
    )
    candidate_links = tuple(
        link for link in topology.links if link.id not in removed_links
    )
    candidate_topology = replace(
        topology,
        nodes=candidate_nodes,
        links=candidate_links,
    )
    reachable_nodes = _reachable_node_ids(candidate_topology)
    disconnected = tuple(
        sorted(node.id for node in candidate_nodes if node.id not in reachable_nodes)
    )
    active_node_ids = {node.id for node in candidate_nodes} - set(disconnected)
    active_topology = replace(
        candidate_topology,
        nodes=tuple(
            node for node in candidate_nodes if node.id in active_node_ids
        ),
        links=tuple(
            link
            for link in candidate_links
            if link.endpoint_a not in set(disconnected)
            and link.endpoint_b not in set(disconnected)
        ),
    )
    return FaultApplication(
        topology=active_topology,
        removed_link_ids=tuple(sorted(removed_links)),
        removed_node_ids=tuple(sorted(removed_nodes)),
        disconnected_node_ids=disconnected,
    )


def replan_after_faults(
    content: RevisionContent,
    faults: tuple[Fault, ...],
    *,
    strategy_name: str = "rail_aware",
    flow_unit_gbps: float = 10.0,
) -> ReplanResult:
    original_strategy = _strategy(strategy_name)
    original_placement = original_strategy.place(content.topology, content.jobs)
    application = apply_faults(content.topology, faults)

    unavailable_nodes = set(application.unavailable_node_ids)
    affected: set[str] = set()
    for assignment in original_placement.assignments:
        if any(gpu.node_id in unavailable_nodes for gpu in assignment.gpus):
            affected.add(assignment.job_id)

    old_faulted_evaluation = evaluate_placement(
        application.topology,
        content.jobs,
        original_placement,
        EvaluationConfig(flow_unit_gbps=flow_unit_gbps),
    )
    affected.update(
        flow.job_id for flow in old_faulted_evaluation.flows if flow.unroutable
    )
    affected_job_ids = tuple(
        job_id
        for job_id in original_placement.job_order
        if job_id in affected
    )
    unaffected_job_ids = tuple(
        job_id
        for job_id in original_placement.job_order
        if job_id not in affected
    )

    preserved = tuple(
        assignment
        for assignment in original_placement.assignments
        if assignment.job_id in unaffected_job_ids
    )
    reserved_gpu_ids = {
        gpu.gpu_id for assignment in preserved for gpu in assignment.gpus
    }
    available_nodes = tuple(
        replace(
            node,
            gpus=tuple(
                gpu for gpu in node.gpus if gpu.id not in reserved_gpu_ids
            ),
        )
        for node in application.topology.nodes
        if node.id not in unavailable_nodes
    )
    available_topology = replace(
        application.topology,
        nodes=available_nodes,
    )
    affected_jobs = tuple(
        job for job in content.jobs if job.id in affected
    )
    replanned = _strategy(strategy_name).place(
        available_topology,
        affected_jobs,
    )

    assignments_by_job = {
        assignment.job_id: assignment
        for assignment in (*preserved, *replanned.assignments)
    }
    merged_assignments = tuple(
        assignments_by_job[job_id]
        for job_id in original_placement.job_order
        if job_id in assignments_by_job
    )
    unplaced = tuple(
        job_id
        for job_id in original_placement.job_order
        if job_id not in assignments_by_job
    )
    merged_placement = PlacementResult(
        strategy=f"replanned_{strategy_name}",
        topology_id=content.topology.id,
        job_order=original_placement.job_order,
        total_gpus=application.topology.gpu_count,
        assignments=merged_assignments,
        unplaced_job_ids=unplaced,
        findings=replanned.findings,
    )
    evaluation = evaluate_placement(
        application.topology,
        content.jobs,
        merged_placement,
        EvaluationConfig(flow_unit_gbps=flow_unit_gbps),
    )

    original_assignments = {
        assignment.job_id: assignment
        for assignment in original_placement.assignments
    }
    migrations: list[Migration] = []
    for assignment in merged_assignments:
        original = original_assignments.get(assignment.job_id)
        if original is None:
            continue
        if original.node_ids != assignment.node_ids:
            original_gpu_ids = {gpu.gpu_id for gpu in original.gpus}
            moved_gpu_count = sum(
                gpu.gpu_id not in original_gpu_ids for gpu in assignment.gpus
            )
            migrations.append(
                Migration(
                    job_id=assignment.job_id,
                    from_node_ids=original.node_ids,
                    to_node_ids=assignment.node_ids,
                    moved_gpu_count=moved_gpu_count,
                )
            )

    return ReplanResult(
        faults=faults,
        application=application,
        original_placement=original_placement,
        placement=merged_placement,
        evaluation=evaluation,
        affected_job_ids=affected_job_ids,
        unaffected_job_ids=unaffected_job_ids,
        migrations=tuple(migrations),
    )


def _strategy(name: str) -> FirstFitPlacementStrategy | RailAwarePlacementStrategy:
    if name == "first_fit":
        return FirstFitPlacementStrategy()
    if name == "rail_aware":
        return RailAwarePlacementStrategy()
    raise ValueError(f"Unknown placement strategy: {name}")


def _reachable_node_ids(topology: Topology) -> set[str]:
    graph: dict[str, set[str]] = defaultdict(set)
    for link in topology.links:
        graph[link.endpoint_a].add(link.endpoint_b)
        graph[link.endpoint_b].add(link.endpoint_a)

    starts = {
        spine.id
        for spine in topology.spines
        if graph[spine.id]
    }
    if not starts:
        return set()
    visited = set(starts)
    queue = deque(sorted(starts))
    while queue:
        current = queue.popleft()
        for neighbor in graph[current]:
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    return {node.id for node in topology.nodes if node.id in visited}
