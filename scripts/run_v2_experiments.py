#!/usr/bin/env python3
"""Run reproducible course simulations and export actual results, not screenshots.

This script never imports CUDA or contacts a GPU server. All charts represent
simulated time; engine_wall_ms is recorded separately.
"""
from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "fabric-sim" / "src"))


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def write_csv(path, rows):
    rows = list(rows)
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns or ["empty"])
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, tuple))
                             else value for key, value in row.items()})


def analytical_ring():
    from fabric_sim.core import Network, Engine, ring_allreduce
    topology = {"nodes": [{"id": f"n{i}", "rack_id": "r0", "gpu_count": 1, "memory_gib": 24}
                           for i in range(4)],
                "node_bandwidth_gbps": 100, "uplink_bandwidth_gbps": 100,
                "local_bandwidth_gbps": 200, "link_latency_us": 0, "local_latency_us": 0}
    network = Network(topology)
    engine = Engine(network)
    complete = []
    payload = 64 * 2**20
    wall = time.perf_counter()
    ring_allreduce(engine, "analytical", sorted(network.gpus), payload,
                   lambda: complete.append(engine.now_ms))
    engine.run(until_ms=1000)
    elapsed = (time.perf_counter() - wall) * 1000
    expected = 6 * (payload / 4) / (100e9 / 8) * 1000
    stats = engine.stats()
    checks = [{"name": "one_completion", "passed": len(complete) == 1, "detail": complete},
              {"name": "analytic_time", "passed": bool(complete) and abs(complete[0] - expected) <= 1e-6,
               "detail": {"expected_ms": expected, "observed_ms": complete[0] if complete else None}},
              {"name": "exact_group_bytes", "passed": abs(stats["total_sent_bytes"] - 6 * payload) < .01,
               "detail": {"expected_bytes": 6 * payload, "observed_bytes": stats["total_sent_bytes"]}},
              {"name": "capacity", "passed": stats["max_capacity_ratio"] <= 1 + 1e-9,
               "detail": stats["max_capacity_ratio"]}]
    spec = {"scenario": "analytical_ring", "topology": topology, "payload_bytes": payload,
            "world_size": 4, "alpha_ms": 0}
    result = {"schema_version": 2, "scenario": "analytical_ring", "summary": checks[1]["detail"],
              "jobs": [{"id": "analytical", "state": "completed" if complete else "incomplete",
                        "completion_ms": complete[0] if complete else None}],
              "events": engine.events, "resource_samples": engine.resource_samples,
              "invariant_checks": checks, "engine_stats": stats, "engine_wall_ms": elapsed,
              "assumptions": ["Analytical calibration fixture, not measured GPU performance."]}
    return spec, result


def available_cases():
    return ["E0_analytic", "D1_compact", "D2_competition", "D2_A_alone", "D2_B_alone",
            "D3_admission", "D3_memory_reject", "D4_fault_spare", "D4_fault_no_spare", "D4_checkpoint_fault",
            "T1_tp1", "T1_tp2", "T1_tp4", "T2_competition", "T2_pair_control", "T2_A_alone", "T2_B_alone", "T2_kv_reject", "T3_fault"]


def scenario_spec(name):
    from fabric_sim.ddp import demo_ddp
    from fabric_sim.tp import demo_tp
    if name in {"T2_pair_control", "T2_A_alone", "T2_B_alone"}:
        spec = demo_tp("competition")
        spec["requests"] = spec["requests"][:2]
        if name != "T2_pair_control":
            selected = 0 if name == "T2_A_alone" else 1
            spec["replicas"] = [spec["replicas"][selected]]
            spec["requests"] = [spec["requests"][selected]]
        return spec
    if name.startswith("T"):
        variant = {"T1_tp1": "tp1", "T1_tp2": "tp2", "T1_tp4": "tp4",
                   "T2_competition": "competition", "T2_kv_reject": "kv_reject", "T3_fault": "fault"}[name]
        return demo_tp(variant)
    variant = {"D1_compact": "compact", "D2_competition": "competition",
               "D2_A_alone": "competition", "D2_B_alone": "competition",
               "D3_admission": "admission", "D3_memory_reject": "memory_reject", "D4_fault_spare": "fault_spare",
               "D4_fault_no_spare": "fault_no_spare", "D4_checkpoint_fault": "checkpoint"}[name]
    spec = demo_ddp(variant)
    if name.endswith("_alone"):
        # Preserve the explicit placements from the shared competition fixture.
        spec = copy.deepcopy(spec)
        selected = 0 if name == "D2_A_alone" else 1
        spec["jobs"] = [spec["jobs"][selected]]
    return spec


