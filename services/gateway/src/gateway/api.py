from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from fabric_domain import (
    ContractError,
    FaultError,
    InvalidRevisionTransition,
    ProjectNotFound,
    RevisionNotFound,
    RevisionLifecycleError,
    StaleRevision,
    load_revision_file,
)
from gateway.runtime import GatewayRuntime, RunStatus
from gateway.security import AccessPolicy
from gateway.assistant import explain_run
from gateway.simulation_api import SimulationService, create_simulation_router


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class CreateRevisionRequest(BaseModel):
    content: dict[str, Any]


class CreateRunRequest(BaseModel):
    revision_id: str
    strategy: Literal["rail_aware", "first_fit"] = "rail_aware"
    flow_unit_gbps: float = Field(default=10.0, gt=0)
    faults: list[dict[str, str]] = Field(default_factory=list)
    failed_workers: list[str] = Field(default_factory=list)
    worker_count: int = Field(default=2, ge=1, le=16)


class ExplainRunRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=120)
    question: str = Field(default="请解释本次运行的瓶颈和局限", min_length=1, max_length=1000)


def create_app(
    runtime: GatewayRuntime | None = None,
    access_policy: AccessPolicy | None = None,
    simulation_service: SimulationService | None = None,
) -> FastAPI:
    state = runtime or GatewayRuntime()
    policy = access_policy or AccessPolicy.from_environment()
    app = FastAPI(
        title="FabricSandbox API",
        version="0.1.0",
        description="GPU fabric placement and bandwidth planning sandbox.",
    )
    app.state.runtime = state
    app.state.access_policy = policy
    simulations = simulation_service or SimulationService()
    app.state.simulation_service = simulations
    app.include_router(create_simulation_router(simulations))

    @app.middleware("http")
    async def enforce_access(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path.rstrip("/") != "/api/health" and request.method != "OPTIONS":
            peer = request.client.host if request.client else None
            role = policy.identify(request.headers.get("Authorization"), peer)
            if policy.mode == "local_demo" and not policy.local_context_is_safe(
                request.headers.get("Host", ""),
                request.headers.get("Origin"),
                any(name in request.headers for name in ("Forwarded", "X-Forwarded-For", "X-Real-IP")),
            ):
                role = None
            if role is None:
                detail = ("Local demo mode accepts loopback clients only"
                          if policy.mode == "local_demo" else "A valid Bearer token is required")
                return JSONResponse(status_code=401, content={"detail": detail},
                                    headers={"WWW-Authenticate": "Bearer"})
            if policy.requires_editor(request.method, path) and role != "editor":
                return JSONResponse(status_code=403, content={"detail": "Editor role required"})
            request.state.role = role
        return await call_next(request)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "gateway", "auth_mode": policy.mode}

    @app.post("/api/demo/bootstrap", status_code=201)
    def bootstrap_demo() -> dict[str, object]:
        root = Path(__file__).resolve().parents[4]
        fixture = root / "tests" / "fixtures" / "topologies" / "fault-replan.json"
        if not fixture.is_file():
            raise HTTPException(status_code=500, detail="Demo fixture is missing")
        project = state.create_project("FabricSandbox Demo")
        revision = state.create_draft(
            project.id,
            load_revision_file(fixture).to_dict(),
        )
        validated = state.validate_revision(revision.id)
        return {
            "project": project.to_dict(),
            "revision": validated.revision.to_dict(),
            "validation": validated.report.to_dict(),
        }

    @app.post("/api/projects", status_code=201)
    def create_project(request: CreateProjectRequest) -> dict[str, object]:
        return state.create_project(request.name).to_dict()

    @app.get("/api/projects")
    def list_projects() -> list[dict[str, object]]:
        return [project.to_dict() for project in state.list_projects()]

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, object]:
        try:
            return state.get_project(project_id).to_dict()
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/revisions", status_code=201)
    def create_revision(
        project_id: str,
        request: CreateRevisionRequest,
    ) -> dict[str, object]:
        try:
            revision = state.create_draft(project_id, request.content)
            return revision.to_dict()
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ContractError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/projects/{project_id}/revisions")
    def list_revisions(project_id: str) -> list[dict[str, object]]:
        try:
            state.get_project(project_id)
            return [
                revision.to_dict()
                for revision in state.list_revisions(project_id)
            ]
        except ProjectNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/revisions/{revision_id}")
    def get_revision(revision_id: str) -> dict[str, object]:
        try:
            return state.get_revision(revision_id).to_dict()
        except RevisionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/revisions/{revision_id}/validate")
    def validate_revision(revision_id: str) -> dict[str, object]:
        try:
            return state.validate_revision(revision_id).to_dict()
        except RevisionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidRevisionTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/revisions/{revision_id}/commit")
    def commit_revision(revision_id: str) -> dict[str, object]:
        try:
            return state.commit_revision(revision_id).to_dict()
        except RevisionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InvalidRevisionTransition, StaleRevision) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/revisions/{left_id}/diff/{right_id}")
    def compare_revisions(
        left_id: str,
        right_id: str,
    ) -> dict[str, object]:
        try:
            return state.compare_revisions(left_id, right_id)
        except RevisionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/runs", status_code=201)
    def create_run(request: CreateRunRequest) -> dict[str, object]:
        try:
            return state.create_run(
                revision_id=request.revision_id,
                strategy=request.strategy,
                flow_unit_gbps=request.flow_unit_gbps,
                fault_payloads=request.faults,
                failed_workers=request.failed_workers,
                worker_count=request.worker_count,
            ).to_dict()
        except RevisionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, FaultError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, object]:
        try:
            return state.get_run(run_id).to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

    @app.get("/api/runs/{run_id}/events")
    async def get_run_events(run_id: str) -> StreamingResponse:
        try:
            run = state.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

        async def event_stream() -> AsyncIterator[str]:
            for event in run.events:
                event_name = event.get("status", event.get("type", "message"))
                yield f"event: {event_name}\ndata: {json.dumps(event)}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/assistant/explain")
    def explain(request: ExplainRunRequest) -> dict[str, Any]:
        try:
            run = state.get_run(request.run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        return explain_run(run.to_dict(), request.question)

    @app.get("/api/info")
    def root() -> dict[str, str]:
        return {
            "service": "FabricSandbox",
            "status": "running",
            "docs": "/docs",
            "health": "/api/health",
            "auth_mode": policy.mode,
            "authorization_scope": "service_wide_roles_no_tenant_isolation",
            "assistant_mode": "rules_based_read_only",
        }

    return app


app = create_app()
