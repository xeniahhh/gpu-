"""Workload-aware GPU network planning and failure simulation."""
from __future__ import annotations

from time import perf_counter
import hashlib
from pathlib import Path
from .validation import canonical_hash, validate_spec

__version__ = "0.2.0"


def implementation_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def simulate(spec: dict) -> dict:
    frozen = validate_spec(spec)
    digest = canonical_hash(frozen)
    started = perf_counter()
    if frozen["scenario"] == "ddp":
        from .ddp import simulate_ddp
        result = simulate_ddp(frozen)
    else:
        from .tp import simulate_tp
        result = simulate_tp(frozen)
    result["input_hash"] = digest
    result["simulator_version"] = __version__
    result["implementation_hash"] = implementation_hash()
    result["engine_wall_ms"] = (perf_counter() - started) * 1000
    result["measurement_status"] = "SIMULATION_NOT_GPU_MEASUREMENT"
    return result


def demo_specs() -> list[dict]:
    from .ddp import demo_ddp
    from .tp import demo_tp
    variants = [("ddp", v, demo_ddp) for v in ("competition", "compact", "admission", "memory_reject", "fault_spare", "fault_no_spare", "checkpoint", "slow_rank")]
    variants += [("tp", v, demo_tp) for v in ("tp1", "tp2", "tp4", "competition", "kv_reject", "fault")]
    return [{"id": f"{case}_{variant}", "scenario": case, "variant": variant, "spec": factory(variant)}
            for case, variant, factory in variants]


__all__ = ["simulate", "demo_specs", "canonical_hash", "validate_spec"]
