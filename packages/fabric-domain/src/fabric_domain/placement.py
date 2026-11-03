from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from fabric_domain.models import Job, Node, Topology


@dataclass(frozen=True, slots=True)
class PlacedGpu:
    node_id: str
    gpu_id: str
    slot: int
    rail_id: int

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "gpu_id": self.gpu_id,
            "slot": self.slot,
            "rail_id": self.rail_id,
        }


@dataclass(frozen=True, slots=True)
class JobPlacement:
    job_id: str
    node_ids: tuple[str, ...]
    gpus: tuple[PlacedGpu, ...]
    quality: PlacementQuality | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "job_id": self.job_id,
            "node_ids": list(self.node_ids),
            "gpus": [gpu.to_dict() for gpu in self.gpus],
        }
        if self.quality is not None:
            result["quality"] = self.quality.to_dict()
        return result


@dataclass(frozen=True, slots=True)
class PlacementQuality:
    node_count: int
    leaf_count: int
    rail_alignment: float
    fragmentation_penalty: float
    score: float

    def to_dict(self) -> dict[str, object]:
        return {
            "node_count": self.node_count,
            "leaf_count": self.leaf_count,
            "rail_alignment": self.rail_alignment,
            "fragmentation_penalty": self.fragmentation_penalty,
            "score": self.score,
        }


@dataclass(frozen=True, slots=True)
class PlacementFinding:
    code: str
    message: str
    job_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "job_id": self.job_id,
        }


@dataclass(frozen=True, slots=True)
class PlacementResult:
    strategy: str
    topology_id: str
    job_order: tuple[str, ...]
    total_gpus: int
    assignments: tuple[JobPlacement, ...]
    unplaced_job_ids: tuple[str, ...]
    findings: tuple[PlacementFinding, ...]

    @property
    def assigned_gpu_count(self) -> int:
        return sum(len(assignment.gpus) for assignment in self.assignments)

    @property
    def utilization(self) -> float:
        if self.total_gpus == 0:
            return 0.0
        return self.assigned_gpu_count / self.total_gpus

    @property
    def complete(self) -> bool:
        return not self.unplaced_job_ids and not self.findings

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "topology_id": self.topology_id,
            "job_order": list(self.job_order),
            "total_gpus": self.total_gpus,
            "assigned_gpus": self.assigned_gpu_count,
            "utilization": self.utilization,
            "complete": self.complete,
            "assignments": [
                assignment.to_dict() for assignment in self.assignments
            ],
            "unplaced_job_ids": list(self.unplaced_job_ids),
            "findings": [finding.to_dict() for finding in self.findings],
        }


class PlacementStrategy(Protocol):
    name: str

    def place(
        self,
        topology: Topology,
        jobs: Sequence[Job],
    ) -> PlacementResult: ...


class FirstFitPlacementStrategy:
    """Deterministic first-fit baseline over node ID and GPU slot."""

    name = "first_fit"

    def place(
        self,
        topology: Topology,
        jobs: Sequence[Job],
    ) -> PlacementResult:
        ordered_jobs = tuple(
            sorted(jobs, key=lambda job: (-job.priority, job.id))
        )
        candidates = tuple(
            PlacedGpu(
                node_id=node.id,
                gpu_id=gpu.id,
                slot=gpu.slot,
                rail_id=gpu.rail_id,
            )
            for node in sorted(topology.nodes, key=lambda item: item.id)
            for gpu in sorted(node.gpus, key=lambda item: (item.slot, item.id))
        )

        cursor = 0
        assignments: list[JobPlacement] = []
        unplaced: list[str] = []
        findings: list[PlacementFinding] = []

        for job in ordered_jobs:
            if job.requested_gpus <= 0:
                unplaced.append(job.id)
                findings.append(
                    PlacementFinding(
                        code="INVALID_JOB_SIZE",
                        message="Requested GPU count must be positive",
                        job_id=job.id,
                    )
                )
                continue

            remaining = len(candidates) - cursor
            if job.requested_gpus > remaining:
                unplaced.append(job.id)
                findings.append(
                    PlacementFinding(
                        code="INSUFFICIENT_GPUS",
                        message=(
                            f"Job requests {job.requested_gpus} GPUs; "
                            f"{remaining} remain available"
                        ),
                        job_id=job.id,
                    )
                )
                continue

            selected = candidates[cursor : cursor + job.requested_gpus]
            cursor += job.requested_gpus
            node_ids = tuple(dict.fromkeys(item.node_id for item in selected))
            assignments.append(
                JobPlacement(
                    job_id=job.id,
                    node_ids=node_ids,
                    gpus=selected,
                )
            )

        return PlacementResult(
            strategy=self.name,
            topology_id=topology.id,
            job_order=tuple(job.id for job in ordered_jobs),
            total_gpus=len(candidates),
            assignments=tuple(assignments),
            unplaced_job_ids=tuple(unplaced),
            findings=tuple(findings),
        )


