from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from uuid import uuid4

from fabric_domain import (
    EvaluationConfig,
    Fault,
    FaultError,
    FirstFitPlacementStrategy,
    InMemoryRevisionRepository,
    Project,
    RailAwarePlacementStrategy,
    Revision,
    RevisionLifecycleService,
    RevisionStatus,
    RevisionValidation,
    evaluate_placement,
    replan_after_faults,
)
from gateway.storage import PostgresRevisionRepository, SqliteRevisionRepository


class RunStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: str
    revision_id: str
    strategy: str
    status: RunStatus
    created_at: str
    completed_at: str | None
    result: dict[str, object] | None
    error: str | None = None
    events: tuple[dict[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "revision_id": self.revision_id,
            "strategy": self.strategy,
            "status": self.status.value,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error,
        }


@dataclass
class GatewayRuntime:
    repository: object = field(default_factory=InMemoryRevisionRepository)
    lifecycle: RevisionLifecycleService | None = None
    runs: dict[str, RunRecord] = field(default_factory=dict)
    lock: RLock = field(default_factory=RLock)

    def __post_init__(self) -> None:
        if self.lifecycle is None:
            self.lifecycle = RevisionLifecycleService(self.repository)

    @classmethod
    def from_environment(cls) -> GatewayRuntime:
        import os
        from pathlib import Path

        backend = os.getenv("REVISION_STORE", "sqlite").lower()
        if backend == "memory":
            return cls()
        if backend == "sqlite":
            root = Path(__file__).resolve().parents[4]
            path = Path(os.getenv("SQLITE_PATH", str(root / "var" / "fabric_sandbox.db")))
            if not path.is_absolute():
                path = root / path
            return cls(repository=SqliteRevisionRepository(path))
        if backend == "postgres":
            database_url = os.getenv("DATABASE_URL")
            if not database_url:
                raise RuntimeError("DATABASE_URL is required for PostgreSQL storage")
            return cls(repository=PostgresRevisionRepository(database_url))
        raise RuntimeError(f"Unsupported REVISION_STORE value: {backend}")

    def create_project(self, name: str) -> Project:
        with self.lock:
            assert self.lifecycle is not None
            return self.lifecycle.create_project(name)

    def get_project(self, project_id: str) -> Project:
        with self.lock:
            assert self.lifecycle is not None
            return self.lifecycle.get_project(project_id)

    def list_projects(self) -> tuple[Project, ...]:
        with self.lock:
            assert self.lifecycle is not None
            return self.lifecycle.list_projects()

    def create_draft(self, project_id: str, content_payload: dict[str, object]) -> Revision:
        from fabric_domain import RevisionContent

        content = RevisionContent.from_dict(content_payload)
        with self.lock:
            assert self.lifecycle is not None
            return self.lifecycle.create_draft(project_id, content)

    def get_revision(self, revision_id: str) -> Revision:
        with self.lock:
            assert self.lifecycle is not None
            return self.lifecycle.get_revision(revision_id)

    def list_revisions(self, project_id: str) -> tuple[Revision, ...]:
        with self.lock:
            assert self.lifecycle is not None
            return self.lifecycle.list_revisions(project_id)

    def validate_revision(self, revision_id: str) -> RevisionValidation:
        with self.lock:
            assert self.lifecycle is not None
            return self.lifecycle.validate(revision_id)

    def commit_revision(self, revision_id: str) -> Revision:
        with self.lock:
            assert self.lifecycle is not None
            return self.lifecycle.commit(revision_id)

    def compare_revisions(self, left_id: str, right_id: str) -> dict[str, object]:
        with self.lock:
            assert self.lifecycle is not None
            return self.lifecycle.compare(left_id, right_id).to_dict()

    def create_run(
        self,
        revision_id: str,
        strategy: str,
        flow_unit_gbps: float,
        fault_payloads: list[dict[str, object]] | None = None,
        failed_workers: list[str] | None = None,
        worker_count: int = 2,
    ) -> RunRecord:
        with self.lock:
            revision = self.get_revision(revision_id)
            if revision.status not in {
                RevisionStatus.VALIDATED,
                RevisionStatus.COMMITTED,
            }:
                raise ValueError(
                    "Only validated or committed revisions can be evaluated"
                )
            if fault_payloads and failed_workers:
                raise ValueError(
                    "Combined fabric faults and worker failures are not supported; "
                    "run these scenarios separately"
                )
            if strategy not in {"rail_aware", "first_fit"}:
                raise ValueError(f"Unknown placement strategy: {strategy}")
            if not 1 <= worker_count <= 16:
                raise ValueError("worker_count must be between 1 and 16")
            unknown_workers = set(failed_workers or []) - {
                f"worker-{index + 1}" for index in range(worker_count)
            }
            if unknown_workers:
                raise ValueError(f"Unknown failed workers: {sorted(unknown_workers)}")

            run_id = f"run-{uuid4()}"
            created_at = datetime.now(UTC).isoformat()
            planning_event = self._event(run_id, RunStatus.PLANNING)
            evaluating_event = self._event(run_id, RunStatus.EVALUATING)
            try:
                faults = tuple(
                    Fault.from_dict(payload)
                    for payload in (fault_payloads or [])
                )
                placement_strategy = (
                    RailAwarePlacementStrategy()
                    if strategy == "rail_aware"
                    else FirstFitPlacementStrategy()
                )
                if faults:
                    replanned = replan_after_faults(
                        revision.content,
                        faults,
                        strategy_name=strategy,
                        flow_unit_gbps=flow_unit_gbps,
                    )
                    placement = replanned.placement
                    evaluation = replanned.evaluation
                    result = {
                        "revision_id": revision.id,
                        "revision_content_hash": revision.content_hash,
                        "placement": placement.to_dict(),
                        "evaluation": evaluation.to_dict(),
                        "fault_replan": replanned.to_dict(),
                    }
                else:
                    placement = placement_strategy.place(
                        revision.content.topology,
                        revision.content.jobs,
                    )
                    from planner import DistributedEvaluationCoordinator

                    distributed = DistributedEvaluationCoordinator(worker_count).evaluate(
                        revision.content.to_dict(),
                        strategy=strategy,
                        flow_unit_gbps=flow_unit_gbps,
                        failed_workers=set(failed_workers or []),
                    )
                    evaluation_data = distributed.evaluation
                    result = {
                        "revision_id": revision.id,
                        "revision_content_hash": revision.content_hash,
                        "placement": placement.to_dict(),
                        "evaluation": evaluation_data,
                        "distributed_evaluation": {
                            "assignments": distributed.assignments,
                            "failed_workers": list(distributed.failed_workers),
                            "reassigned": distributed.reassigned,
                            "recovery_ms": distributed.recovery_ms,
                            "events": list(distributed.events),
                        },
                    }
                status = (
                    RunStatus.COMPLETED
                    if placement.complete and (
                        evaluation.unroutable_flow_count == 0
                        if faults
                        else evaluation_data["unroutable_flow_count"] == 0
                    )
                    else RunStatus.DEGRADED
                )
                worker_events: tuple[dict[str, object], ...] = ()
                if not faults:
                    worker_events = tuple(distributed.events)
                record = RunRecord(
                    id=run_id,
                    revision_id=revision.id,
                    strategy=strategy,
                    status=status,
                    created_at=created_at,
                    completed_at=datetime.now(UTC).isoformat(),
                    result=result,
                    events=(
                        planning_event,
                        evaluating_event,
                        *worker_events,
                        self._event(run_id, status),
                    ),
                )
            except Exception as exc:
                record = RunRecord(
                    id=run_id,
                    revision_id=revision.id,
                    strategy=strategy,
                    status=RunStatus.FAILED,
                    created_at=created_at,
                    completed_at=datetime.now(UTC).isoformat(),
                    result=None,
                    error=str(exc),
                    events=(
                        planning_event,
                        self._event(run_id, RunStatus.FAILED),
                    ),
                )
            self.runs[run_id] = record
            return record

    def get_run(self, run_id: str) -> RunRecord:
        with self.lock:
            try:
                return self.runs[run_id]
            except KeyError as exc:
                raise KeyError(run_id) from exc

    @staticmethod
    def _event(run_id: str, status: RunStatus) -> dict[str, object]:
        return {
            "run_id": run_id,
            "status": status.value,
            "occurred_at": datetime.now(UTC).isoformat(),
        }
