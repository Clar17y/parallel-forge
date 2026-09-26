"""Local operator command reporting recorded search-ranking measurements."""

from __future__ import annotations

# Typer declares CLI arguments as call defaults by design.
# ruff: noqa: B008
import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict
from typing import Any, cast
from uuid import UUID

import typer

from forge.application.ports.tools import ToolCallRecord
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.ranking.measurement import (
    SearchRankingMeasurement,
    format_measurements,
    measure_search_ranking,
)
from forge.settings import Settings

ranking_app = typer.Typer(add_completion=False, no_args_is_help=True)


@ranking_app.command("report")
def report(
    run_id: UUID = typer.Argument(..., help="Run whose recorded searches are summarized."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable totals."),
) -> None:
    """Summarize how much search context ranking delivered or withheld.

    Run the same fixtures with FORGE_SEARCH_RANKING_MODE set to off, shadow,
    and on, then compare the reports for each run.
    """

    measurements = asyncio.run(_measure(run_id))
    if as_json:
        typer.echo(json.dumps([_row(item) for item in measurements], indent=2, sort_keys=True))
        return
    typer.echo(format_measurements(measurements))


def _row(item: SearchRankingMeasurement) -> dict[str, object]:
    return {
        **asdict(item),
        "matches_withheld": item.matches_withheld,
        "delivered_fraction": round(item.delivered_fraction, 4),
        "projected_fraction": round(item.projected_fraction, 4),
    }


async def _measure(run_id: UUID) -> tuple[SearchRankingMeasurement, ...]:
    settings = Settings(process_role="cli")
    engine = create_engine(settings.database_url)
    try:
        session_factory = create_session_factory(engine)
        factory = cast(Callable[[], Any], lambda: PostgresUnitOfWork(session_factory))
        async with factory() as work:
            records: Sequence[ToolCallRecord] = await work.tool_calls.list_for_run(run_id)
        return measure_search_ranking(records)
    finally:
        await engine.dispose()


__all__ = ["ranking_app"]
