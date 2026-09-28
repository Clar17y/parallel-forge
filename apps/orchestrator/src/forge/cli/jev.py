"""Read-only operator reporting for Jev usage."""

from __future__ import annotations

# Typer's declarative arguments and options are call defaults by design.
# ruff: noqa: B008
import asyncio
import json
from uuid import UUID

import typer

from forge.application.services.jev_reporting import JevReportingService
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings

jev_app = typer.Typer(add_completion=False, no_args_is_help=True)


@jev_app.command("report")
def report(
    run_id: UUID = typer.Argument(..., help="Run whose Jev activity is summarized."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable report."),
) -> None:
    value = asyncio.run(_report(run_id))
    if value is None:
        typer.echo("Run not found.", err=True)
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(value, indent=2, sort_keys=True))
    else:
        typer.echo(
            f"Jev run {run_id}: configured {value['requested_mode']}, effective {value['effective_mode']} "
            f"({value['availability']}); model {value['requested_model'] or 'legacy global'}, "
            f"reported {value['actual_model'] or 'not yet observed'}; {value['attempts']} attempts, "
            f"{value['calls']} provider calls, "
            f"{value['cache_hits']} cache hits, {value['unknown']} unknown outcomes, "
            f"{value['actual_input_units']} reported input tokens, {value['duration_ms']} ms; "
            f"{value['remaining_requests']} requests and {value['remaining_input_units']} input allowance remaining."
        )
        diagnostics = value.get("by_diagnostic")
        if isinstance(diagnostics, dict):
            for diagnostic, count in sorted(diagnostics.items()):
                typer.echo(f"  {diagnostic}: {count}")


async def _report(run_id: UUID) -> dict[str, object] | None:
    settings = Settings(process_role="cli")
    engine = create_engine(settings.database_url)
    try:
        service = JevReportingService(lambda: PostgresUnitOfWork(create_session_factory(engine)))
        return await service.report(run_id)
    finally:
        await engine.dispose()
