# Case 2 TP baseline run: 2026-11-03

This directory contains a fresh CPU-only run of the Qwen2.5-7B tensor-parallel
simulation. All performance inputs are `SYNTHETIC`; GPU measurement status is
`NOT_RUN`.

## Commands

```powershell
.\.venv\Scripts\python.exe scripts\run_v2_experiments.py `
  --cases E0_analytic T1_tp1 T1_tp2 T1_tp4 `
          T2_pair_control T2_A_alone T2_B_alone `
          T2_kv_reject T3_fault `
  --output evidence\team_1103_tp

.\.venv\Scripts\python.exe ..\..\..\一日分工\cpu_followups.py `
  --project . --case tp `
  --output evidence\team_1103_tp_extra
```

## Main results

| Case | Result |
|---|---|
| `E0_analytic` | PASS; symbolic 8.05306368 ms remains the analytic reference |
| `T1_tp1` | 2/2 requests completed; mean TTFT 45.800 ms; end 110.400 ms |
| `T1_tp2` | 2/2 requests completed; mean TTFT 34.003 ms; end 79.695 ms |
| `T1_tp4` | 2/2 requests completed; mean TTFT 39.636 ms; end 95.815 ms |
| `T2_A_alone` | 1 request; TTFT 36.061 ms; latency 66.974 ms |
| `T2_pair_control` | 2 requests on two replicas; mean TTFT 44.282 ms; end 75.387 ms |
| `T2_kv_reject` | 1 request completed and 1 rejected for `kv_or_model_memory_exceeded` |
| `T3_fault` | 3 requests completed and 1 failed on `node_down:n0`; 1 stale callback ignored |

The TP sizes use a configured synthetic compute profile. The observed ordering
TP2 < TP4 < TP1 in this scenario is not evidence that TP2 will be fastest on
real Qwen hardware. TP4 adds network work while reducing per-GPU model state,
and the result depends on the assigned computation and communication inputs.

The two-replica pair has higher latency than one replica alone because both
requests share the configured rack uplink. The comparison keeps placement and
request work fixed; it does not include independent repeated samples or a
confidence interval.

## Sensitivity run

At 25, 50, and 100 Gbps, mean TTFT was 60.723, 44.282, and 36.061 ms. With
2, 4, and 8 requests, all requests completed and mean TTFT was 44.282, 81.975,
and 157.362 ms. Processed events and result size also increased with request
count.

These observations show event/workload growth and bandwidth sensitivity. They
are not GPU-count scaling measurements, and single wall-clock observations do
not establish statistical significance.

## Web acceptance

`http_acceptance.json` contains 15 passing HTTP checks for `tp:competition` and
`tp:kv_reject`. `browser_tp_acceptance.json` records the actual Edge run:

- 14 scenarios loaded.
- TP competition completed.
- 12 summary metrics, 4 request rows, and 6 invariant checks were displayed.
- Timeline and resource chart were rendered.
- The read-only explainer cited the saved result and did not mutate it.

The browser screenshot is `browser_tp_acceptance.png`.

## Limits

- No Qwen weights were loaded and no real inference was executed.
- TTFT, token intervals, model compute time, and network parameters are
  synthetic model inputs.
- Each replica handles one request at a time. Continuous batching,
  PagedAttention, KV migration, and exact vLLM behavior are not implemented.
- The service exposes a completed simulation record; individual requests may
  still be rejected or failed.
