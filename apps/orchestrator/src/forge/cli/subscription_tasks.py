"""Local operator task controls use the same audited application service as HTTP."""

# Typer defaults describe the CLI; errors never echo request or connection values.
# ruff: noqa: B008, BLE001
import asyncio
from enum import StrEnum
from uuid import UUID

import typer
from sqlalchemy.ext.asyncio import AsyncEngine

from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.application.services.subscription_task_controls import SubscriptionTaskControlService
from forge.domain.subscription_task_controls import (
    SubscriptionTaskControlRequest,
    TaskControlConflict,
    TaskControlReceipt,
)
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.repositories.mutations import MutationConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings

task_app = typer.Typer(add_completion=False, no_args_is_help=True)


class ControlAction(StrEnum):
    PAUSE = "pause"
    CANCEL = "cancel"
    RESUME = "resume"


def _service(engine: AsyncEngine, settings: Settings) -> SubscriptionTaskControlService:
    sessions = create_session_factory(engine)
    return SubscriptionTaskControlService(
        lambda: PostgresUnitOfWork(sessions, quota_policy=settings.subscription_quota_policy)
    )


async def _execute(
    run_id: UUID, task_id: UUID, key: str, request: SubscriptionTaskControlRequest
) -> TaskControlReceipt:
    settings = Settings(process_role="cli")
    engine = create_engine(settings.database_url)
    try:
        return await _service(engine, settings).control(
            run_id=run_id,
            task_id=task_id,
            actor=LocalOperatorProfileActor(),
            idempotency_key=key,
            request=request,
        )
    finally:
        await engine.dispose()


@task_app.command("control")
def control_task(
    action: ControlAction = typer.Option(..., "--action"),
    run_id: UUID = typer.Option(..., "--run-id"),
    task_id: UUID = typer.Option(..., "--task-id"),
    expected_run_version: int = typer.Option(..., "--expected-run-version", min=0),
    expected_task_version: int = typer.Option(..., "--expected-task-version", min=0),
    reason: str = typer.Option(..., "--reason"),
    idempotency_key: str = typer.Option(..., "--idempotency-key"),
    pause_receipt_id: UUID | None = typer.Option(None, "--pause-receipt-id"),
) -> None:
    """Pause, cancel or resume one specialist task with exact observed versions."""
    try:
        request = SubscriptionTaskControlRequest(
            action=action.value,
            expected_run_version=expected_run_version,
            expected_task_version=expected_task_version,
            reason=reason,
            pause_receipt_id=pause_receipt_id,
        )
        result = asyncio.run(_execute(run_id, task_id, idempotency_key, request))
        typer.echo(result.model_dump_json())
    except Exception as error:
        if isinstance(error, (TaskControlConflict, MutationConflict)):
            message = "task control conflicts with current state; inspect the task before retrying"
        elif isinstance(error, (TypeError, ValueError)):
            message = "task control request rejected"
        else:
            message = "task control could not be confirmed; retry the same request and key"
        typer.echo(f"Error: {message}", err=True)
        raise typer.Exit(code=1) from None


__all__ = ["task_app"]
