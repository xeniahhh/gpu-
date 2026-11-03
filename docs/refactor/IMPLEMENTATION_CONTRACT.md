# FabricSandbox v2 implementation contract

This is the integration contract for the current refactor. All numbers marked SYNTHETIC are scenario inputs, not GPU measurements. Keep legacy v1 as a separately labelled static model.

## Ownership（三人协作）

- A（你）：ddp.py、tp.py 及各自状态测试；模型 profile 与故障语义。
- B（组员一）：core.py、资源/通信测试、实验脚本与性能分析。
- C（组员二）：gateway simulation service/router、workload.html、API测试与证据交付。
- validation.py 与 schema：A 主责，涉及接口改动必须由 B/C 在首小时共同确认。
- 每人写自己模块的报告小节；C 排版集成，A 最终核验领域结论。

## Topology (common JSON)

```json
{"nodes":[{"id":"n0","rack_id":"r0","gpu_count":2,"memory_gib":24},{"id":"n1","rack_id":"r0","gpu_count":2,"memory_gib":24},{"id":"n2","rack_id":"r1","gpu_count":2,"memory_gib":24},{"id":"n3","rack_id":"r1","gpu_count":2,"memory_gib":24}],"node_bandwidth_gbps":100,"uplink_bandwidth_gbps":50,"local_bandwidth_gbps":200,"link_latency_us":5,"local_latency_us":2}
```

GPU IDs are `n0:gpu0`. The two-level effective-resource abstraction represents endpoint NIC and rack-uplink contention, not an explicit switch graph. Directed node tx/rx and rack uplink tx/rx resources; no ECMP or alternate-route computation. Same-node transfers consume one shared finite local bus. Cross-node same-rack uses endpoint NICs; cross-rack also consumes both rack uplinks. Effective rates, not packet-level protocol.

## Core public interface (core.py)

- `Network(topology: dict)`; `.gpus: dict[str, GPU]`, GPU has `.id`, `.node_id`, `.rack_id`, `.memory_gib`.
- `.capacities: dict[str,float]` in bytes/s; `.path(src_gpu,dst_gpu)->tuple[str,...]`; `.latency_ms(src_gpu,dst_gpu)->float`.
- `.down_nodes: set[str]`; `.fail_node(node_id)`; `.repair_node(node_id)`; `.fail_resource(resource_id)`; `.repair_resource(resource_id)`.
- `.available_gpu_ids()->list[str]` (healthy, without application allocation filtering).
- `Engine(network)`; `.now_ms`, `.events: list[dict]`, `.resource_samples: list[dict]`.
- `.record(kind: str, **fields)` adds `time_ms` and kind. Application events use `subject_id`, `state`, `message` when appropriate.
- `.schedule(at_ms: float, callback: Callable[[],None], priority: int=0)`; `.after(delay_ms,callback,priority=0)`.
- `.transfer(owner_id,src_gpu,dst_gpu,size_bytes,on_complete,meta=None)`; handles path latency then active transfer; event fields include actual route and bytes.
- `.cancel_owner(owner_id)` cancels active AND not-yet-started transmissions for that owner; no completion callbacks afterward.
- `.run(until_ms,max_events=200000)` progresses sharing flows + callbacks deterministically; never advances past horizon; application must mark unfinished jobs.
- `.stats()->dict` includes processed event count, total sent bytes, max capacity ratio, horizon_reached.
- `ring_allreduce(engine,owner_id,gpu_ids,size_bytes,on_complete,meta=None)` implements two phases, R-1 rounds each; per-round group barrier. R=1 completes without traffic. DDP world size 1/2/4/8 ensures chosen profile bytes divide evenly; TP payload bytes must also divide by R.

Cancel application callbacks by checking epoch captured in each callback. Failure event priority -10 precedes same-time completion.

## Adapter interface

- `ddp.simulate_ddp(spec:dict)->dict`; `ddp.demo_ddp(variant='competition')->dict`.
- `tp.simulate_tp(spec:dict)->dict`; `tp.demo_tp(variant='baseline')->dict`.
- Spec: `schema_version:2`, `scenario:'ddp'|'tp'`, `topology`, `horizon_ms`, `faults:[{at_ms,type:'node_down'|'node_up'|'resource_down'|'resource_up',target_id}]`.
- DDP adds `profile`, `jobs`, `placement_policy:'compact'|'spread'|'first_fit'`. Each job may contain explicit `gpu_ids` for controlled placement.
- TP adds `model`, `replicas`, `requests`, `profile`. One request at a time per replica is an explicit v2 baseline; multiple replicas may execute concurrently and share network. Static worst-case KV admission; no claim to implement continuous batching/vLLM.
- Each adapter returns `schema_version`, `scenario`, `summary`, `events`, `resource_samples`, `invariant_checks`, `assumptions`, `engine_stats`; plus `jobs` or `requests` and `model/profile`.
- `invariant_checks` entries are `{name,passed,detail}` and must reflect observed checks, not blanket hardcoded success. Reject malformed input with ValueError; permanent business infeasibility belongs in result, not server failure.
- Root wrapper `fabric_sim.simulate(spec)` validates bounds, copies input, adds canonical `input_hash` and `engine_wall_ms` (separate from simulated time).

## Limits and fidelity

Small course simulator: max 64 GPUs, 12 jobs, 64 requests, 200 training steps, 256 generated tokens/request, 60,000 ms horizon; externally configurable profiles must be finite nonnegative and schema bounded. No CUDA dependency for simulation. Profiles must identify source DERIVED/SYNTHETIC/MEASURED; never label guessed timings as measurements.

## API integration

GET `/api/v2/scenarios` returns demo list/specs. POST `/api/v2/simulations` body spec, returns 202 `{id,status,input_hash}`. GET `/api/v2/simulations/{id}` returns status/spec/result. GET events and POST `/api/v2/simulations/{id}/explain` read-only explain. Bound concurrent queue; persist snapshots/results with existing SQLite environment location. Root route `/workload` serves standalone HTML view. Existing access middleware must protect all v2 API routes; viewer may read/explain but not submit. Return explicit errors; never accept OS command execution.

`compact` currently uses the same ordered feasible selection as `first_fit`; do not present them as empirically distinct algorithms. Compare explicit compact/spread placements first.
