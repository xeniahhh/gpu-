from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from collections.abc import Callable
from typing import Mapping, Sequence, TypeVar


SUPPORTED_SCHEMA_VERSION = 1
ItemT = TypeVar("ItemT")


class ContractError(ValueError):
    """Raised when a contract payload cannot be represented by the domain model."""


class LinkKind(StrEnum):
    SPINE_LEAF = "spine_leaf"
    LEAF_NODE = "leaf_node"


class CommunicationMode(StrEnum):
    RING_ALL_REDUCE = "ring_all_reduce"
    ALL_TO_ALL = "all_to_all"


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must be a JSON object")
    return value


def _list(value: object, path: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ContractError(f"{path} must be a JSON array")
    return value


def _string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{path} must be a non-empty string")
    return value


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{path} must be an integer")
    return value


def _number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{path} must be a number")
    return float(value)


def _required(data: Mapping[str, object], key: str, path: str) -> object:
    if key not in data:
        raise ContractError(f"{path}.{key} is required")
    return data[key]


@dataclass(frozen=True, slots=True)
class Spine:
    id: str

    @classmethod
    def from_dict(cls, value: object, path: str = "spine") -> Spine:
        data = _mapping(value, path)
        return cls(id=_string(_required(data, "id", path), f"{path}.id"))

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id}


@dataclass(frozen=True, slots=True)
class Rack:
    id: str

    @classmethod
    def from_dict(cls, value: object, path: str = "rack") -> Rack:
        data = _mapping(value, path)
        return cls(id=_string(_required(data, "id", path), f"{path}.id"))

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id}


