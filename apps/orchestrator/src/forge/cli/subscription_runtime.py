"""Local operator reads of worker registration; this command launches no client."""

import asyncio

import typer

from forge.api.schemas.subscription_runtime import SubscriptionRuntimeStatusPage
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.repositories.subscription_runtime_status import (
    SubscriptionRuntimeStatusStore,
)
from forge.settings import Settings

runtime_app = typer.Typer(add_completion=False, no_args_is_help=True)


async def _status(offset: int, limit: int) -> SubscriptionRuntimeStatusPage:
    settings = Settings(process_role="cli")
    engine = create_engine(settings.database_url)
    try:
        query = SubscriptionRuntimeStatusStore(create_session_factory(engine))
        return SubscriptionRuntimeStatusPage.model_validate(
            await query.status(offset=offset, limit=limit)
        )
    finally:
        await engine.dispose()


@runtime_app.command("status")
def status(
    offset: int = typer.Option(0, "--offset", min=0, max=1_000_000),
    limit: int = typer.Option(25, "--limit", min=1, max=100),
) -> None:
    """Show current, stale or stopped worker registration snapshots as JSON."""
    try:
        value = asyncio.run(_status(offset, limit))
        typer.echo(value.model_dump_json())
    except Exception:  # noqa: BLE001 - never echo connection or configuration values
        typer.echo("subscription runtime status unavailable", err=True)
        raise typer.Exit(1) from None
