from __future__ import annotations

import math
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from fabric_domain.models import Job, RevisionContent, Topology


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    code: str
    severity: Severity
    message: str
    entity_type: str
    entity_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    findings: tuple[ValidationFinding, ...]

    @property
    def errors(self) -> tuple[ValidationFinding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.severity is Severity.ERROR
        )

    @property
    def warnings(self) -> tuple[ValidationFinding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.severity is Severity.WARNING
        )

    @property
    def valid(self) -> bool:
        return not self.errors

    def codes(self) -> tuple[str, ...]:
        return tuple(finding.code for finding in self.findings)

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "findings": [finding.to_dict() for finding in self.findings],
        }


def _finding(
    code: str,
    message: str,
    entity_type: str,
    entity_id: str,
    severity: Severity = Severity.ERROR,
) -> ValidationFinding:
    return ValidationFinding(
        code=code,
        severity=severity,
        message=message,
        entity_type=entity_type,
        entity_id=entity_id,
    )


def _ordered(findings: Iterable[ValidationFinding]) -> tuple[ValidationFinding, ...]:
    unique = {
        (
            finding.code,
            finding.severity.value,
            finding.entity_type,
            finding.entity_id,
            finding.message,
        ): finding
        for finding in findings
    }
    return tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda item: (item[0], item[2], item[3], item[4]),
        )
    )