@dataclass(frozen=True, slots=True)
class Leaf:
    id: str
    rack_id: str

    @classmethod
    def from_dict(cls, value: object, path: str = "leaf") -> Leaf:
        data = _mapping(value, path)
        return cls(
            id=_string(_required(data, "id", path), f"{path}.id"),
            rack_id=_string(_required(data, "rack_id", path), f"{path}.rack_id"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "rack_id": self.rack_id}


@dataclass(frozen=True, slots=True)
class Gpu:
    id: str
    slot: int
    rail_id: int

    @classmethod
    def from_dict(cls, value: object, path: str = "gpu") -> Gpu:
        data = _mapping(value, path)
        return cls(
            id=_string(_required(data, "id", path), f"{path}.id"),
            slot=_integer(_required(data, "slot", path), f"{path}.slot"),
            rail_id=_integer(_required(data, "rail_id", path), f"{path}.rail_id"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "slot": self.slot, "rail_id": self.rail_id}


@dataclass(frozen=True, slots=True)
class Node:
    id: str
    rack_id: str
    leaf_id: str
    gpus: tuple[Gpu, ...]
    power_budget_watts: float | None = None

    @classmethod
    def from_dict(cls, value: object, path: str = "node") -> Node:
        data = _mapping(value, path)
        gpu_values = _list(_required(data, "gpus", path), f"{path}.gpus")
        power_value = data.get("power_budget_watts")
        power_budget = (
            None if power_value is None else _number(power_value, f"{path}.power_budget_watts")
        )
        return cls(
            id=_string(_required(data, "id", path), f"{path}.id"),
            rack_id=_string(_required(data, "rack_id", path), f"{path}.rack_id"),
            leaf_id=_string(_required(data, "leaf_id", path), f"{path}.leaf_id"),
            gpus=tuple(
                Gpu.from_dict(gpu, f"{path}.gpus[{index}]")
                for index, gpu in enumerate(gpu_values)
            ),
            power_budget_watts=power_budget,
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.id,
            "rack_id": self.rack_id,
            "leaf_id": self.leaf_id,
            "gpus": [gpu.to_dict() for gpu in self.gpus],
        }
        if self.power_budget_watts is not None:
            result["power_budget_watts"] = self.power_budget_watts
        return result


@dataclass(frozen=True, slots=True)
class Link:
    id: str
    kind: LinkKind
    endpoint_a: str
    endpoint_b: str
    capacity_gbps: float

    @classmethod
    def from_dict(cls, value: object, path: str = "link") -> Link:
        data = _mapping(value, path)
        kind_value = _string(_required(data, "kind", path), f"{path}.kind")
        try:
            kind = LinkKind(kind_value)
        except ValueError as exc:
            raise ContractError(f"{path}.kind has unsupported value: {kind_value}") from exc
        return cls(
            id=_string(_required(data, "id", path), f"{path}.id"),
            kind=kind,
            endpoint_a=_string(
                _required(data, "endpoint_a", path),
                f"{path}.endpoint_a",
            ),
            endpoint_b=_string(
                _required(data, "endpoint_b", path),
                f"{path}.endpoint_b",
            ),
            capacity_gbps=_number(
                _required(data, "capacity_gbps", path),
                f"{path}.capacity_gbps",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "endpoint_a": self.endpoint_a,
            "endpoint_b": self.endpoint_b,
            "capacity_gbps": self.capacity_gbps,
        }


@dataclass(frozen=True, slots=True)
class Topology:
    id: str
    spines: tuple[Spine, ...]
    racks: tuple[Rack, ...]
    leaves: tuple[Leaf, ...]
    nodes: tuple[Node, ...]
    links: tuple[Link, ...]

    @property
    def gpu_count(self) -> int:
        return sum(len(node.gpus) for node in self.nodes)

    @classmethod
    def from_dict(cls, value: object, path: str = "topology") -> Topology:
        data = _mapping(value, path)

        def parse_items(
            key: str,
            parser: Callable[[object, str], ItemT],
        ) -> tuple[ItemT, ...]:
            items = _list(_required(data, key, path), f"{path}.{key}")
            return tuple(
                parser(item, f"{path}.{key}[{index}]")
                for index, item in enumerate(items)
            )

        return cls(
            id=_string(_required(data, "id", path), f"{path}.id"),
            spines=parse_items("spines", Spine.from_dict),
            racks=parse_items("racks", Rack.from_dict),
            leaves=parse_items("leaves", Leaf.from_dict),
            nodes=parse_items("nodes", Node.from_dict),
            links=parse_items("links", Link.from_dict),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "spines": [spine.to_dict() for spine in self.spines],
            "racks": [rack.to_dict() for rack in self.racks],
            "leaves": [leaf.to_dict() for leaf in self.leaves],
            "nodes": [node.to_dict() for node in self.nodes],
            "links": [link.to_dict() for link in self.links],
        }


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    requested_gpus: int
    communication_mode: CommunicationMode
    priority: int

    @classmethod
    def from_dict(cls, value: object, path: str = "job") -> Job:
        data = _mapping(value, path)
        mode_value = _string(
            _required(data, "communication_mode", path),
            f"{path}.communication_mode",
        )
        try:
            mode = CommunicationMode(mode_value)
        except ValueError as exc:
            raise ContractError(
                f"{path}.communication_mode has unsupported value: {mode_value}"
            ) from exc
        return cls(
            id=_string(_required(data, "id", path), f"{path}.id"),
            requested_gpus=_integer(
                _required(data, "requested_gpus", path),
                f"{path}.requested_gpus",
            ),
            communication_mode=mode,
            priority=_integer(_required(data, "priority", path), f"{path}.priority"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "requested_gpus": self.requested_gpus,
            "communication_mode": self.communication_mode.value,
            "priority": self.priority,
        }


@dataclass(frozen=True, slots=True)
class RevisionContent:
    schema_version: int
    revision_id: str
    topology: Topology
    jobs: tuple[Job, ...]

    @classmethod
    def from_dict(cls, value: object, path: str = "revision") -> RevisionContent:
        data = _mapping(value, path)
        schema_version = _integer(
            _required(data, "schema_version", path),
            f"{path}.schema_version",
        )
        if schema_version != SUPPORTED_SCHEMA_VERSION:
            raise ContractError(
                f"{path}.schema_version must be {SUPPORTED_SCHEMA_VERSION}; "
                f"received {schema_version}"
            )
        job_values = _list(_required(data, "jobs", path), f"{path}.jobs")
        return cls(
            schema_version=schema_version,
            revision_id=_string(
                _required(data, "revision_id", path),
                f"{path}.revision_id",
            ),
            topology=Topology.from_dict(
                _required(data, "topology", path),
                f"{path}.topology",
            ),
            jobs=tuple(
                Job.from_dict(job, f"{path}.jobs[{index}]")
                for index, job in enumerate(job_values)
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "revision_id": self.revision_id,
            "topology": self.topology.to_dict(),
            "jobs": [job.to_dict() for job in self.jobs],
        }