def plot_resource(path, samples, title):
    """Step curve for one observed resource; no invented interpolation values."""
    grouped = {}
    for row in samples:
        if all(k in row for k in ("time_ms", "resource_id", "allocated_gbps", "capacity_gbps")):
            grouped.setdefault(row["resource_id"], []).append(row)
    candidates = {k: v for k, v in grouped.items() if k.startswith("rack:")} or grouped
    if not candidates:
        return False
    rid = max(candidates, key=lambda k: max(r["allocated_gbps"] / r["capacity_gbps"]
              if r["capacity_gbps"] > 0 else 0 for r in candidates[k]))
    rows = sorted(candidates[rid], key=lambda r: r["time_ms"])
    xmax = max(r["time_ms"] for r in rows) or 1
    ymax = max(max(r["capacity_gbps"], r["allocated_gbps"]) for r in rows) or 1
    x = lambda value: 70 + value / xmax * 790
    y = lambda value: 315 - value / ymax * 230
    def points(field):
        data = []
        for i, row in enumerate(rows):
            if i:
                data.append(f'{x(row["time_ms"]):.3f},{y(rows[i-1][field]):.3f}')
            data.append(f'{x(row["time_ms"]):.3f},{y(row[field]):.3f}')
        return " ".join(data)
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 920 390">',
           '<rect width="920" height="390" fill="white"/>',
           '<g font-family="Arial, sans-serif" fill="#203a60">',
           f'<text x="35" y="30" font-size="19">{html.escape(title)}</text>',
           f'<text x="35" y="53" font-size="13">{html.escape(rid)} | simulation, not GPU measurement</text>',
           '<path d="M70 80V315H865" fill="none" stroke="#64748b"/>']
    for fraction in (0, .25, .5, .75, 1):
        svg.append(f'<text x="60" y="{y(ymax*fraction)+4:.2f}" text-anchor="end" font-size="11">{ymax*fraction:.2f}</text>')
        svg.append(f'<text x="{x(xmax*fraction):.2f}" y="337" text-anchor="middle" font-size="11">{xmax*fraction:.1f}</text>')
    svg += [f'<polyline points="{points("capacity_gbps")}" fill="none" stroke="#9ca3af" stroke-dasharray="5 4"/>',
            f'<polyline points="{points("allocated_gbps")}" fill="none" stroke="#2563eb" stroke-width="1.8"/>',
            '<text x="440" y="365" font-size="12">Simulated time (ms)</text>',
            '<text x="12" y="75" font-size="12">Gbps</text>',
            '<text x="620" y="75" font-size="12">Blue: allocated | Grey: capacity</text>', '</g></svg>']
    path.write_text("\n".join(svg), encoding="utf-8")
    return True


def export_case(folder, spec, result):
    folder.mkdir(parents=True, exist_ok=True)
    dump(folder / "input.json", spec)
    dump(folder / "result.json", result)
    dump(folder / "checks.json", result.get("invariant_checks", []))
    if (folder / "error.json").exists():
        (folder / "error.json").unlink()
    dump(folder / "profile.json", {k: spec[k] for k in ("profile", "model") if k in spec})
    with (folder / "events.jsonl").open("w", encoding="utf-8") as f:
        for event in result.get("events", []):
            f.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
    write_csv(folder / "metrics.csv", result.get("jobs", result.get("requests", [])))
    write_csv(folder / "link_timeseries.csv", result.get("resource_samples", []))
    plot_resource(folder / "bottleneck.svg", result.get("resource_samples", []), folder.name)


def acceptance_checks(name, result):
    """Scenario intent is checked separately from engine invariants."""
    jobs = {j["id"]: j for j in result.get("jobs", [])}
    reqs = result.get("requests", [])
    out = []
    def check(label, passed, detail):
        out.append({"name": "scenario_" + label, "passed": bool(passed), "detail": detail})
    if name == "D3_admission":
        c = jobs.get("C", {})
        check("wait_for_whole_group", c.get("waiting_ms", 0) > 0 and c.get("state") == "completed",
              {"waiting_ms": c.get("waiting_ms"), "state": c.get("state")})
    if name == "D3_memory_reject":
        check("memory_is_per_rank", all(j["state"] == "unschedulable" for j in jobs.values()),
              {k: j["state"] for k, j in jobs.items()})
    if name in ("D4_fault_spare", "D4_fault_no_spare", "D4_checkpoint_fault"):
        a = jobs.get("A", {})
        check("rollback_then_complete", a.get("restart_count", 0) >= 1 and a.get("state") == "completed"
              and a.get("lost_steps", 0) > 0 and a.get("world_size") == 4,
              {k: a.get(k) for k in ("restart_count", "lost_steps", "world_size", "state")})
        check("unaffected_job_continues", jobs.get("B", {}).get("restart_count") == 0,
              jobs.get("B", {}).get("restart_count"))
        if name == "D4_fault_no_spare":
            check("no_spare_means_wait", a.get("waiting_ms", 0) > 0, a.get("waiting_ms"))
    if name == "T2_kv_reject":
        check("kv_rejected_without_starvation", any(r["state"] == "rejected" for r in reqs)
              and any(r["state"] == "completed" for r in reqs), [r["state"] for r in reqs])
    if name == "T3_fault":
        check("failed_replica_not_free_migration", any(r["state"] == "failed" for r in reqs)
              and any(r["state"] == "completed" for r in reqs), [r["state"] for r in reqs])
    if name in ("T1_tp1", "T1_tp2", "T1_tp4", "T2_competition", "T2_pair_control", "T2_A_alone", "T2_B_alone"):
        check("same_requested_output_work", all(r["state"] == "completed" and
              r["generated_tokens"] == r["max_new_tokens"] for r in reqs),
              [{"id": r["id"], "generated_tokens": r["generated_tokens"], "max_new_tokens": r["max_new_tokens"]} for r in reqs])
    return out


