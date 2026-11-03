from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "fabric-domain" / "src"))

from fabric_domain import (  # noqa: E402
    RevisionContent,
    load_revision_file,
    revision_from_json,
    revision_to_json,
)


DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "topologies" / "micro-2x2.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect the S1 domain fixture and contracts.")
    parser.add_argument(
        "fixture",
        nargs="?",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="Revision fixture to inspect.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the normalized JSON contract instead of a summary.",
    )
    return parser.parse_args()


def load_and_verify(path: Path) -> tuple[RevisionContent, bool]:
    content = load_revision_file(path)
    normalized = revision_to_json(content)
    reparsed = revision_from_json(normalized)
    return content, reparsed == content


def print_summary(path: Path, content: RevisionContent, round_trip_ok: bool) -> None:
    topology = content.topology
    gpu_rails = sorted(
        {gpu.rail_id for node in topology.nodes for gpu in node.gpus}
    )

    print("FabricSandbox S1 contract inspection")
    print(f"Fixture       : {path}")
    print(f"Revision      : {content.revision_id}")
    print(f"Schema version: {content.schema_version}")
    print()
    print("Topology")
    print(f"  spines : {len(topology.spines)}")
    print(f"  racks  : {len(topology.racks)}")
    print(f"  leaves : {len(topology.leaves)}")
    print(f"  nodes  : {len(topology.nodes)}")
    print(f"  GPUs   : {topology.gpu_count}")
    print(f"  links  : {len(topology.links)}")
    print(f"  rails  : {gpu_rails}")
    print()
    print("Jobs")
    for job in content.jobs:
        print(
            f"  {job.id}: {job.requested_gpus} GPU, "
            f"{job.communication_mode.value}, priority={job.priority}"
        )
    print()
    print(f"JSON round-trip: {'PASS' if round_trip_ok else 'FAIL'}")
    print()
    print("Inspect source files:")
    print(f"  fixture: {path}")
    print(f"  schemas: {ROOT / 'packages' / 'event-contracts' / 'schemas'}")


def main() -> int:
    args = parse_args()
    path = args.fixture.resolve()
    content, round_trip_ok = load_and_verify(path)

    if args.json:
        print(json.dumps(content.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_summary(path, content, round_trip_ok)

    return 0 if round_trip_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
