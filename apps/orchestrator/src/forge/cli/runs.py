"""Local operator commands for creating and inspecting durable runs."""

from __future__ import annotations

# Typer options are declarative; failures are emitted with bounded text.
# ruff: noqa: B008, BLE001
import asyncio
import json
from collections.abc import Callable
from typing import Any, cast
from uuid import UUID

import typer

from forge.application.services.runs import RunService
from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings

run_app = typer.Typer(add_completion=False, no_args_is_help=True)


async def _run(operation: Callable[[RunService], Any]) -> Any:
    settings = Settings(process_role="cli")
    engine = create_engine(settings.database_url)
    try:
        sessions = create_session_factory(engine)
        factory = cast(Callable[[], Any], lambda: PostgresUnitOfWork(sessions))
        service = RunService(factory, settings=settings)
        return await operation(service)
    finally:
        await engine.dispose()


def _emit(value: Any) -> None:
    typer.echo(json.dumps(value, default=str, ensure_ascii=False, sort_keys=True))


def _failure() -> None:
    typer.echo("Error: run operation failed", err=True)
    raise typer.Exit(code=1)


@run_app.command("create")
def create_run(
    task_id: UUID = typer.Option(..., "--task-id"),
    idempotency_key: str = typer.Option(..., "--idempotency-key"),
    profile_id: UUID | None = typer.Option(None, "--profile-id"),
    profile_version: int | None = typer.Option(None, "--profile-version", min=1),
) -> None:
    """Create a run, optionally freezing a specific immutable profile version."""
    if (profile_id is None) != (profile_version is None):
        typer.echo("Error: profile identity and version must be supplied together", err=True)
        raise typer.Exit(code=1)
    try:
        async def create(service: RunService) -> dict[str, object]:
            run = await service.create_run(
                actor=LocalOperatorProfileActor(), idempotency_key=idempotency_key,
                task_id=task_id, profile_id=profile_id, profile_version=profile_version,
            )
            return {"id": str(run.id), "state": run.state.value,
                    "subscription_profile": await service.profile_selection(run.id)}
        _emit(asyncio.run(_run(create)))
    except Exception:
        _failure()


@run_app.command("show")
def show_run(run_id: UUID = typer.Option(..., "--run-id")) -> None:
    """Show a run and its frozen profile selection as JSON."""
    try:
        async def show(service: RunService) -> dict[str, object]:
            run = await service.get(run_id)
            return {"id": str(run.id), "project_id": str(run.project_id),
                    "task_id": str(run.task_id), "state": run.state.value,
                    "version": run.version,
                    "subscription_profile": await service.profile_selection(run.id)}
        _emit(asyncio.run(_run(show)))
    except Exception:
        _failure()