def validate_topology(
    topology: Topology,
    *,
    expected_gpus_per_node: int | None = None,
) -> ValidationReport:
    findings: list[ValidationFinding] = []
    entities: list[tuple[str, str]] = []

    entities.extend(("spine", spine.id) for spine in topology.spines)
    entities.extend(("rack", rack.id) for rack in topology.racks)
    entities.extend(("leaf", leaf.id) for leaf in topology.leaves)
    entities.extend(("node", node.id) for node in topology.nodes)
    for node in topology.nodes:
        entities.extend(("gpu", gpu.id) for gpu in node.gpus)
    entities.extend(("link", link.id) for link in topology.links)

    entity_types: dict[str, set[str]] = defaultdict(set)
    id_counts = Counter(entity_id for _, entity_id in entities)
    for entity_type, entity_id in entities:
        entity_types[entity_id].add(entity_type)
    for entity_id, count in id_counts.items():
        if count > 1:
            types = ", ".join(sorted(entity_types[entity_id]))
            findings.append(
                _finding(
                    "DUPLICATE_ENTITY_ID",
                    f"ID {entity_id!r} is used {count} times across: {types}",
                    "entity",
                    entity_id,
                )
            )

    spine_ids = {spine.id for spine in topology.spines}
    rack_ids = {rack.id for rack in topology.racks}
    leaves_by_id = {leaf.id: leaf for leaf in topology.leaves}
    nodes_by_id = {node.id: node for node in topology.nodes}

    if not topology.spines:
        findings.append(
            _finding("TOPOLOGY_WITHOUT_SPINES", "Topology has no spines", "topology", topology.id)
        )
    if not topology.leaves:
        findings.append(
            _finding("TOPOLOGY_WITHOUT_LEAVES", "Topology has no leaves", "topology", topology.id)
        )
    if not topology.nodes:
        findings.append(
            _finding("TOPOLOGY_WITHOUT_NODES", "Topology has no nodes", "topology", topology.id)
        )

    for leaf in topology.leaves:
        if leaf.rack_id not in rack_ids:
            findings.append(
                _finding(
                    "MISSING_LEAF_RACK",
                    f"Leaf references missing rack {leaf.rack_id!r}",
                    "leaf",
                    leaf.id,
                )
            )

    for node in topology.nodes:
        if node.rack_id not in rack_ids:
            findings.append(
                _finding(
                    "MISSING_NODE_RACK",
                    f"Node references missing rack {node.rack_id!r}",
                    "node",
                    node.id,
                )
            )
        leaf = leaves_by_id.get(node.leaf_id)
        if leaf is None:
            findings.append(
                _finding(
                    "MISSING_NODE_LEAF",
                    f"Node references missing leaf {node.leaf_id!r}",
                    "node",
                    node.id,
                )
            )
        elif leaf.rack_id != node.rack_id:
            findings.append(
                _finding(
                    "NODE_RACK_LEAF_MISMATCH",
                    f"Node rack {node.rack_id!r} does not match leaf rack {leaf.rack_id!r}",
                    "node",
                    node.id,
                )
            )

        if not node.gpus:
            findings.append(
                _finding(
                    "NODE_WITHOUT_GPUS",
                    "Node has no GPUs",
                    "node",
                    node.id,
                )
            )
        if expected_gpus_per_node is not None and len(node.gpus) != expected_gpus_per_node:
            findings.append(
                _finding(
                    "UNEXPECTED_GPU_COUNT",
                    f"Node has {len(node.gpus)} GPUs; expected {expected_gpus_per_node}",
                    "node",
                    node.id,
                )
            )

        slots = Counter(gpu.slot for gpu in node.gpus)
        rails = Counter(gpu.rail_id for gpu in node.gpus)
        for slot, count in slots.items():
            if slot < 0:
                findings.append(
                    _finding(
                        "INVALID_GPU_SLOT",
                        f"GPU slot {slot} must be non-negative",
                        "node",
                        node.id,
                    )
                )
            if count > 1:
                findings.append(
                    _finding(
                        "DUPLICATE_GPU_SLOT",
                        f"GPU slot {slot} is assigned {count} times",
                        "node",
                        node.id,
                    )
                )
        for rail_id, count in rails.items():
            if rail_id < 0:
                findings.append(
                    _finding(
                        "INVALID_RAIL_ID",
                        f"Rail ID {rail_id} must be non-negative",
                        "node",
                        node.id,
                    )
                )
            if count > 1:
                findings.append(
                    _finding(
                        "DUPLICATE_RAIL_ID",
                        f"Rail ID {rail_id} is assigned {count} times",
                        "node",
                        node.id,
                    )
                )

        if node.power_budget_watts is not None and (
            not math.isfinite(node.power_budget_watts)
            or node.power_budget_watts <= 0
        ):
            findings.append(
                _finding(
                    "INVALID_POWER_BUDGET",
                    "Power budget must be a finite positive number",
                    "node",
                    node.id,
                )
            )

    endpoint_types = {
        **{item: "spine" for item in spine_ids},
        **{item: "leaf" for item in leaves_by_id},
        **{item: "node" for item in nodes_by_id},
    }
    graph: dict[str, set[str]] = defaultdict(set)

    for link in topology.links:
        if (
            not math.isfinite(link.capacity_gbps)
            or link.capacity_gbps <= 0
        ):
            findings.append(
                _finding(
                    "INVALID_LINK_CAPACITY",
                    "Link capacity must be a finite positive number",
                    "link",
                    link.id,
                )
            )

        endpoint_a_type = endpoint_types.get(link.endpoint_a)
        endpoint_b_type = endpoint_types.get(link.endpoint_b)
        if endpoint_a_type is None:
            findings.append(
                _finding(
                    "MISSING_LINK_ENDPOINT",
                    f"Link references missing endpoint {link.endpoint_a!r}",
                    "link",
                    link.id,
                )
            )
        if endpoint_b_type is None:
            findings.append(
                _finding(
                    "MISSING_LINK_ENDPOINT",
                    f"Link references missing endpoint {link.endpoint_b!r}",
                    "link",
                    link.id,
                )
            )

        if link.endpoint_a == link.endpoint_b:
            findings.append(
                _finding(
                    "SELF_LOOP_LINK",
                    "Link endpoints must be different entities",
                    "link",
                    link.id,
                )
            )

        expected_types = {
            "spine_leaf": {"spine", "leaf"},
            "leaf_node": {"leaf", "node"},
        }[link.kind.value]
        actual_types = {
            endpoint_a_type,
            endpoint_b_type,
        }
        if (
            endpoint_a_type is not None
            and endpoint_b_type is not None
            and actual_types != expected_types
        ):
            findings.append(
                _finding(
                    "LINK_KIND_MISMATCH",
                    f"Link kind {link.kind.value!r} does not match endpoints "
                    f"{endpoint_a_type}/{endpoint_b_type}",
                    "link",
                    link.id,
                )
            )

        if endpoint_a_type is not None and endpoint_b_type is not None:
            graph[link.endpoint_a].add(link.endpoint_b)
            graph[link.endpoint_b].add(link.endpoint_a)

    active_entities = set(spine_ids) | set(leaves_by_id) | set(nodes_by_id)
    if active_entities:
        start = next(iter(sorted(active_entities)))
        visited = {start}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for neighbor in graph[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        if visited != active_entities:
            missing = len(active_entities - visited)
            findings.append(
                _finding(
                    "TOPOLOGY_DISCONNECTED",
                    f"{missing} fabric entities are not reachable from {start!r}",
                    "topology",
                    topology.id,
                )
            )

    return ValidationReport(findings=_ordered(findings))


def validate_jobs(jobs: tuple[Job, ...], topology: Topology) -> ValidationReport:
    findings: list[ValidationFinding] = []
    job_ids = Counter(job.id for job in jobs)
    for job_id, count in job_ids.items():
        if count > 1:
            findings.append(
                _finding(
                    "DUPLICATE_JOB_ID",
                    f"Job ID {job_id!r} is used {count} times",
                    "job",
                    job_id,
                )
            )

    gpu_count = topology.gpu_count
    for job in jobs:
        if job.requested_gpus <= 0:
            findings.append(
                _finding(
                    "INVALID_JOB_GPU_COUNT",
                    "Requested GPU count must be positive",
                    "job",
                    job.id,
                )
            )
        if job.requested_gpus > gpu_count:
            findings.append(
                _finding(
                    "JOB_EXCEEDS_TOPOLOGY",
                    f"Job requests {job.requested_gpus} GPUs; topology has {gpu_count}",
                    "job",
                    job.id,
                )
            )

    return ValidationReport(findings=_ordered(findings))


def validate_revision(content: RevisionContent) -> ValidationReport:
    topology_report = validate_topology(content.topology)
    job_report = validate_jobs(content.jobs, content.topology)
    return ValidationReport(
        findings=_ordered((*topology_report.findings, *job_report.findings))
    )
