"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.api.routes.approvals import router_for as approval_router_for
from forge.api.routes.auth import router_for as auth_router_for
from forge.api.routes.health import router_for as health_router_for
from forge.api.routes.projects import router_for as project_router_for
from forge.api.routes.runs import router_for as run_router_for
from forge.api.routes.tasks import router_for as task_router_for
from forge.api.security import parse_web_origin
from forge.application.ports.clock import Clock
from forge.application.services.approvals import (
    ApprovalAuthorizationService,
    ApprovalChallengeService,
)
from forge.application.services.auth import AuthService
from forge.application.services.projects import ProjectService
from forge.application.services.runs import RunCommandService, RunService
from forge.application.services.tasks import TaskService
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings


def create_app(
    settings: Settings | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    unit_of_work_factory: Callable[[], Any] | None = None,
    clock: Clock | None = None,
    auth_service: Any | None = None,
    approval_challenge_service: ApprovalChallengeService | Any | None = None,
    approval_authorization_service: ApprovalAuthorizationService | Any | None = None,
    project_service: Any | None = None,
    task_service: Any | None = None,
    run_service: Any | None = None,
    run_command_service: Any | None = None,
) -> FastAPI:
    """Create the API without opening a database connection."""

    resolved_settings = settings or Settings(process_role="api")
    parse_web_origin(resolved_settings.web_origin)
    resolved_uow_factory = unit_of_work_factory
    if resolved_uow_factory is None:
        if session_factory is None:
            engine = create_engine(resolved_settings.database_url)
            session_factory = create_session_factory(engine)
        assert session_factory is not None
        resolved_session_factory = session_factory
        resolved_uow_factory = cast(
            Callable[[], Any],
            lambda: PostgresUnitOfWork(resolved_session_factory),
        )
    resolved_auth_service = auth_service or AuthService(resolved_uow_factory, clock=clock)
    resolved_challenge_service = approval_challenge_service or ApprovalChallengeService(
        resolved_uow_factory,
        clock=clock,
    )
    resolved_authorization_service = approval_authorization_service or ApprovalAuthorizationService(
        resolved_uow_factory, clock=clock
    )
    resolved_project_service = project_service or ProjectService(
        resolved_uow_factory, settings=resolved_settings
    )
    resolved_task_service = task_service or TaskService(resolved_uow_factory)
    resolved_run_service = run_service or RunService(
        resolved_uow_factory, settings=resolved_settings
    )
    resolved_run_command_service = run_command_service or RunCommandService(resolved_uow_factory)

    app = FastAPI(title="Parallel Forge", version="0.1.0")

    @app.exception_handler(SQLAlchemyError)
    async def persistence_error(_request: Request, _error: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "persistence unavailable"},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": "invalid request"},
        )

    app.state.settings = resolved_settings
    app.state.auth_service = resolved_auth_service
    app.state.approval_challenge_service = resolved_challenge_service
    app.state.approval_authorization_service = resolved_authorization_service
    app.state.project_service = resolved_project_service
    app.state.task_service = resolved_task_service
    app.state.run_service = resolved_run_service
    app.state.run_command_service = resolved_run_command_service
    app.state.session_factory = session_factory
    app.include_router(health_router_for(resolved_settings.process_role), prefix="/api")
    app.include_router(auth_router_for(), prefix="/api")
    app.include_router(approval_router_for(), prefix="/api")
    app.include_router(project_router_for(), prefix="/api")
    app.include_router(task_router_for(), prefix="/api")
    app.include_router(run_router_for(), prefix="/api")
    return app
