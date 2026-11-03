"""Run a versioned scenario on CPU; no GPU or web server required."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/fabric-sim/src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["ddp", "tp"], default="ddp")
    parser.add_argument("--variant", default=None)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    from fabric_sim import simulate, demo_specs
    if args.list:
        print(json.dumps([{k:v for k,v in d.items() if k != "spec"} for d in demo_specs()], ensure_ascii=False, indent=2))
        return 0
    if args.input:
        spec = json.loads(args.input.read_text(encoding="utf-8"))
    elif args.case == "ddp":
        from fabric_sim.ddp import demo_ddp
        spec = demo_ddp(args.variant or "competition")
    else:
        from fabric_sim.tp import demo_tp
        spec = demo_tp(args.variant or "baseline")
    result = simulate(spec)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k in {"scenario", "input_hash", "summary", "invariant_checks", "engine_wall_ms", "measurement_status"}}, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if all(x["passed"] for x in result.get("invariant_checks", [])) else 1


if __name__ == "__main__":
    raise SystemExit(main())