def comparison_rows(results):
    rows = []
    for solo_name in ("D2_A_alone", "D2_B_alone"):
        if solo_name not in results or "D2_competition" not in results:
            continue
        for solo in results[solo_name].get("jobs", []):
            shared = next((x for x in results["D2_competition"].get("jobs", []) if x.get("id") == solo.get("id")), None)
            if shared:
                row = {"job_id": solo.get("id"), "solo_case": solo_name, "shared_case": "D2_competition"}
                for key in ("completion_ms", "completed_at_ms", "finish_ms", "total_ms", "latency_ms", "job_completion_ms"):
                    if isinstance(solo.get(key), (int, float)) and isinstance(shared.get(key), (int, float)):
                        row.update({"metric": key, "solo_ms": solo[key], "shared_ms": shared[key],
                                    "ratio": shared[key] / solo[key] if solo[key] > 0 else None})
                        break
                rows.append(row)
    for solo_name in ("T2_A_alone", "T2_B_alone"):
        if solo_name not in results or "T2_pair_control" not in results:
            continue
        for solo in results[solo_name].get("requests", []):
            shared = next((r for r in results["T2_pair_control"]["requests"] if r["id"] == solo["id"]), None)
            if shared:
                for metric in ("ttft_ms", "latency_ms"):
                    rows.append({"job_id": solo["id"], "solo_case": solo_name,
                                 "shared_case": "T2_pair_control", "metric": metric,
                                 "solo_ms": solo[metric], "shared_ms": shared[metric],
                                 "ratio": shared[metric] / solo[metric] if solo[metric] else None})
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=ROOT / "evidence" / "v2" / "simulation")
    p.add_argument("--cases", nargs="+", choices=available_cases(), default=available_cases())
    p.add_argument("--overwrite", action="store_true", help="Explicitly overwrite an existing experiment destination")
    p.add_argument("--list", action="store_true")
    args = p.parse_args()
    if args.list:
        print("\n".join(available_cases()))
        return 0
    if args.output.exists() and any(args.output.iterdir()) and not args.overwrite:
        p.error("Output is not empty; choose a new directory or pass --overwrite")
    from fabric_sim import simulate
    args.output.mkdir(parents=True, exist_ok=True)
    runs, results = [], {}
    for name in args.cases:
        folder = args.output / name
        start = time.perf_counter()
        try:
            if name == "E0_analytic":
                spec, result = analytical_ring()
            else:
                spec = scenario_spec(name)
                result = simulate(spec)
            result.setdefault("invariant_checks", []).extend(acceptance_checks(name, result))
            export_case(folder, spec, result)
            checks = result.get("invariant_checks", [])
            failed = [c for c in checks if not c.get("passed", False)]
            outcome = {"case": name, "status": "PASS" if checks and not failed else "FAIL",
                       "checks": len(checks), "failed_checks": failed,
                       "input_hash": result.get("input_hash") or hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
                       "engine_wall_ms": result.get("engine_wall_ms"), "summary": result.get("summary", {}),
                       "folder": name}
            results[name] = result
        except Exception as exc:
            folder.mkdir(parents=True, exist_ok=True)
            outcome = {"case": name, "status": "ERROR", "error_type": type(exc).__name__, "reason": str(exc)}
            dump(folder / "error.json", outcome)
        outcome["export_wall_ms"] = (time.perf_counter() - start) * 1000
        runs.append(outcome)
        print(json.dumps({"case": name, "status": outcome["status"]}, ensure_ascii=False), flush=True)
    summary_result = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                      "python": platform.python_version(), "kind": "SIMULATION",
                      "gpu_measurement_status": "NOT_RUN", "runs": runs,
                      "passed": sum(r["status"] == "PASS" for r in runs),
                      "total": len(runs), "all_passed": all(r["status"] == "PASS" for r in runs),
                      "notes": ["PASS means the listed model invariants passed, not that every job was feasible or every request succeeded.",
                                "Synthetic compute/network/profile settings are inputs, not physical GPU measurements.",
                                "Each scenario's invariants require independent review and unit/integration tests."]}
    dump(args.output / "summary.json", summary_result)
    comparisons = comparison_rows(results)
    write_csv(args.output / "contention_comparison.csv", comparisons)
    return 0 if summary_result["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
