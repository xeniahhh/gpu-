from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from fabric_domain.models import RevisionContent
from fabric_domain.validation import ValidationReport, validate_revision


class RevisionLifecycleError(RuntimeError):
    """Base error for revision lifecycle operations."""


class ProjectNotFound(RevisionLifecycleError):
    pass


class RevisionNotFound(RevisionLifecycleError):
    pass


class InvalidRevisionTransition(RevisionLifecycleError):
    pass


class StaleRevision(RevisionLifecycleError):
    pass


class RevisionStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    VALIDATION_FAILED = "validation_failed"
    COMMITTED = "committed"


class ChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    current_revision_id: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "current_revision_id": self.current_revision_id,
        }


@dataclass(frozen=True, slots=True)
class Revision:
    id: str
    project_id: str
    parent_revision_id: str | None
    status: RevisionStatus
    content: RevisionContent
    content_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "parent_revision_id": self.parent_revision_id,
            "status": self.status.value,
            "content_hash": self.content_hash,
            "content": self.content.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RevisionValidation:
    revision: Revision
    report: ValidationReport

    def to_dict(self) -> dict[str, object]:
        return {
            "revision": self.revision.to_dict(),
            "validation": self.report.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class DiffEntry:
    path: str
    change: ChangeKind
    before: object | None
    after: object | None

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "change": self.change.value,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True, slots=True)
class RevisionDiff:
    left_revision_id: str
    right_revision_id: str
    entries: tuple[DiffEntry, ...]

    @property
    def added_count(self) -> int:
        return sum(entry.change is ChangeKind.ADDED for entry in self.entries)

    @property
    def removed_count(self) -> int:
        return sum(entry.change is ChangeKind.REMOVED for entry in self.entries)

    @property
    def changed_count(self) -> int:
        return sum(entry.change is ChangeKind.CHANGED for entry in self.entries)

    def to_dict(self) -> dict[str, object]:
        return {
            "left_revision_id": self.left_revision_id,
            "right_revision_id": self.right_revision_id,
            "added_count": self.added_count,
            "removed_count": self.removed_count,
            "changed_count": self.changed_count,
            "entries": [entry.to_dict() for entry in self.entries],
        }


class RevisionRepository(Protocol):
    def add_project(self, project: Project) -> None: ...

    def get_project(self, project_id: str) -> Project: ...

    def save_project(self, project: Project) -> None: ...

    def list_projects(self) -> tuple[Project, ...]: ...

    def add_revision(self, revision: Revision) -> None: ...

    def get_revision(self, revision_id: str) -> Revision: ...

    def save_revision(self, revision: Revision) -> None: ...

    def list_revisions(self, project_id: str) -> tuple[Revision, ...]: ...

    def commit(self, revision: Revision, project: Project) -> None: ...


class InMemoryRevisionRepository:
    def __init__(self) -> None:
        self._projects: dict[str, Project] = {}
        self._revisions: dict[str, Revision] = {}

    def add_project(self, project: Project) -> None:
        if project.id in self._projects:
            raise RevisionLifecycleError(f"Project {project.id!r} already exists")
        self._projects[project.id] = project

    def get_project(self, project_id: str) -> Project:
        try:
            return self._projects[project_id]
        except KeyError as exc:
            raise ProjectNotFound(project_id) from exc

    def save_project(self, project: Project) -> None:
        self._projects[project.id] = project

    def list_projects(self) -> tuple[Project, ...]:
        return tuple(
            sorted(self._projects.values(), key=lambda project: project.id)
        )

    def add_revision(self, revision: Revision) -> None:
        if revision.id in self._revisions:
            raise RevisionLifecycleError(f"Revision {revision.id!r} already exists")
        self._revisions[revision.id] = revision

    def get_revision(self, revision_id: str) -> Revision:
        try:
            return self._revisions[revision_id]
        except KeyError as exc:
            raise RevisionNotFound(revision_id) from exc

    def save_revision(self, revision: Revision) -> None:
        self._revisions[revision.id] = revision

    def list_revisions(self, project_id: str) -> tuple[Revision, ...]:
        return tuple(
            sorted(
                (
                    revision
                    for revision in self._revisions.values()
                    if revision.project_id == project_id
                ),
                key=lambda revision: revision.id,
            )
        )

    def commit(self, revision: Revision, project: Project) -> None:
        self._revisions[revision.id] = revision
        self._projects[project.id] = project


class IdFactory(Protocol):
    def new_project_id(self) -> str: ...

    def new_revision_id(self) -> str: ...


class UuidIdFactory:
    def new_project_id(self) -> str:
        return f"project-{uuid4()}"

    def new_revision_id(self) -> str:
        return f"revision-{uuid4()}"


class SequentialIdFactory:
    def __init__(self) -> None:
        self._project_number = 0
        self._revision_number = 0

    def new_project_id(self) -> str:
        self._project_number += 1
        return f"project-{self._project_number:04d}"

    def new_revision_id(self) -> str:
        self._revision_number += 1
        return f"revision-{self._revision_number:04d}"


