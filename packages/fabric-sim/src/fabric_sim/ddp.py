"""Fixed-world-size training scenarios with resource and checkpoint semantics.

This is a flow-level, single-bucket, non-overlapped model. Timings in the demo
are SYNTHETIC, not measured GPT-2/RTX3090 performance.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
import math
from typing import Any, Callable

from .core import Engine, Network, ring_allreduce


DEFAULT_CONFIG = {"n_layer": 12, "n_embd": 768, "n_head": 12,
                  "vocab_size": 50257, "n_positions": 1024}


def _number(value: Any, name: str, minimum: float = 0, maximum: float = 60000) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def training_profile(raw: dict | None = None) -> dict:
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("profile must be an object")
    raw = deepcopy(raw or {})
    if raw.get("dtype", "float32") != "float32":
        raise ValueError("DDP v2 supports float32 parameters and gradients only")
    if raw.get("optimizer", "AdamW") != "AdamW":
        raise ValueError("DDP v2 supports AdamW optimizer state accounting only")
    config = dict(DEFAULT_CONFIG)
    if raw.get("model_config"):
        if not isinstance(raw["model_config"], dict):
            raise ValueError("model_config must be an object")
        for key in raw["model_config"]:
            if key not in config:
                raise ValueError(f"Unsupported GPT-2 config field: {key}")
        config.update(raw["model_config"])
    for k, v in config.items():
        _integer(v, k, 1, 200000 if k == "vocab_size" else 16384)
    if config["n_embd"] % config["n_head"]:
        raise ValueError("hidden size must be divisible by attention heads")
    h = config["n_embd"]
    p = config["vocab_size"] * h + config["n_positions"] * h + config["n_layer"] * (12*h*h + 13*h) + 2*h
    gradient = p * 4
    if "gradient_bytes" in raw and raw["gradient_bytes"] != gradient:
        raise ValueError("gradient_bytes contradicts the FP32 tied-embedding GPT-2 model")
    if "parameter_count" in raw and raw["parameter_count"] != p:
        raise ValueError("parameter_count contradicts the GPT-2 model configuration")
    source = raw.get("source", "SYNTHETIC")
    if source not in {"SYNTHETIC", "MEASURED"}:
        raise ValueError("Timing profile source must be SYNTHETIC or MEASURED")
    if source == "MEASURED" and not raw.get("measurement_id"):
        raise ValueError("MEASURED timing profiles require measurement_id")
    measured_fields = {"compute_ms", "optimizer_ms", "peak_memory_gib"}
    if source == "MEASURED" and not measured_fields.issubset(raw):
        raise ValueError("MEASURED profiles must explicitly provide compute_ms, optimizer_ms and peak_memory_gib")
    defaults = {"compute_ms": 100, "optimizer_ms": 5, "peak_memory_gib": 4,
                "checkpoint_save_ms": 200, "restore_ms": 300, "group_init_ms": 100}
    profile = {k: _number(raw.get(k, v), k, 0, 1024 if k == "peak_memory_gib" else 60000)
               for k, v in defaults.items()}
    field_sources = {k: source if k in raw else "SYNTHETIC" for k in defaults}
    if profile["compute_ms"] + profile["optimizer_ms"] <= 0:
        raise ValueError("compute_ms + optimizer_ms must be positive")
    if profile["peak_memory_gib"] * 2**30 < 16 * p:
        raise ValueError("peak_memory_gib is below FP32 parameters/gradients/Adam states alone")
    seq = _integer(raw.get("sequence_length", 512), "sequence_length", 1, config["n_positions"])
    batch = _integer(raw.get("local_batch_size", 1), "local_batch_size", 1, 4096)
    profile.update({"source": source, "measurement_id": raw.get("measurement_id"),
                    "source_scope": "Per-field provenance is authoritative; default recovery costs are synthetic.",
                    "field_sources": field_sources,
                    "model_id": "gpt2-small" if config == DEFAULT_CONFIG else "gpt2-configured",
                    "model_config": config, "parameter_count": p, "gradient_bytes": gradient,
                    "gradient_source": "DERIVED", "dtype": "float32", "optimizer": "AdamW",
                    "sequence_length": seq, "local_batch_size": batch,
                    "base_training_storage_bytes": 16*p,
                    "memory_note": "Peak includes a stated profile allowance; base parameter storage is not peak memory."})
    return profile


@dataclass
class _Job:
    id: str
    world_size: int
    steps: int
    arrival_ms: float
    priority: int
    checkpoint_interval: int
    explicit: tuple[str, ...] = ()
    compute_factor: float = 1
    state: str = "pending"
    state_since: float = 0
    progress: int = 0
    checkpoint: int = 0
    epoch: int = 0
    allocation: tuple[str, ...] = ()
    arrival_seen: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    latest_failure: float | None = None
    recovery_started_at: float | None = None
    recovery_pending: bool = False
    recovery_ms: float = 0
    restart_count: int = 0
    lost_steps: int = 0
    attempted_steps: int = 0
    ignored_events: int = 0
    last_reason: str = ""
    durations: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    timeline: list[dict] = field(default_factory=list)
    allocations: list[dict] = field(default_factory=list)
    recoveries: list[dict] = field(default_factory=list)


class _Training:
    def __init__(self, spec: dict):
        self.profile = training_profile(spec.get("profile"))
        self.network = Network(spec["topology"])
        self.engine = Engine(self.network)
        self.horizon = _number(spec.get("horizon_ms", 20000), "horizon_ms", 0.001, 60000)
        self.policy = spec.get("placement_policy", "compact")
        if self.policy not in {"compact", "spread", "first_fit"}:
            raise ValueError("placement_policy must be compact, spread or first_fit")
        self.jobs: list[_Job] = []
        rawjobs = spec.get("jobs", [])
        if not isinstance(rawjobs, list) or not 1 <= len(rawjobs) <= 12:
            raise ValueError("DDP requires between 1 and 12 jobs")
        seen: set[str] = set()
        for raw in rawjobs:
            if not isinstance(raw, dict):
                raise ValueError("Each job must be an object")
            jid = raw.get("id")
            if not isinstance(jid, str) or not jid or len(jid) > 100 or jid in seen:
                raise ValueError("Each job needs a unique non-empty id of at most 100 characters")
            seen.add(jid)
            world = _integer(raw.get("world_size", 4), "world_size", 1, 8)
            if world not in {1, 2, 4, 8}:
                raise ValueError("DDP v2 world_size must be 1, 2, 4 or 8")
            if self.profile["gradient_bytes"] % world:
                raise ValueError("This equal-chunk Ring model requires gradient bytes divisible by world_size")
            explicit = raw.get("gpu_ids", [])
            if not isinstance(explicit, list) or any(not isinstance(g, str) for g in explicit):
                raise ValueError("gpu_ids must be a list of GPU IDs")
            if explicit and (len(explicit) != world or len(set(explicit)) != world
                             or any(g not in self.network.gpus for g in explicit)):
                raise ValueError("Explicit gpu_ids must identify exactly world_size distinct known GPUs")
            factors = raw.get("rank_compute_factors", [1])
            if not isinstance(factors, list) or len(factors) not in {1, world}:
                raise ValueError("rank_compute_factors must have length 1 or world_size")
            factor = max(_number(x, "rank_compute_factor", .01, 100) for x in factors)
            self.jobs.append(_Job(jid, world,
                _integer(raw.get("steps", 30), "steps", 1, 200),
                _number(raw.get("arrival_ms", 0), "arrival_ms"),
                _integer(raw.get("priority", 0), "priority", -1000, 1000),
                _integer(raw.get("checkpoint_interval", 10), "checkpoint_interval", 0, 200),
                tuple(explicit), factor))
        self.owners: dict[str, str] = {}
        self.counts: Counter = Counter()
        self.violations: list[str] = []
        self.faults = spec.get("faults", [])
        if not isinstance(self.faults, list) or len(self.faults) > 64:
            raise ValueError("faults must be a list of at most 64 events")
        node_ids = {g.node_id for g in self.network.gpus.values()}
        for fault in self.faults:
            if not isinstance(fault, dict):
                raise ValueError("Each fault must be an object")
            _number(fault.get("at_ms"), "fault.at_ms")
            typ = fault.get("type")
            target = fault.get("target_id")
            if typ not in {"node_down", "node_up", "resource_down", "resource_up"}:
                raise ValueError("Unknown fault type")
            if target not in (node_ids if typ.startswith("node_") else self.network.capacities):
                raise ValueError(f"Unknown fault target {target}")

    def owner(self, job: _Job) -> str:
        return f"ddp:{job.id}:{job.epoch}"

    def state(self, job: _Job, new: str, reason: str) -> None:
        now = self.engine.now_ms
        old = job.state
        if new == old:
            if reason != job.last_reason:
                self.engine.record("waiting_reason", subject_id=job.id, state=new, message=reason,
                                   epoch=job.epoch)
            job.last_reason = reason
            return
        if old != "pending":
            job.durations[old] += now - job.state_since
            job.timeline.append({"state": old, "start_ms": job.state_since, "end_ms": now,
                                 "epoch": job.epoch})
        job.state, job.state_since, job.last_reason = new, now, reason
        self.engine.record("job_state", subject_id=job.id, before=old, state=new,
                           epoch=job.epoch, step=job.progress, message=reason)

    def check(self) -> None:
        self.counts["state_checks"] += 1
        assigned = [g for j in self.jobs for g in j.allocation]
        if len(assigned) != len(set(assigned)) or len(assigned) != len(self.owners):
            self.violations.append("GPU allocation is not exclusive")
        for job in self.jobs:
            if job.allocation:
                if len(job.allocation) != job.world_size:
                    self.violations.append(f"{job.id}: partial world size")
                if any(self.network.gpus[g].memory_gib < self.profile["peak_memory_gib"] for g in job.allocation):
                    self.violations.append(f"{job.id}: memory exceeded")
                if any(self.owners.get(g) != job.id for g in job.allocation):
                    self.violations.append(f"{job.id}: owner mismatch")
            if not 0 <= job.checkpoint <= job.progress <= job.steps:
                self.violations.append(f"{job.id}: checkpoint or progress invalid")
        if self.violations:
            raise AssertionError(self.violations[-1])

    def guarded(self, job: _Job, callback: Callable[[], None]) -> Callable[[], None]:
        epoch = job.epoch
        def execute() -> None:
            if epoch != job.epoch or not job.allocation:
                job.ignored_events += 1
                self.counts["ignored_old_events"] += 1
                self.engine.record("ignored_old_event", subject_id=job.id, event_epoch=epoch,
                                   epoch=job.epoch, message="故障前的事件不得推进新一轮执行")
                return
            callback()
            self.check()
        return execute

    def connected(self, allocation: tuple[str, ...]) -> bool:
        if any(self.network.gpus[g].node_id in self.network.down_nodes for g in allocation):
            return False
        if len(allocation) == 1:
            return True
        for i, g in enumerate(allocation):
            try:
                path = self.network.path(g, allocation[(i+1) % len(allocation)])
            except ValueError:
                return False
            if any(self.network.capacities[r] <= 0 for r in path):
                return False
        return True

    def candidates(self, job: _Job) -> tuple[str, ...]:
        free = [g for g in self.network.available_gpu_ids()
                if g not in self.owners and self.network.gpus[g].memory_gib >= self.profile["peak_memory_gib"]]
        free.sort()
        if job.explicit and job.restart_count == 0:
            allocation = job.explicit if all(g in free for g in job.explicit) else ()
            return allocation if allocation and self.connected(allocation) else ()
        if len(free) < job.world_size:
            return ()
        if self.policy == "spread":
            by_node: dict[str, list[str]] = defaultdict(list)
            for gpu in free:
                by_node[self.network.gpus[gpu].node_id].append(gpu)
            ordered = []
            while any(by_node.values()):
                for node in sorted(by_node):
                    if by_node[node]:
                        ordered.append(by_node[node].pop(0))
            free = ordered
        else:
            free.sort(key=lambda g: (self.network.gpus[g].rack_id, self.network.gpus[g].node_id, g))
        # A bounded candidate search admits around a failed uplink rather than
        # repeatedly choosing the same disconnected ring. No future knowledge.
        options = [tuple(free[:job.world_size])]
        for rack in sorted({self.network.gpus[g].rack_id for g in free}):
            local = [g for g in free if self.network.gpus[g].rack_id == rack]
            if len(local) >= job.world_size:
                options.append(tuple(local[:job.world_size]))
        for offset in range(1, len(free)-job.world_size+1):
            options.append(tuple(free[offset:offset+job.world_size]))
        return next((x for x in options if self.connected(x)), ())

    def dispatch(self) -> None:
        waiting = sorted((j for j in self.jobs if j.state == "waiting"),
                         key=lambda j: (-j.priority, j.arrival_ms, j.id))
        eligible_count = sum(g.memory_gib >= self.profile["peak_memory_gib"] for g in self.network.gpus.values())
        for job in waiting:
            if eligible_count < job.world_size:
                self.state(job, "unschedulable", "单卡显存规格或集群总卡数无法满足固定通信组")
                job.finished_at = self.engine.now_ms
                self.counts["permanent_rejections"] += 1
                continue
            if (job.explicit and job.restart_count == 0 and
                    any(self.network.gpus[g].memory_gib < self.profile["peak_memory_gib"] for g in job.explicit)):
                self.state(job, "unschedulable", "指定初始 GPU 的单卡显存不足，固定放置计划不可行")
                job.finished_at = self.engine.now_ms
                self.counts["permanent_rejections"] += 1
                continue
            allocation = self.candidates(job)
            if not allocation:
                free = sum(g not in self.owners and self.network.gpus[g].memory_gib >= self.profile["peak_memory_gib"]
                           for g in self.network.available_gpu_ids())
                self.state(job, "waiting", f"需要 {job.world_size} 张可达且显存合格的 GPU，当前空闲 {free} 张；不部分占卡")
                self.counts["resource_wait_checks"] += 1
                break  # Explicit strict queue, no silent backfill or preemption.
            job.allocation = allocation
            for g in allocation:
                self.owners[g] = job.id
            job.allocations.append({"time_ms": self.engine.now_ms, "epoch": job.epoch,
                                    "gpu_ids": list(allocation)})
            self.engine.record("allocation", subject_id=job.id, epoch=job.epoch,
                               gpu_ids=list(allocation), world_size=job.world_size)
            self.counts["atomic_admissions"] += 1
            if job.started_at is None:
                job.started_at = self.engine.now_ms
            if job.recovery_pending:
                self.state(job, "restoring", "完整通信组已重新获得资源，加载检查点并重建通信组")
                delay = self.profile["restore_ms"] + self.profile["group_init_ms"]
                def restored(j=job) -> None:
                    recovery = self.engine.now_ms - (j.latest_failure if j.latest_failure is not None else self.engine.now_ms)
                    # A second failure while restoring extends the same outage.
                    # Counting only from latest_failure would lose earlier wait
                    # and failed restoration time from the job-level metric.
                    episode_start = j.recovery_started_at if j.recovery_started_at is not None else self.engine.now_ms
                    episode_duration = self.engine.now_ms - episode_start
                    j.recovery_ms += episode_duration
                    j.recoveries[-1].update({"resume_ms": self.engine.now_ms, "recovery_ms": recovery,
                                            "recovery_episode_start_ms": episode_start,
                                            "recovery_episode_ms": episode_duration})
                    j.recovery_pending = False
                    j.recovery_started_at = None
                    self.compute(j)
                self.engine.after(delay, self.guarded(job, restored))
            else:
                self.compute(job)
            self.check()

    def compute(self, job: _Job) -> None:
        self.state(job, "computing", "各 rank 前向与反向计算；等待最慢 rank 的梯度")
        duration = self.profile["compute_ms"] * job.compute_factor
        self.engine.after(duration, self.guarded(job, lambda: self.synchronize(job)))

    def synchronize(self, job: _Job) -> None:
        if not self.connected(job.allocation):
            self.abort(job, "通信路径不可达")
            self.dispatch()
            return
        self.state(job, "syncing", "先 ReduceScatter，再 AllGather；本轮结束才进入下一轮")
        self.counts["collectives_started"] += 1
        ring_allreduce(self.engine, self.owner(job), list(job.allocation), self.profile["gradient_bytes"],
                       self.guarded(job, lambda: self.update(job)),
                       meta={"job_id": job.id, "epoch": job.epoch, "step": job.progress})

    def update(self, job: _Job) -> None:
        if job.state != "syncing":
            self.violations.append("Parameter update began before synchronization")
            raise AssertionError(self.violations[-1])
        self.counts["collectives_completed"] += 1
        self.state(job, "updating", "全组梯度同步完成，执行优化器更新")
        self.engine.after(self.profile["optimizer_ms"], self.guarded(job, lambda: self.step_complete(job)))

    def step_complete(self, job: _Job) -> None:
        job.progress += 1
        job.attempted_steps += 1
        self.engine.record("step_completed", subject_id=job.id, epoch=job.epoch,
                           step=job.progress, checkpoint_step=job.checkpoint)
        if job.progress == job.steps:
            self.state(job, "completed", "完成目标逻辑训练步数")
            job.finished_at = self.engine.now_ms
            self.release(job)
            self.dispatch()
        elif job.checkpoint_interval and job.progress % job.checkpoint_interval == 0:
            saved_step = job.progress
            self.state(job, "checkpointing", "保存尚未完成；此时故障仍须使用上一份检查点")
            def saved() -> None:
                job.checkpoint = saved_step
                self.counts["durable_checkpoints"] += 1
                self.engine.record("checkpoint_committed", subject_id=job.id, epoch=job.epoch,
                                   step=saved_step, message="检查点保存完成，可用于恢复")
                self.compute(job)
            self.engine.after(self.profile["checkpoint_save_ms"], self.guarded(job, saved))
        else:
            self.compute(job)

    def release(self, job: _Job) -> None:
        for g in job.allocation:
            if self.owners.get(g) == job.id:
                del self.owners[g]
        job.allocation = ()

    def abort(self, job: _Job, reason: str) -> None:
        old_epoch = job.epoch
        self.engine.cancel_owner(self.owner(job))
        lost = job.progress - job.checkpoint
        job.lost_steps += lost
        previous_step = job.progress
        self.state(job, "interrupted", reason)
        self.release(job)
        job.progress = job.checkpoint
        job.epoch += 1
        job.restart_count += 1
        if not job.recovery_pending:
            job.recovery_started_at = self.engine.now_ms
        elif job.recoveries:
            job.recoveries[-1]["superseded_at_ms"] = self.engine.now_ms
        job.recovery_pending = True
        job.latest_failure = self.engine.now_ms
        job.recoveries.append({"failure_ms": self.engine.now_ms, "from_step": previous_step,
                               "checkpoint_step": job.checkpoint, "lost_steps": lost,
                               "old_epoch": old_epoch, "new_epoch": job.epoch,
                               "resume_ms": None, "recovery_ms": None})
        self.engine.record("checkpoint_rollback", subject_id=job.id, epoch=job.epoch,
                           from_step=previous_step, checkpoint_step=job.checkpoint,
                           lost_steps=lost, message="保持 world_size 不变；丢弃当前步及未持久化进度")
        self.state(job, "waiting", "整个同步组等待重新准入")
        self.check()

    def fault(self, fault: dict) -> None:
        typ, target = fault["type"], fault["target_id"]
        if typ == "node_down":
            self.network.fail_node(target)
        elif typ == "node_up":
            self.network.repair_node(target)
        elif typ == "resource_down":
            self.network.fail_resource(target)
        else:
            self.network.repair_resource(target)
        self.engine.record("fault", fault_type=typ, target_id=target)
        if typ.endswith("down"):
            for job in self.jobs:
                # Inspect the full planned ring, even if this job is currently
                # computing/checkpointing, not only currently active transfers.
                if job.allocation and not self.connected(job.allocation):
                    self.abort(job, f"{typ}: {target}，训练通信组失效")
        self.dispatch()
        self.check()

    def run(self) -> dict:
        # Admit simultaneous arrivals together so list order cannot override
        # the declared priority queue before the other arrivals are visible.
        arrivals: dict[float, list[_Job]] = defaultdict(list)
        for job in self.jobs:
            arrivals[job.arrival_ms].append(job)
        for when, group in sorted(arrivals.items()):
            def arrived(group=tuple(group)):
                for j in sorted(group, key=lambda j: j.id):
                    j.arrival_seen = True
                    self.state(j, "waiting", "作业到达，等待完整 GPU 通信组")
                self.dispatch()
                self.check()
            self.engine.schedule(when, arrived, priority=1)
        for fault in self.faults:
            self.engine.schedule(fault["at_ms"], lambda f=fault: self.fault(f), priority=-10)
        self.check()
        self.engine.run(self.horizon)
        # No more events can mean resource deadlock, not successful completion.
        # Count waiting through the declared horizon in this censored case.
        unfinished = any(j.state not in {"completed", "unschedulable"} for j in self.jobs)
        end_time = self.horizon if unfinished else self.engine.now_ms
        output = []
        for job in self.jobs:
            durations = dict(job.durations)
            timeline = list(job.timeline)
            if job.state != "pending":
                durations[job.state] = durations.get(job.state, 0) + max(0, end_time-job.state_since)
                if job.state not in {"completed", "unschedulable"}:
                    timeline.append({"state": job.state, "start_ms": job.state_since,
                                     "end_ms": end_time, "epoch": job.epoch})
            total = None if job.finished_at is None else job.finished_at-job.arrival_ms
            running = sum(durations.get(k, 0) for k in ("computing", "syncing", "updating"))
            output.append({"id": job.id, "state": job.state, "reason": job.last_reason,
                "world_size": job.world_size, "target_steps": job.steps,
                "completed_steps": job.progress, "durable_checkpoint_step": job.checkpoint,
                "execution_epoch": job.epoch, "arrival_ms": job.arrival_ms,
                "started_at_ms": job.started_at, "completed_at_ms": job.finished_at,
                "completion_ms": total, "waiting_ms": durations.get("waiting", 0),
                "computing_ms": durations.get("computing", 0), "syncing_ms": durations.get("syncing", 0),
                "updating_ms": durations.get("updating", 0), "checkpointing_ms": durations.get("checkpointing", 0),
                "restoring_ms": durations.get("restoring", 0), "recovery_ms": job.recovery_ms,
                "recovery_pending_ms": (max(0, end_time-job.recovery_started_at)
                                        if job.recovery_pending and job.recovery_started_at is not None else 0),
                "sync_fraction": durations.get("syncing", 0)/running if running else 0,
                "lost_steps": job.lost_steps, "physical_completed_steps": job.attempted_steps,
                "restart_count": job.restart_count, "ignored_old_events": job.ignored_events,
                "gpu_ids": list(job.allocation), "allocation_history": job.allocations,
                "state_timeline": timeline, "recoveries": job.recoveries,
                "censored": job.finished_at is None,
                "censored_at_ms": end_time if job.finished_at is None else None})
        self.check()
        stats = self.engine.stats()
        stats["observation_end_ms"] = end_time
        stats["termination_reason"] = (
            "horizon_reached" if unfinished and self.engine.now_ms >= self.horizon else
            "no_future_events_with_unfinished_jobs" if unfinished else "terminal_jobs")
        capacity = float(stats.get("max_capacity_ratio", 0))
        checks = [
            {"name": "gpu_world_memory_checkpoint", "passed": not self.violations,
             "detail": {"state_checks": self.counts["state_checks"], "violations": self.violations}},
            {"name": "link_capacity", "passed": capacity <= 1+1e-9,
             "detail": {"max_observed_ratio": capacity, "samples": len(self.engine.resource_samples)}},
            {"name": "synchronization_before_update", "passed": self.counts["collectives_completed"] <= self.counts["collectives_started"],
             "detail": {"started": self.counts["collectives_started"], "completed": self.counts["collectives_completed"]}},
            {"name": "completed_jobs_have_target_progress", "passed": all(j.progress == j.steps for j in self.jobs if j.state == "completed"),
             "detail": {"completed_jobs": sum(j.state == "completed" for j in self.jobs)}},
        ]
        completed = [j for j in output if j["state"] == "completed"]
        return {"schema_version": 2, "scenario": "ddp", "profile": self.profile,
            "summary": {"job_count": len(output), "completed_jobs": len(completed),
                        "unfinished_jobs": sum(j["completed_at_ms"] is None for j in output),
                        "unschedulable_jobs": sum(j["state"] == "unschedulable" for j in output),
                        "simulated_ms": end_time,
                        "mean_completion_ms": sum(j["completion_ms"] for j in completed)/len(completed) if completed else None,
                        "lost_steps": sum(j["lost_steps"] for j in output),
                        "restarts": sum(j["restart_count"] for j in output)},
            "jobs": output, "events": self.engine.events,
            "resource_samples": self.engine.resource_samples, "invariant_checks": checks,
            "engine_stats": stats, "observed_checks": dict(self.counts),
            "assumptions": ["单梯度桶、计算与通信不重叠、Ring 轮间统一等待；不模拟 NCCL 内部流水。",
                            "固定 world_size，GPU 独占、非抢占优先队列、队首不回填。",
                            "checkpoint_interval=0 表示不保存新检查点，故障恢复到初始第 0 步。",
                            "故障中止整组，回滚最近完整检查点；检查点保存/加载为显式 profile 成本，不模拟存储网络。",
                            "recovery_ms 累计已结束的连续恢复区间；恢复中再次故障不重置起点，截止时未结束部分记为 recovery_pending_ms。",
                            "SYNTHETIC profile 的时长和显存是设定值；梯度大小由 FP32 模型结构推导。",
                            "节点/链路是合成网络；不能把结果当单机 RTX3090 的实测性能。",
                            "compact 与 first_fit 首版均为确定性节点优先；spread 按节点轮转，不宣称新最优算法。"]}


def simulate_ddp(spec: dict) -> dict:
    """Run a detached scenario; never mutate the caller's specification."""
    return _Training(deepcopy(spec)).run()


