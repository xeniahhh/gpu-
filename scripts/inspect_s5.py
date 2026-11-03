from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "fabric-domain" / "src"))

from fabric_domain import (  # noqa: E402
    EvaluationConfig,
    FirstFitPlacementStrategy,
    RailAwarePlacementStrategy,
    evaluate_placement,
    load_revision_file,
    validate_revision,
)


DEFAULT_FIXTURE = (
    ROOT / "tests" / "fixtures" / "topologies" / "rail-aware-comparison.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate S5 fabric paths and metrics.")
    parser.add_argument(
        "fixture",
        nargs="?",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="Revision fixture used for evaluation.",
    )
    parser.add_argument(
        "--strategy",
        choices=("rail_aware", "first_fit"),
        default="rail_aware",
        help="Placement strategy to evaluate.",
    )
    parser.add_argument(
        "--flow-unit-gbps",
        type=float,
        default=10.0,
        help="Requested bandwidth represented by one traffic unit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete evaluation as JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fixture = args.fixture.resolve()
    content = load_revision_file(fixture)
    validation = validate_revision(content)
    if not validation.valid:
        print("Cannot evaluate because revision validation failed.")
        return 1

    strategy = (
        RailAwarePlacementStrategy()
        if args.strategy == "rail_aware"
        else FirstFitPlacementStrategy()
    )
    placement = strategy.place(content.topology, content.jobs)
    evaluation = evaluate_placement(
        content.topology,
        content.jobs,
        placement,
        EvaluationConfig(flow_unit_gbps=args.flow_unit_gbps),
    )

    if args.json:
        print(json.dumps(evaluation.to_dict(), ensure_ascii=False, indent=2))
        return 0 if evaluation.unroutable_flow_count == 0 else 1

    print("FabricSandbox S5 path and bandwidth evaluation")
    print(f"Fixture          : {fixture}")
    print(f"Topology         : {content.topology.id}")
    print(f"Strategy         : {evaluation.placement_strategy}")
    print(f"Flow unit demand : {evaluation.flow_unit_gbps:.1f} Gbps")
    print()
    print("Flows")
    if evaluation.flows:
        for flow in evaluation.flows:
            path = " -> ".join(flow.path_link_ids) or "unroutable"
            print(
                f"  {flow.job_id}: {flow.source_node_id} -> {flow.target_node_id} "
                f"units={flow.traffic_units}"
            )
            print(f"    path: {path}")
            print(
                f"    allocated={flow.allocated_gbps:.1f} Gbps "
                f"per-unit={flow.per_unit_gbps:.1f} Gbps"
            )
    else:
        print("  no cross-node flows")
    print()
    print("Jobs")
    for job in evaluation.jobs:
        throughput = (
            "n/a"
            if job.throughput_proxy_gbps is None
            else f"{job.throughput_proxy_gbps:.1f} Gbps"
        )
        print(
            f"  {job.job_id}: flows={job.flow_count}, "
            f"units={job.traffic_units}, throughput={throughput}"
        )
        print(
            f"    bottlenecks: "
            f"{', '.join(job.bottleneck_link_ids) or 'none'}"
        )
    print()
    print("Top loaded links")
    for link in evaluation.top_bottlenecks(limit=8):
        print(
            f"  {link.link_id}: {link.requested_gbps:.1f}/"
            f"{link.capacity_gbps:.1f} Gbps ({link.utilization:.1%})"
        )
    if not evaluation.top_bottlenecks(limit=8):
        print("  none")
    print()
    print("Leaf oversubscription")
    for leaf in evaluation.leaf_oversubscription:
        ratio = "n/a" if leaf.ratio is None else f"{leaf.ratio:.2f}:1"
        print(f"  {leaf.leaf_id}: {ratio}")
    print()
    print(
        "Balanced leaf bisection proxy: "
        f"{evaluation.balanced_leaf_bisection_proxy_gbps:.1f} Gbps"
    )
    print(f"Oversubscribed links: {len(evaluation.oversubscribed_link_ids)}")
    print(f"Unroutable flows    : {evaluation.unroutable_flow_count}")

    return 0 if evaluation.unroutable_flow_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