def calculate_content_hash(content: RevisionContent) -> str:
    payload = content.to_dict()
    payload.pop("revision_id", None)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RevisionLifecycleService:
    def __init__(
        self,
        repository: RevisionRepository,
        id_factory: IdFactory | None = None,
    ) -> None:
        self._repository = repository
        self._id_factory = id_factory or UuidIdFactory()

    def create_project(self, name: str) -> Project:
        stripped_name = name.strip()
        if not stripped_name:
            raise RevisionLifecycleError("Project name must not be empty")
        project = Project(
            id=self._id_factory.new_project_id(),
            name=stripped_name,
            current_revision_id=None,
        )
        self._repository.add_project(project)
        return project

    def get_project(self, project_id: str) -> Project:
        return self._repository.get_project(project_id)

    def list_projects(self) -> tuple[Project, ...]:
        return self._repository.list_projects()

    def get_revision(self, revision_id: str) -> Revision:
        return self._repository.get_revision(revision_id)

    def list_revisions(self, project_id: str) -> tuple[Revision, ...]:
        return self._repository.list_revisions(project_id)

    def create_draft(
        self,
        project_id: str,
        content: RevisionContent,
    ) -> Revision:
        project = self._repository.get_project(project_id)
        revision_id = self._id_factory.new_revision_id()
        canonical_content = replace(content, revision_id=revision_id)
        revision = Revision(
            id=revision_id,
            project_id=project.id,
            parent_revision_id=project.current_revision_id,
            status=RevisionStatus.DRAFT,
            content=canonical_content,
            content_hash=calculate_content_hash(canonical_content),
        )
        self._repository.add_revision(revision)
        return revision

    def validate(self, revision_id: str) -> RevisionValidation:
        revision = self._repository.get_revision(revision_id)
        if revision.status not in {
            RevisionStatus.DRAFT,
            RevisionStatus.VALIDATION_FAILED,
        }:
            raise InvalidRevisionTransition(
                f"Cannot validate revision in {revision.status.value!r} state"
            )
        report = validate_revision(revision.content)
        next_status = (
            RevisionStatus.VALIDATED
            if report.valid
            else RevisionStatus.VALIDATION_FAILED
        )
        updated = replace(revision, status=next_status)
        self._repository.save_revision(updated)
        return RevisionValidation(revision=updated, report=report)

    def commit(self, revision_id: str) -> Revision:
        revision = self._repository.get_revision(revision_id)
        if revision.status is not RevisionStatus.VALIDATED:
            raise InvalidRevisionTransition(
                f"Cannot commit revision in {revision.status.value!r} state"
            )
        project = self._repository.get_project(revision.project_id)
        if revision.parent_revision_id != project.current_revision_id:
            raise StaleRevision(
                "Revision parent does not match the project's current revision"
            )
        committed = replace(revision, status=RevisionStatus.COMMITTED)
        updated_project = replace(
            project,
            current_revision_id=committed.id,
        )
        self._repository.commit(committed, updated_project)
        return committed

    def compare(self, left_id: str, right_id: str) -> RevisionDiff:
        left = self._repository.get_revision(left_id)
        right = self._repository.get_revision(right_id)
        return compare_revision_content(
            left_id=left.id,
            left=left.content,
            right_id=right.id,
            right=right.content,
        )


def compare_revision_content(
    *,
    left_id: str,
    left: RevisionContent,
    right_id: str,
    right: RevisionContent,
) -> RevisionDiff:
    entries: list[DiffEntry] = []

    if left.schema_version != right.schema_version:
        entries.append(
            DiffEntry(
                path="schema_version",
                change=ChangeKind.CHANGED,
                before=left.schema_version,
                after=right.schema_version,
            )
        )
    if left.topology.id != right.topology.id:
        entries.append(
            DiffEntry(
                path="topology.id",
                change=ChangeKind.CHANGED,
                before=left.topology.id,
                after=right.topology.id,
            )
        )

    left_topology = left.topology.to_dict()
    right_topology = right.topology.to_dict()
    for collection_name in ("spines", "racks", "leaves", "nodes", "links"):
        _compare_entities(
            entries,
            f"topology.{collection_name}",
            left_topology[collection_name],
            right_topology[collection_name],
        )

    _compare_entities(
        entries,
        "jobs",
        [job.to_dict() for job in left.jobs],
        [job.to_dict() for job in right.jobs],
    )

    return RevisionDiff(
        left_revision_id=left_id,
        right_revision_id=right_id,
        entries=tuple(entries),
    )


def _compare_entities(
    entries: list[DiffEntry],
    path: str,
    left_items: object,
    right_items: object,
) -> None:
    if not isinstance(left_items, list) or not isinstance(right_items, list):
        raise RevisionLifecycleError(f"Expected lists while comparing {path}")

    left_by_id = {item["id"]: item for item in left_items}
    right_by_id = {item["id"]: item for item in right_items}
    for entity_id in sorted(set(left_by_id) | set(right_by_id)):
        entity_path = f"{path}.{entity_id}"
        if entity_id not in left_by_id:
            entries.append(
                DiffEntry(
                    path=entity_path,
                    change=ChangeKind.ADDED,
                    before=None,
                    after=right_by_id[entity_id],
                )
            )
        elif entity_id not in right_by_id:
            entries.append(
                DiffEntry(
                    path=entity_path,
                    change=ChangeKind.REMOVED,
                    before=left_by_id[entity_id],
                    after=None,
                )
            )
        elif left_by_id[entity_id] != right_by_id[entity_id]:
            entries.append(
                DiffEntry(
                    path=entity_path,
                    change=ChangeKind.CHANGED,
                    before=left_by_id[entity_id],
                    after=right_by_id[entity_id],
                )
            )
