"""Audited local operator commands for shared subscription quota pools."""

from __future__ import annotations

# Typer's declarative defaults and boundary conversion are intentional.
# ruff: noqa: BLE001
import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import typer
from pydantic import ValidationError

from forge.api.schemas.subscription_quota import QuotaExhaustionReportRequest
from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.application.services.subscription_quota import SubscriptionQuotaService
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings

quota_app = typer.Typer(add_completion=False, no_args_is_help=True)


class _SetupError(RuntimeError):
    def __init__(self, engine: Any) -> None:
        super().__init__("quota operation setup failed")
        self.engine = engine


def _service() -> tuple[SubscriptionQuotaService, Any]:
    settings = Settings(process_role="cli")
    engine = create_engine(settings.database_url)
    try:
        session_factory = create_session_factory(engine)
        factory = cast(
            Callable[[], Any],
            lambda: PostgresUnitOfWork(
                session_factory, quota_policy=settings.subscription_quota_policy
            ),
        )
        return SubscriptionQuotaService(factory), engine
    except BaseException as error:
        raise _SetupError(engine) from error


async def _run(operation: Callable[[SubscriptionQuotaService], Any]) -> Any:
    try:
        service, engine = _service()
    except _SetupError as error:
        await error.engine.dispose()
        raise
    try:
        return await operation(service)
    finally:
        await engine.dispose()


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


@quota_app.command("list")
def list_quota(
    offset: int = typer.Option(0, "--offset", min=0, max=1_000_000),
    limit: int = typer.Option(100, "--limit", min=1, max=100),
) -> None:
    """List one page of shared provider quota pools as JSON."""
    try:
        values = asyncio.run(_run(lambda service: service.list(offset=offset, limit=limit)))
        typer.echo(json.dumps(_jsonable(values), sort_keys=True))
    except Exception as error:
        _failure(error)


@quota_app.command("report-exhaustion")
def report_exhaustion(
    provider: str = typer.Option(..., "--provider"),
    account: str = typer.Option(..., "--account"),
    pool: str = typer.Option(..., "--pool"),
    reason: str = typer.Option(..., "--reason"),
    reset_at: str | None = typer.Option(None, "--reset-at"),
    idempotency_key: str = typer.Option(..., "--idempotency-key"),
) -> None:
    """Record provider exhaustion evidence for one explicit pool."""
    try:
        parsed_reset = None
        if reset_at is not None:
            parsed_reset = datetime.fromisoformat(reset_at)
            if parsed_reset.tzinfo is None or parsed_reset.utcoffset() is None:
                raise ValueError("reset_at must include a timezone")
            parsed_reset = parsed_reset.astimezone(UTC)
        body = QuotaExhaustionReportRequest(
            provider=provider,
            account=account,
            pool=pool,
            reason=reason,
            reset_at=parsed_reset,
        )
        value = asyncio.run(
            _run(
                lambda service: service.report_exhaustion(
                    actor=LocalOperatorProfileActor(),
                    idempotency_key=idempotency_key,
                    request=body,
                )
            )
        )
        typer.echo(json.dumps(_jsonable(value), sort_keys=True))
    except Exception as error:
        _failure(error)


def _failure(error: Exception) -> None:
    if isinstance(error, (ValidationError, ValueError, TypeError)):
        message = "quota request rejected"
    else:
        message = "quota operation failed"
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


__all__ = ["quota_app"]
