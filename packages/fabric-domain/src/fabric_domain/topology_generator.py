from __future__ import annotations

import math
from dataclasses import dataclass

from fabric_domain.models import Gpu, Leaf, Link, LinkKind, Node, Rack, Spine, Topology


class TopologyGenerationError(ValueError):
    """Raised when topology generation parameters are invalid."""


@dataclass(frozen=True, slots=True)
class TopologyGenerationConfig:
    spine_count: int = 8
    leaf_count: int = 16
    nodes_per_leaf: int = 8
    gpus_per_node: int = 8
    spine_leaf_capacity_gbps: float = 400.0
    leaf_node_capacity_gbps: float = 3200.0
    node_power_budget_watts: float | None = 6000.0
    topology_id: str = "mvp-8x16x8x8"

    def validate(self) -> None:
        integer_fields = {
            "spine_count": self.spine_count,
            "leaf_count": self.leaf_count,
            "nodes_per_leaf": self.nodes_per_leaf,
            "gpus_per_node": self.gpus_per_node,
        }
        for name, value in integer_fields.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise TopologyGenerationError(f"{name} must be a positive integer")

        capacities = {
            "spine_leaf_capacity_gbps": self.spine_leaf_capacity_gbps,
            "leaf_node_capacity_gbps": self.leaf_node_capacity_gbps,
        }
        for name, value in capacities.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise TopologyGenerationError(f"{name} must be a finite positive number")

        if self.node_power_budget_watts is not None:
            value = self.node_power_budget_watts
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise TopologyGenerationError(
                    "node_power_budget_watts must be a finite positive number or null"
                )

        if not isinstance(self.topology_id, str) or not self.topology_id:
            raise TopologyGenerationError("topology_id must be a non-empty string")


def generate_topology(config: TopologyGenerationConfig | None = None) -> Topology:
    """Generate a deterministic full-bisection spine/leaf topology."""

    effective = config or TopologyGenerationConfig()
    effective.validate()

    spines = tuple(
        Spine(id=f"spine-{spine_index:02d}")
        for spine_index in range(effective.spine_count)
    )
    racks = tuple(
        Rack(id=f"rack-{leaf_index:02d}")
        for leaf_index in range(effective.leaf_count)
    )
    leaves = tuple(
        Leaf(id=f"leaf-{leaf_index:02d}", rack_id=f"rack-{leaf_index:02d}")
        for leaf_index in range(effective.leaf_count)
    )

    nodes: list[Node] = []
    for leaf_index in range(effective.leaf_count):
        leaf_id = f"leaf-{leaf_index:02d}"
        rack_id = f"rack-{leaf_index:02d}"
        for node_index in range(effective.nodes_per_leaf):
            node_id = f"node-{leaf_index:02d}-{node_index:02d}"
            gpus = tuple(
                Gpu(
                    id=f"{node_id}-gpu-{gpu_index}",
                    slot=gpu_index,
                    rail_id=gpu_index,
                )
                for gpu_index in range(effective.gpus_per_node)
            )
            nodes.append(
                Node(
                    id=node_id,
                    rack_id=rack_id,
                    leaf_id=leaf_id,
                    gpus=gpus,
                    power_budget_watts=effective.node_power_budget_watts,
                )
            )

    links: list[Link] = []
    for spine in spines:
        for leaf in leaves:
            links.append(
                Link(
                    id=f"{spine.id}--{leaf.id}",
                    kind=LinkKind.SPINE_LEAF,
                    endpoint_a=spine.id,
                    endpoint_b=leaf.id,
                    capacity_gbps=float(effective.spine_leaf_capacity_gbps),
                )
            )

    for node in nodes:
        links.append(
            Link(
                id=f"{node.leaf_id}--{node.id}",
                kind=LinkKind.LEAF_NODE,
                endpoint_a=node.leaf_id,
                endpoint_b=node.id,
                capacity_gbps=float(effective.leaf_node_capacity_gbps),
            )
        )

    return Topology(
        id=effective.topology_id,
        spines=spines,
        racks=racks,
        leaves=leaves,
        nodes=tuple(nodes),
        links=tuple(links),
    )
