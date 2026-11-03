from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "fabric-domain" / "src"))

from fabric_domain import (  # noqa: E402
    TopologyGenerationConfig,
    generate_topology,
    load_revision_file,
    validate_revision,
    validate_topology,
)


MICRO_FIXTURE = ROOT / "tests" / "fixtures" / "topologies" / "micro-2x2.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate and validate an S2 topology.")
    parser.add_argument(
        "--preset",
        choices=("mvp", "micro"),
        default="mvp",
        help="Use the 1024-GPU generated topology or the S1 micro fixture.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.preset == "micro":
        content = load_revision_file(MICRO_FIXTURE)
        topology = content.topology
        report = validate_revision(content)
        source = f"fixture {MICRO_FIXTURE}"
        job_count = len(content.jobs)
    else:
        config = TopologyGenerationConfig()
        topology = generate_topology(config)
        report = validate_topology(
            topology,
            expected_gpus_per_node=config.gpus_per_node,
        )
        source = (
            f"generated {config.spine_count}x{config.leaf_count}"
            f"x{config.nodes_per_leaf}x{config.gpus_per_node}"
        )
        job_count = 0

    link_kinds = Counter(link.kind.value for link in topology.links)

    print("FabricSandbox S2 topology validation")
    print(f"Source         : {source}")
    print(f"Topology       : {topology.id}")
    print()
    print("Scale")
    print(f"  spines : {len(topology.spines)}")
    print(f"  racks  : {len(topology.racks)}")
    print(f"  leaves : {len(topology.leaves)}")
    print(f"  nodes  : {len(topology.nodes)}")
    print(f"  GPUs   : {topology.gpu_count}")
    print(f"  links  : {len(topology.links)}")
    print(f"  jobs   : {job_count}")
    print()
    print("Links by kind")
    for kind, count in sorted(link_kinds.items()):
        print(f"  {kind}: {count}")
    print()
    print(f"Validation: {'PASS' if report.valid else 'FAIL'}")
    print(f"Errors    : {len(report.errors)}")
    print(f"Warnings  : {len(report.warnings)}")
    if report.findings:
        print("Findings")
        for finding in report.findings:
            print(
                f"  [{finding.severity.value}] {finding.code}: "
                f"{finding.entity_type}/{finding.entity_id} - {finding.message}"
            )
    else:
        print("Findings  : none")

    return 0 if report.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
