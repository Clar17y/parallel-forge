"""Privileged local operator commands for immutable subscription profiles."""

from __future__ import annotations

# Typer's declarative defaults and command-boundary exception conversion are intentional.
# ruff: noqa: B008, BLE001
import asyncio
import json
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import typer
from pydantic import ValidationError

from forge.application.services.subscription_profiles import (
    LocalOperatorProfileActor,
    ProfileBody,
    ProfileVersionRequest,
    ProjectProfileSelectionRequest,
    SubscriptionProfileService,
    SubscriptionProfileServiceError,
)
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings

profile_app = typer.Typer(add_completion=False, no_args_is_help=True)
_MAX_INPUT_BYTES = 256 * 1024


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite numbers are not allowed")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON fields are not allowed")
        result[key] = value
    return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _emit(value: Any) -> None:
    typer.echo(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True))


def _read_input(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            raise ValueError("input file exceeds the 256 KiB limit")
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=_reject_constant
        )
        if not isinstance(value, dict):
            raise TypeError("input must be one JSON object")
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise typer.BadParameter("invalid profile input") from None


class _SetupError(RuntimeError):
    def __init__(self, engine: Any) -> None:
        super().__init__("profile operation setup failed")
        self.engine = engine


def _service() -> tuple[SubscriptionProfileService, Any]:
    settings = Settings(process_role="cli")
    engine = create_engine(settings.database_url)
    try:
        session_factory = create_session_factory(engine)
        factory = cast(Callable[[], Any], lambda: PostgresUnitOfWork(session_factory))
        return SubscriptionProfileService(factory), engine
    except BaseException as error:
        raise _SetupError(engine) from error


async def _run(operation: Callable[[SubscriptionProfileService], Any]) -> Any:
    try:
        service, engine = _service()
    except _SetupError as error:
        try:
            await error.engine.dispose()
        finally:
            raise
    try:
        return await operation(service)
    finally:
        await engine.dispose()


def _failure(error: Exception) -> None:
    if isinstance(error, typer.BadParameter):
        message = "invalid profile input"
    elif isinstance(error, ValidationError):
        message = "profile input failed schema validation"
    elif isinstance(error, SubscriptionProfileServiceError):
        message = "profile mutation rejected"
    elif isinstance(error, (ValueError, KeyError)):
        message = "profile request rejected"
    else:
        message = "profile operation failed"
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


@profile_app.command("list")
def list_profiles() -> None:
    """List all immutable profile versions as JSON."""
    try:
        _emit(asyncio.run(_run(lambda service: service.list())))
    except Exception as error:
        _failure(error)


@profile_app.command("show")
def show_profile(
    profile_id: UUID = typer.Option(..., "--profile-id"),
    version: int = typer.Option(..., "--version", min=1),
) -> None:
    """Show one immutable profile version as JSON."""
    try:
        _emit(asyncio.run(_run(lambda service: service.get(profile_id, version))))
    except Exception as error:
        _failure(error)


@profile_app.command("create")
def create_profile(
    input_file: Path = typer.Option(..., "--file", "--input-file", exists=True, dir_okay=False),
    idempotency_key: str = typer.Option(..., "--idempotency-key"),
) -> None:
    """Create profile version 1 from a closed-schema JSON file."""
    try:
        request = ProfileBody.model_validate(_read_input(input_file))
        _emit(asyncio.run(_run(lambda service: service.create(
            actor=LocalOperatorProfileActor(), idempotency_key=idempotency_key, request=request
        ))))
    except Exception as error:
        _failure(error)


@profile_app.command("append")
def append_profile(
    profile_id: UUID = typer.Option(..., "--profile-id"),
    input_file: Path = typer.Option(..., "--file", "--input-file", exists=True, dir_okay=False),
    idempotency_key: str = typer.Option(..., "--idempotency-key"),
) -> None:
    """Append the next immutable version with an expected current version."""
    try:
        request = ProfileVersionRequest.model_validate(_read_input(input_file))
        _emit(asyncio.run(_run(lambda service: service.append(
            actor=LocalOperatorProfileActor(), profile_id=profile_id,
            idempotency_key=idempotency_key, request=request
        ))))
    except Exception as error:
        _failure(error)


@profile_app.command("project-show")
def project_show(project_id: UUID = typer.Option(..., "--project-id")) -> None:
    """Show the profile currently selected for a project, or null."""
    try:
        _emit(asyncio.run(_run(lambda service: service.selected(project_id))))
    except Exception as error:
        _failure(error)


@profile_app.command("project-select")
def project_select(
    project_id: UUID = typer.Option(..., "--project-id"),
    input_file: Path = typer.Option(..., "--file", "--input-file", exists=True, dir_okay=False),
    idempotency_key: str = typer.Option(..., "--idempotency-key"),
) -> None:
    """Select a profile for a project using paired expected identity fields."""
    try:
        request = ProjectProfileSelectionRequest.model_validate(_read_input(input_file))
        _emit(asyncio.run(_run(lambda service: service.select(
            actor=LocalOperatorProfileActor(), project_id=project_id,
            idempotency_key=idempotency_key, request=request
        ))))
    except Exception as error:
        _failure(error)


__all__ = ["profile_app"]
