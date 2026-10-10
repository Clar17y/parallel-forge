"""Local owner commands for project monitoring policy revisions."""

from __future__ import annotations

# Typer's command defaults and boundary exception conversion are intentional.
# ruff: noqa: B008, BLE001
import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import typer
from pydantic import ValidationError

from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.application.services.task_usage_policy import (
    MonitoringPolicyRevisionRequest,
    TaskUsagePolicyService,
)
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings

monitoring_app = typer.Typer(add_completion=False, no_args_is_help=True)


async def _run(operation: Callable[[TaskUsagePolicyService], Any]) -> Any:
    settings = Settings(process_role="cli")
    engine = create_engine(settings.database_url)
    try:
        session_factory = create_session_factory(engine)
        factory = cast(Callable[[], Any], lambda: PostgresUnitOfWork(session_factory))
        return await operation(TaskUsagePolicyService(factory))
    finally:
        await engine.dispose()


def _input(path: Path) -> MonitoringPolicyRevisionRequest:
    raw = path.read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError("monitoring policy input is too large")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for name, item in pairs:
            if name in value:
                raise ValueError("duplicate monitoring policy field")
            value[name] = item
        return value

    def reject_nonfinite(_value: str) -> None:
        raise ValueError("non-finite number")

    document = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique,
        parse_constant=reject_nonfinite,
    )
    return MonitoringPolicyRevisionRequest.model_validate(document)


def _emit(value: Any) -> None:
    typer.echo(json.dumps(None if value is None else asdict(value), default=str, sort_keys=True))


def _failure(error: Exception) -> None:
    if isinstance(
        error, (ValueError, ValidationError, json.JSONDecodeError, UnicodeError, OSError)
    ):
        message = "monitoring policy input is invalid"
    else:
        message = "monitoring policy operation failed"
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


@monitoring_app.command("show")
def show(
    project_id: UUID = typer.Option(..., "--project-id"),
    revision: int | None = typer.Option(None, "--revision", min=1),
) -> None:
    """Show the current or a historical monitoring revision; null means unconfigured."""
    try:
        _emit(asyncio.run(_run(lambda service: service.get(project_id, revision))))
    except Exception as error:
        _failure(error)


@monitoring_app.command("revise")
def revise(
    project_id: UUID = typer.Option(..., "--project-id"),
    input_file: Path = typer.Option(..., "--file", exists=True, dir_okay=False),
    idempotency_key: str = typer.Option(..., "--idempotency-key"),
) -> None:
    """Append an owner revision with an expected version and idempotency key."""
    try:
        request = _input(input_file)
        _emit(
            asyncio.run(
                _run(
                    lambda service: service.revise(
                        actor=LocalOperatorProfileActor(),
                        project_id=project_id,
                        idempotency_key=idempotency_key,
                        request=request,
                    )
                )
            )
        )
    except Exception as error:
        _failure(error)


__all__ = ["monitoring_app"]