def rail_alignment_score(
    assignment: JobPlacement,
) -> float:
    if len(assignment.node_ids) <= 1:
        return 1.0

    rails_by_node: dict[str, set[int]] = {}
    for gpu in assignment.gpus:
        rails_by_node.setdefault(gpu.node_id, set()).add(gpu.rail_id)
    used_rails = set().union(*rails_by_node.values())
    if not used_rails:
        return 0.0

    shared_rails = {
        rail_id
        for rail_id in used_rails
        if sum(rail_id in rails for rails in rails_by_node.values()) >= 2
    }
    return len(shared_rails) / len(used_rails)


class RailAwarePlacementStrategy:
    """Greedy rail-aware placement with a fragmentation-aware score."""

    name = "rail_aware"

    def place(
        self,
        topology: Topology,
        jobs: Sequence[Job],
    ) -> PlacementResult:
        ordered_jobs = tuple(
            sorted(jobs, key=lambda job: (-job.priority, job.id))
        )
        nodes = tuple(sorted(topology.nodes, key=lambda item: item.id))
        leaf_by_node = {node.id: node.leaf_id for node in topology.nodes}
        available: dict[str, list[PlacedGpu]] = {
            node.id: sorted(
                (
                    PlacedGpu(
                        node_id=node.id,
                        gpu_id=gpu.id,
                        slot=gpu.slot,
                        rail_id=gpu.rail_id,
                    )
                    for gpu in node.gpus
                ),
                key=lambda item: (item.rail_id, item.slot, item.gpu_id),
            )
            for node in nodes
        }

        assignments: list[JobPlacement] = []
        unplaced: list[str] = []
        findings: list[PlacementFinding] = []

        for job in ordered_jobs:
            if job.requested_gpus <= 0:
                unplaced.append(job.id)
                findings.append(
                    PlacementFinding(
                        code="INVALID_JOB_SIZE",
                        message="Requested GPU count must be positive",
                        job_id=job.id,
                    )
                )
                continue

            plans: dict[tuple[str, ...], JobPlacement] = {}
            for node_group in self._candidate_node_groups(
                nodes,
                available,
                job.requested_gpus,
            ):
                for mode in ("compact", "balanced"):
                    selected = self._select_gpus(
                        node_group,
                        available,
                        job.requested_gpus,
                        mode,
                    )
                    if len(selected) != job.requested_gpus:
                        continue
                    plan_key = tuple(gpu.gpu_id for gpu in selected)
                    if plan_key in plans:
                        continue
                    node_ids = tuple(dict.fromkeys(gpu.node_id for gpu in selected))
                    provisional = JobPlacement(
                        job_id=job.id,
                        node_ids=node_ids,
                        gpus=selected,
                    )
                    quality = self._quality(
                        provisional,
                        available,
                        leaf_by_node,
                    )
                    plans[plan_key] = JobPlacement(
                        job_id=job.id,
                        node_ids=node_ids,
                        gpus=selected,
                        quality=quality,
                    )

            if not plans:
                remaining = sum(len(gpus) for gpus in available.values())
                unplaced.append(job.id)
                findings.append(
                    PlacementFinding(
                        code="INSUFFICIENT_GPUS",
                        message=(
                            f"Job requests {job.requested_gpus} GPUs; "
                            f"{remaining} remain available"
                        ),
                        job_id=job.id,
                    )
                )
                continue

            selected_plan = min(
                plans.values(),
                key=lambda plan: (
                    -plan.quality.score if plan.quality is not None else 0.0,
                    tuple(gpu.gpu_id for gpu in plan.gpus),
                ),
            )
            assignments.append(selected_plan)

            selected_ids = {gpu.gpu_id for gpu in selected_plan.gpus}
            for node_id, gpus in available.items():
                available[node_id] = [
                    gpu for gpu in gpus if gpu.gpu_id not in selected_ids
                ]

        return PlacementResult(
            strategy=self.name,
            topology_id=topology.id,
            job_order=tuple(job.id for job in ordered_jobs),
            total_gpus=sum(len(node.gpus) for node in topology.nodes),
            assignments=tuple(assignments),
            unplaced_job_ids=tuple(unplaced),
            findings=tuple(findings),
        )

    @staticmethod
    def _candidate_node_groups(
        nodes: Sequence[Node],
        available: dict[str, list[PlacedGpu]],
        requested: int,
    ) -> tuple[tuple[str, ...], ...]:
        node_ids = [node.id for node in nodes]
        groups: list[tuple[str, ...]] = []
        for start in range(len(node_ids)):
            group: list[str] = []
            capacity = 0
            for node_id in node_ids[start:]:
                if not available[node_id]:
                    continue
                group.append(node_id)
                capacity += len(available[node_id])
                if capacity >= requested:
                    groups.append(tuple(group))
                    break
        return tuple(dict.fromkeys(groups))

    @staticmethod
    def _select_gpus(
        node_group: Sequence[str],
        available: dict[str, list[PlacedGpu]],
        requested: int,
        mode: str,
    ) -> tuple[PlacedGpu, ...]:
        if mode == "compact":
            shares = []
            remaining = requested
            for node_id in node_group:
                share = min(remaining, len(available[node_id]))
                shares.append(share)
                remaining -= share
        else:
            shares = [0 for _ in node_group]
            for _ in range(requested):
                eligible = [
                    index
                    for index, node_id in enumerate(node_group)
                    if shares[index] < len(available[node_id])
                ]
                if not eligible:
                    break
                chosen = min(
                    eligible,
                    key=lambda index: (shares[index], node_group[index]),
                )
                shares[chosen] += 1

        selected: list[PlacedGpu] = []
        for node_id, share in zip(node_group, shares, strict=True):
            selected.extend(available[node_id][:share])
        return tuple(selected)

    @staticmethod
    def _quality(
        assignment: JobPlacement,
        available: dict[str, list[PlacedGpu]],
        leaf_by_node: dict[str, str],
    ) -> PlacementQuality:
        selected_counts: dict[str, int] = {}
        for gpu in assignment.gpus:
            selected_counts[gpu.node_id] = selected_counts.get(gpu.node_id, 0) + 1

        node_count = len(assignment.node_ids)
        leaf_count = len(
            {leaf_by_node[node_id] for node_id in assignment.node_ids}
        )
        rail_alignment = rail_alignment_score(assignment)
        partial_nodes = sum(
            1
            for node_id, selected_count in selected_counts.items()
            if len(available[node_id]) > selected_count
        )
        fragmentation_penalty = partial_nodes / node_count
        score = (
            0.55 * rail_alignment
            + 0.20 * (1.0 / node_count)
            + 0.15 * (1.0 / leaf_count)
            + 0.10 * (1.0 - fragmentation_penalty)
        )
        return PlacementQuality(
            node_count=node_count,
            leaf_count=leaf_count,
            rail_alignment=rail_alignment,
            fragmentation_penalty=fragmentation_penalty,
            score=round(score, 6),
        )
