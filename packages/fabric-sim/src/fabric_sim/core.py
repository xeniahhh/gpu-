"""Deterministic, capacity-bounded flow simulation for the course workload models.

This is an effective-bandwidth model, not a packet simulator or NCCL emulator.
Network rates are bytes/second; the event clock is milliseconds. Local GPU
communication uses a finite shared bus. Inter-node links are full duplex.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import heapq
import math
from typing import Any, Callable


Callback = Callable[[], None]
_EPS_MS = 1e-9
_EPS_BYTES = 1e-7


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


@dataclass(frozen=True, slots=True)
class GPU:
    id: str
    node_id: str
    rack_id: str
    memory_gib: float


class Network:
    """Two-level effective topology, including shared endpoint NIC limits."""

    def __init__(self, topology: dict[str, Any]):
        if not isinstance(topology, dict):
            raise ValueError("topology must be an object")
        self.gpus: dict[str, GPU] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.down_nodes: set[str] = set()
        self.down_resources: set[str] = set()
        self.capacities: dict[str, float] = {}
        self._base_capacities: dict[str, float] = {}
        self.link_latency_us = _number(topology.get("link_latency_us", 5), "link_latency_us")
        self.local_latency_us = _number(topology.get("local_latency_us", 2), "local_latency_us")
        node_rate = _number(topology.get("node_bandwidth_gbps", 100), "node_bandwidth_gbps", positive=True) * 1e9 / 8
        rack_rate = _number(topology.get("uplink_bandwidth_gbps", 50), "uplink_bandwidth_gbps", positive=True) * 1e9 / 8
        local_rate = _number(topology.get("local_bandwidth_gbps", 200), "local_bandwidth_gbps", positive=True) * 1e9 / 8
        nodes = topology.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("topology.nodes must be a nonempty list")
        for node in nodes:
            if not isinstance(node, dict):
                raise ValueError("each topology node must be an object")
            node_id, rack_id = node.get("id"), node.get("rack_id")
            if not isinstance(node_id, str) or not node_id or not isinstance(rack_id, str) or not rack_id:
                raise ValueError("node id and rack_id must be nonempty strings")
            if node_id in self.nodes:
                raise ValueError(f"duplicate node id: {node_id}")
            count = node.get("gpu_count", 2)
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 64:
                raise ValueError("gpu_count must be an integer in [1,64]")
            memory = _number(node.get("memory_gib", 24), "memory_gib", positive=True)
            self.nodes[node_id] = dict(node)
            for i in range(count):
                gpu_id = f"{node_id}:gpu{i}"
                self.gpus[gpu_id] = GPU(gpu_id, node_id, rack_id, memory)
            for direction in ("tx", "rx"):
                self.capacities[f"node:{node_id}:{direction}"] = node_rate
                self.capacities[f"rack:{rack_id}:{direction}"] = rack_rate
            self.capacities[f"node:{node_id}:local"] = local_rate
        if len(self.gpus) > 64:
            raise ValueError("topology supports at most 64 GPUs")
        self._base_capacities = dict(self.capacities)

    def path(self, src_gpu: str, dst_gpu: str) -> tuple[str, ...]:
        try:
            source, target = self.gpus[src_gpu], self.gpus[dst_gpu]
        except KeyError as exc:
            raise ValueError(f"unknown GPU: {exc.args[0]}") from exc
        if src_gpu == dst_gpu:
            return ()
        if source.node_id == target.node_id:
            return (f"node:{source.node_id}:local",)
        result = [f"node:{source.node_id}:tx"]
        if source.rack_id != target.rack_id:
            result.extend((f"rack:{source.rack_id}:tx", f"rack:{target.rack_id}:rx"))
        result.append(f"node:{target.node_id}:rx")
        return tuple(result)

    def latency_ms(self, src_gpu: str, dst_gpu: str) -> float:
        path = self.path(src_gpu, dst_gpu)
        if not path:
            return 0.0
        if len(path) == 1:
            return self.local_latency_us / 1000
        return len(path) * self.link_latency_us / 1000

    def available_gpu_ids(self) -> list[str]:
        return sorted(g.id for g in self.gpus.values() if g.node_id not in self.down_nodes)

    def _refresh(self) -> None:
        node_resources = {
            f"node:{node}:{direction}"
            for node in self.down_nodes for direction in ("tx", "rx", "local")
        }
        for resource, capacity in self._base_capacities.items():
            self.capacities[resource] = 0.0 if resource in self.down_resources or resource in node_resources else capacity

    def fail_node(self, node_id: str) -> None:
        if node_id not in self.nodes:
            raise ValueError(f"unknown node: {node_id}")
        self.down_nodes.add(node_id)
        self._refresh()

    def repair_node(self, node_id: str) -> None:
        if node_id not in self.nodes:
            raise ValueError(f"unknown node: {node_id}")
        self.down_nodes.discard(node_id)
        self._refresh()

    def fail_resource(self, resource_id: str) -> None:
        if resource_id not in self.capacities:
            raise ValueError(f"unknown resource: {resource_id}")
        self.down_resources.add(resource_id)
        self._refresh()

    def repair_resource(self, resource_id: str) -> None:
        if resource_id not in self.capacities:
            raise ValueError(f"unknown resource: {resource_id}")
        self.down_resources.discard(resource_id)
        self._refresh()


@dataclass(slots=True)
class _Flow:
    id: str
    owner_id: str
    generation: int
    src_gpu: str
    dst_gpu: str
    size_bytes: int
    remaining_bytes: float
    path: tuple[str, ...]
    callback: Callback
    meta: dict[str, Any]
    state: str = "pending"
    rate: float = 0.0


class Engine:
    """A single deterministic simulation clock, shared by all active workloads.

    Each active flow receives min(capacity / users) over its resources. This
    conservative sharing is feasible, but is not work-conserving max-min fair
    allocation. Resource samples describe the rate immediately after time_ms.
    """

    def __init__(self, network: Network):
        self.network = network
        self.now_ms = 0.0
        self.events: list[dict[str, Any]] = []
        self.resource_samples: list[dict[str, Any]] = []
        self._queue: list[tuple[float, int, int, Callback]] = []
        self._sequence = 0
        self._flow_sequence = 0
        self._flows: dict[str, _Flow] = {}
        self._owner_generations: Counter[str] = Counter()
        self._processed = 0
        self._total_sent = 0.0
        self._completed_bytes = 0
        self._completed_flows = 0
        self._cancelled_flows = 0
        self._capacity_checks = 0
        self._max_capacity_ratio = 0.0
        self._horizon_reached = False
        self._sample_signature: tuple[Any, ...] | None = None

    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        item = {"time_ms": self.now_ms, "kind": kind, **fields}
        # Callers cannot replace the simulation clock or event kind.
        item["time_ms"], item["kind"] = self.now_ms, kind
        self.events.append(item)
        return item

    def schedule(self, at_ms: float, callback: Callback, priority: int = 0) -> None:
        when = _number(at_ms, "event time")
        if when < self.now_ms - _EPS_MS:
            raise ValueError("cannot schedule an event in the past")
        if not callable(callback):
            raise ValueError("event callback must be callable")
        self._sequence += 1
        heapq.heappush(self._queue, (max(when, self.now_ms), priority, self._sequence, callback))

    def after(self, delay_ms: float, callback: Callback, priority: int = 0) -> None:
        self.schedule(self.now_ms + _number(delay_ms, "event delay"), callback, priority)

    def transfer(self, owner_id: str, src_gpu: str, dst_gpu: str, size_bytes: int,
                 on_complete: Callback, meta: dict[str, Any] | None = None) -> str:
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError("size_bytes must be a nonnegative integer")
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("owner_id must be a nonempty string")
        if not callable(on_complete):
            raise ValueError("on_complete must be callable")
        path = self.network.path(src_gpu, dst_gpu)
        self._flow_sequence += 1
        flow_id = f"flow-{self._flow_sequence}"
        flow = _Flow(flow_id, owner_id, self._owner_generations[owner_id], src_gpu,
                     dst_gpu, size_bytes, float(size_bytes), path, on_complete, dict(meta or {}))
        self._flows[flow_id] = flow

        def begin() -> None:
            if self._flows.get(flow_id) is not flow or flow.generation != self._owner_generations[owner_id]:
                return
            flow.state = "active"
            self.record("transfer_started", subject_id=owner_id, flow_id=flow.id,
                        src_gpu=src_gpu, dst_gpu=dst_gpu, size_bytes=size_bytes,
                        resource_ids=list(path), meta=flow.meta)
            if size_bytes == 0 or not path:
                # A self transfer requires no physical network traffic.
                flow.remaining_bytes = 0.0
                self.schedule(self.now_ms, lambda: self._finish(flow))

        self.after(self.network.latency_ms(src_gpu, dst_gpu), begin)
        return flow_id

    def _finish(self, flow: _Flow) -> None:
        if self._flows.get(flow.id) is not flow or flow.state != "active":
            return
        if flow.generation != self._owner_generations[flow.owner_id]:
            return
        del self._flows[flow.id]
        flow.state = "completed"
        self._completed_flows += 1
        self._completed_bytes += flow.size_bytes if flow.path else 0
        self.record("transfer_completed", subject_id=flow.owner_id, flow_id=flow.id,
                    src_gpu=flow.src_gpu, dst_gpu=flow.dst_gpu, size_bytes=flow.size_bytes,
                    resource_ids=list(flow.path), meta=flow.meta)
        flow.callback()

    def cancel_owner(self, owner_id: str) -> None:
        self._owner_generations[owner_id] += 1
        for flow in list(self._flows.values()):
            if flow.owner_id == owner_id:
                del self._flows[flow.id]
                flow.state = "cancelled"
                self._cancelled_flows += 1
                self.record("transfer_cancelled", subject_id=owner_id, flow_id=flow.id,
                            remaining_bytes=max(0.0, flow.remaining_bytes),
                            resource_ids=list(flow.path), meta=flow.meta)

    def _allocate(self) -> list[_Flow]:
        active = [f for f in self._flows.values() if f.state == "active" and f.remaining_bytes > _EPS_BYTES]
        users: Counter[str] = Counter(r for f in active for r in f.path)
        carried: Counter[str] = Counter()
        for flow in active:
            flow.rate = min((self.network.capacities[r] / users[r] for r in flow.path), default=0.0)
            for resource in flow.path:
                carried[resource] += flow.rate
        signature: list[Any] = []
        for resource in sorted(self.network.capacities):
            cap = self.network.capacities[resource]
            rate = carried[resource]
            self._capacity_checks += 1
            if rate > cap + max(1e-6, cap * 1e-12):
                raise AssertionError(f"capacity violation on {resource}: {rate} > {cap}")
            ratio = rate / cap if cap else 0.0
            self._max_capacity_ratio = max(self._max_capacity_ratio, ratio)
            signature.append((resource, cap, rate, users[resource]))
        signature_tuple = tuple(signature)
        if signature_tuple != self._sample_signature:
            for resource, cap, rate, count in signature:
                self.resource_samples.append({"time_ms": self.now_ms, "resource_id": resource,
                    "capacity_gbps": cap * 8 / 1e9, "allocated_gbps": rate * 8 / 1e9,
                    "active_flows": count})
            self._sample_signature = signature_tuple
        return active

    def run(self, until_ms: float, max_events: int = 200000) -> None:
        horizon = _number(until_ms, "until_ms")
        if horizon < self.now_ms:
            raise ValueError("until_ms cannot precede current time")
        if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events <= 0:
            raise ValueError("max_events must be a positive integer")
        self._horizon_reached = False
        processed_before = self._processed
        while True:
            # Completion events are enqueued before reaching this branch. A
            # failure with priority -10 can cancel them at the identical time.
            while self._queue and self._queue[0][0] <= self.now_ms:
                _, _, _, callback = heapq.heappop(self._queue)
                self._processed += 1
                if self._processed - processed_before > max_events:
                    raise ValueError("simulation event limit exceeded")
                callback()
            active = self._allocate()
            if not self._queue and not self._flows:
                break
            if self.now_ms >= horizon:
                self._horizon_reached = True
                break
            next_event = self._queue[0][0] if self._queue else math.inf
            next_flow = min((self.now_ms + f.remaining_bytes / f.rate * 1000 for f in active if f.rate > 0), default=math.inf)
            next_time = min(next_event, next_flow, horizon)
            if next_time < self.now_ms - _EPS_MS:
                raise AssertionError("simulation time moved backwards")
            delta_s = (next_time - self.now_ms) / 1000
            finishing: list[_Flow] = []
            for flow in active:
                sent = min(flow.remaining_bytes, flow.rate * delta_s)
                flow.remaining_bytes -= sent
                self._total_sent += sent
                if flow.remaining_bytes <= max(_EPS_BYTES, flow.size_bytes * 1e-12):
                    # Floating arithmetic may leave a sub-byte residue.
                    self._total_sent += max(0.0, flow.remaining_bytes)
                    flow.remaining_bytes = 0.0
                    finishing.append(flow)
            self.now_ms = next_time
            for flow in finishing:
                self.schedule(self.now_ms, lambda flow=flow: self._finish(flow))
            if next_time == horizon and not finishing and next_event > horizon:
                self._allocate()
                self._horizon_reached = bool(self._flows or self._queue)
                break

    def stats(self) -> dict[str, Any]:
        return {"processed_events": self._processed, "total_sent_bytes": round(self._total_sent, 6),
                "completed_transfer_bytes": self._completed_bytes,
                "completed_flows": self._completed_flows, "cancelled_flows": self._cancelled_flows,
                "pending_flows": sum(f.state == "pending" for f in self._flows.values()),
                "active_flows": sum(f.state == "active" for f in self._flows.values()),
                "capacity_checks": self._capacity_checks,
                "max_capacity_ratio": self._max_capacity_ratio,
                "horizon_reached": self._horizon_reached,
                "simulated_time_ms": self.now_ms}


def ring_allreduce(engine: Engine, owner_id: str, gpu_ids: list[str] | tuple[str, ...],
                   size_bytes: int, on_complete: Callback,
                   meta: dict[str, Any] | None = None) -> None:
    """Single-bucket ring, two phases and a conservative barrier per round."""
    ranks = tuple(gpu_ids)
    if not ranks or len(set(ranks)) != len(ranks):
        raise ValueError("allreduce requires a nonempty set of distinct GPUs")
    if any(gpu not in engine.network.gpus for gpu in ranks):
        raise ValueError("allreduce references an unknown GPU")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise ValueError("allreduce size_bytes must be a nonnegative integer")
    if size_bytes % len(ranks):
        raise ValueError("allreduce payload must divide evenly across ranks in this model")
    generation = engine._owner_generations[owner_id]
    base_meta = dict(meta or {})
    total_rounds = 2 * (len(ranks) - 1)
    chunk = size_bytes // len(ranks)

    def alive() -> bool:
        return generation == engine._owner_generations[owner_id]

    def round_start(index: int) -> None:
        if not alive():
            return
        if index == total_rounds:
            engine.record("allreduce_completed", subject_id=owner_id, ranks=len(ranks),
                          size_bytes=size_bytes, meta=base_meta)
            on_complete()
            return
        phase = "reduce_scatter" if index < len(ranks) - 1 else "all_gather"
        phase_round = index if phase == "reduce_scatter" else index - (len(ranks) - 1)
        remaining = len(ranks)
        engine.record("collective_round_started", subject_id=owner_id, phase=phase,
                      round_index=phase_round, chunk_bytes=chunk, meta=base_meta)

        def one_done() -> None:
            nonlocal remaining
            if not alive():
                return
            remaining -= 1
            if remaining == 0:
                engine.record("collective_round_completed", subject_id=owner_id,
                              phase=phase, round_index=phase_round, meta=base_meta)
                round_start(index + 1)

        for rank, source in enumerate(ranks):
            engine.transfer(owner_id, source, ranks[(rank + 1) % len(ranks)], chunk,
                            one_done, {**base_meta, "phase": phase, "round_index": phase_round, "rank": rank})

    engine.schedule(engine.now_ms, lambda: round_start(0))