def demo_ddp(variant: str = "competition") -> dict:
    topology = {"nodes": [{"id": f"n{i}", "rack_id": f"r{i//2}", "gpu_count": 2, "memory_gib": 24}
                          for i in range(4)], "node_bandwidth_gbps": 100,
                "uplink_bandwidth_gbps": 50, "local_bandwidth_gbps": 200,
                "link_latency_us": 5, "local_latency_us": 2}
    spec = {"schema_version": 2, "scenario": "ddp", "topology": topology,
            "horizon_ms": 20000, "placement_policy": "compact", "profile": {"source": "SYNTHETIC"},
            "jobs": [{"id": "A", "world_size": 4, "steps": 30, "checkpoint_interval": 10,
                      "gpu_ids": ["n0:gpu0", "n0:gpu1", "n1:gpu0", "n1:gpu1"]},
                     {"id": "B", "world_size": 4, "steps": 30, "checkpoint_interval": 10,
                      "gpu_ids": ["n2:gpu0", "n2:gpu1", "n3:gpu0", "n3:gpu1"]}], "faults": []}
    if variant in {"competition", "baseline", "spread"}:
        spec["jobs"][0]["gpu_ids"] = [f"n{i}:gpu0" for i in range(4)]
        spec["jobs"][1]["gpu_ids"] = [f"n{i}:gpu1" for i in range(4)]
        spec["placement_policy"] = "spread"
    elif variant == "compact":
        pass
    elif variant == "admission":
        for job in spec["jobs"]:
            job.pop("gpu_ids")
        spec["jobs"][1]["world_size"] = 2
        spec["jobs"].append({"id": "C", "world_size": 4, "steps": 10, "arrival_ms": 1,
                              "checkpoint_interval": 0})
    elif variant in {"fault_spare", "checkpoint"}:
        spec["jobs"][1].update(world_size=2, gpu_ids=["n2:gpu0", "n2:gpu1"])
        spec["faults"] = [{"at_ms": 3000, "type": "node_down", "target_id": "n0"}]
        if variant == "checkpoint":
            # Place failure inside A's first checkpoint save using exact duration
            # from a successful no-fault run, rather than assume a platform rate.
            healthy = simulate_ddp(spec | {"faults": []})
            checkpoint = next(x for x in healthy["jobs"][0]["state_timeline"] if x["state"] == "checkpointing")
            spec["faults"][0]["at_ms"] = (checkpoint["start_ms"]+checkpoint["end_ms"])/2
    elif variant == "fault_no_spare":
        spec["faults"] = [{"at_ms": 3000, "type": "node_down", "target_id": "n0"}]
    elif variant == "memory_reject":
        for node in topology["nodes"]:
            node["memory_gib"] = 3
    elif variant == "slow_rank":
        spec["jobs"][0]["rank_compute_factors"] = [1, 1, 1, 2]
    else:
        raise ValueError(f"Unknown DDP demo: {variant}")
    spec["demo_variant"] = variant
    return spec
