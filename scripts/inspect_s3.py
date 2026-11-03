from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "fabric-domain" / "src"))

from fabric_domain import (  # noqa: E402
    FirstFitPlacementStrategy,
    load_revision_file,
    validate_revision,
)


DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "topologies" / "micro-2x2.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the S3 first-fit placement.")
    parser.add_argument(
        "fixture",
        nargs="?",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="Revision fixture used for placement.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete placement result as JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fixture = args.fixture.resolve()
    content = load_revision_file(fixture)
    validation = validate_revision(content)
    if not validation.valid:
        print("Cannot place jobs because revision validation failed.")
        for finding in validation.errors:
            print(
                f"[error] {finding.code}: "
                f"{finding.entity_type}/{finding.entity_id} - {finding.message}"
            )
        return 1

    result = FirstFitPlacementStrategy().place(
        content.topology,
        content.jobs,
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print("FabricSandbox S3 first-fit placement")
    print(f"Fixture     : {fixture}")
    print(f"Topology    : {content.topology.id}")
    print(f"Strategy    : {result.strategy}")
    print(f"Job order   : {', '.join(result.job_order)}")
    print()
    print("Assignments")
    for assignment in result.assignments:
        gpu_ids = ", ".join(gpu.gpu_id for gpu in assignment.gpus)
        print(f"  {assignment.job_id}")
        print(f"    nodes: {', '.join(assignment.node_ids)}")
        print(f"    GPUs : {gpu_ids}")
    if not result.assignments:
        print("  none")
    print()
    print(f"Allocated GPUs : {result.assigned_gpu_count}/{result.total_gpus}")
    print(f"Utilization    : {result.utilization:.1%}")
    print(f"Unplaced jobs  : {', '.join(result.unplaced_job_ids) or 'none'}")
    print(f"Complete       : {'YES' if result.complete else 'NO'}")
    if result.findings:
        print("Findings")
        for finding in result.findings:
            print(f"  {finding.job_id}: {finding.code} - {finding.message}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
