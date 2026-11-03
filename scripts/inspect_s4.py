from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "fabric-domain" / "src"))

from fabric_domain import (  # noqa: E402
    FirstFitPlacementStrategy,
    PlacementResult,
    RailAwarePlacementStrategy,
    Topology,
    load_revision_file,
    rail_alignment_score,
    validate_revision,
)


DEFAULT_FIXTURE = (
    ROOT / "tests" / "fixtures" / "topologies" / "rail-aware-comparison.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare S3 first-fit with S4 rail-aware placement."
    )
    parser.add_argument(
        "fixture",
        nargs="?",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="Revision fixture used for comparison.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print both placement results as JSON.",
    )
    return parser.parse_args()


def average_rail_alignment(
    topology: Topology,
    result: PlacementResult,
) -> float:
    if not result.assignments:
        return 0.0
    return sum(
        rail_alignment_score(assignment)
        for assignment in result.assignments
    ) / len(result.assignments)


def print_result(
    topology: Topology,
    title: str,
    result: PlacementResult,
) -> None:
    print(title)
    print(f"  strategy       : {result.strategy}")
    print(f"  job order      : {', '.join(result.job_order)}")
    print(
        f"  allocated GPUs : "
        f"{result.assigned_gpu_count}/{result.total_gpus}"
    )
    print(
        f"  rail alignment : "
        f"{average_rail_alignment(topology, result):.1%}"
    )
    print(f"  complete       : {'YES' if result.complete else 'NO'}")
    print("  assignments")
    for assignment in result.assignments:
        rails = sorted({gpu.rail_id for gpu in assignment.gpus})
        gpu_ids = ", ".join(gpu.gpu_id for gpu in assignment.gpus)
        print(
            f"    {assignment.job_id}: nodes={','.join(assignment.node_ids)} "
            f"rails={rails}"
        )
        print(f"      GPUs: {gpu_ids}")


def main() -> int:
    args = parse_args()
    fixture = args.fixture.resolve()
    content = load_revision_file(fixture)
    validation = validate_revision(content)
    if not validation.valid:
        print("Cannot place jobs because revision validation failed.")
        return 1

    first_fit = FirstFitPlacementStrategy().place(
        content.topology,
        content.jobs,
    )
    rail_aware = RailAwarePlacementStrategy().place(
        content.topology,
        content.jobs,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "fixture": str(fixture),
                    "first_fit": first_fit.to_dict(),
                    "rail_aware": rail_aware.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("FabricSandbox S4 rail-aware comparison")
    print(f"Fixture: {fixture}")
    print(f"Topology: {content.topology.id}")
    print()
    print_result(content.topology, "first-fit baseline", first_fit)
    print()
    print_result(content.topology, "rail-aware placement", rail_aware)
    print()
    first_alignment = average_rail_alignment(content.topology, first_fit)
    rail_alignment = average_rail_alignment(content.topology, rail_aware)
    print(f"Average rail-alignment delta: {rail_alignment - first_alignment:+.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
