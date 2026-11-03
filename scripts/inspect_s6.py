from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "fabric-domain" / "src"))

from fabric_domain import (  # noqa: E402
    InMemoryRevisionRepository,
    InvalidRevisionTransition,
    RevisionLifecycleService,
    RevisionStatus,
    SequentialIdFactory,
    StaleRevision,
    load_revision_file,
)


FIXTURE = ROOT / "tests" / "fixtures" / "topologies" / "micro-2x2.json"


def main() -> int:
    base = load_revision_file(FIXTURE)
    service = RevisionLifecycleService(
        InMemoryRevisionRepository(),
        SequentialIdFactory(),
    )
    project = service.create_project("S6 revision demo")

    revision_one = service.create_draft(project.id, base)
    validation_one = service.validate(revision_one.id)
    committed_one = service.commit(revision_one.id)

    changed_jobs = (
        replace(base.jobs[0], priority=20),
        base.jobs[1],
    )
    changed_content = replace(base, jobs=changed_jobs)
    revision_two = service.create_draft(project.id, changed_content)
    validation_two = service.validate(revision_two.id)
    diff = service.compare(committed_one.id, revision_two.id)
    committed_two = service.commit(revision_two.id)

    invalid_content = replace(
        base,
        jobs=(
            replace(base.jobs[0], requested_gpus=9),
            base.jobs[1],
        ),
    )
    revision_three = service.create_draft(project.id, invalid_content)
    validation_three = service.validate(revision_three.id)
    try:
        service.commit(revision_three.id)
        commit_rejected = False
        commit_error = None
    except (InvalidRevisionTransition, StaleRevision) as exc:
        commit_rejected = True
        commit_error = str(exc)

    project_after = service.get_project(project.id)

    print("FabricSandbox S6 revision lifecycle")
    print(f"Project           : {project.id} ({project.name})")
    print()
    print("Lifecycle")
    for revision, validation in (
        (revision_one, validation_one),
        (revision_two, validation_two),
        (revision_three, validation_three),
    ):
        result = "PASS" if validation.report.valid else "FAIL"
        print(
            f"  {revision.id}: DRAFT -> validate({result}) -> "
            f"{validation.revision.status.value}"
        )
    print(f"  current revision: {project_after.current_revision_id}")
    print(f"  revision 1 hash : {revision_one.content_hash[:12]}")
    print(f"  revision 2 hash : {revision_two.content_hash[:12]}")
    print()
    print("Revision diff: revision-0001 -> revision-0002")
    if not diff.entries:
        print("  no content changes")
    for entry in diff.entries:
        if entry.change.value == "changed":
            before = entry.before
            after = entry.after
            if isinstance(before, dict) and isinstance(after, dict):
                changed_fields = [
                    key
                    for key in sorted(set(before) | set(after))
                    if before.get(key) != after.get(key)
                ]
                detail = ", ".join(
                    f"{field}: {before.get(field)!r} -> {after.get(field)!r}"
                    for field in changed_fields
                )
            else:
                detail = f"{entry.before!r} -> {entry.after!r}"
        else:
            detail = entry.change.value
        print(f"  {entry.path}: {detail}")
    print(
        f"  summary: +{diff.added_count} -{diff.removed_count} "
        f"~{diff.changed_count}"
    )
    print()
    print(
        f"Invalid revision commit rejected: "
        f"{'YES' if commit_rejected else 'NO'}"
    )
    if commit_error:
        print(f"  reason: {commit_error}")
    print(
        f"Final committed status: "
        f"{RevisionStatus.COMMITTED.value} ({committed_two.id})"
    )


if __name__ == "__main__":
    raise SystemExit(main())
