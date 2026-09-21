"""Forge operator CLI entry point."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

import typer

from forge.application.services.auth import AuthService, AuthUnitOfWork
from forge.cli.evaluations import eval_app
from forge.cli.search_ranking import ranking_app
from forge.cli.subscription_capabilities import capability_app
from forge.cli.subscription_profiles import profile_app
from forge.cli.subscription_quota import quota_app
from forge.cli.subscription_runtime import runtime_app
from forge.cli.subscription_tasks import task_app
from forge.cli.worktrees import worktree_app
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings

app = typer.Typer(add_completion=False, no_args_is_help=True)
operator_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(operator_app, name="operator")
app.add_typer(worktree_app, name="worktree")
app.add_typer(eval_app, name="eval")
app.add_typer(profile_app, name="profile")
app.add_typer(quota_app, name="subscription-quota")
app.add_typer(runtime_app, name="subscription-runtime")
app.add_typer(task_app, name="subscription-tasks")
app.add_typer(capability_app, name="subscription-capabilities")
app.add_typer(ranking_app, name="search-ranking")


@app.callback()
def main() -> None:
    """Run Forge operator commands."""


@app.command()
def status() -> None:
    """Report that the local Forge CLI is available."""

    Settings(process_role="cli")
    typer.echo("Forge CLI is ready.")


@operator_app.command("rotate")
def rotate_operator() -> None:
    """Revoke current local credentials and print one fresh bootstrap URL."""

    settings = Settings(process_role="cli")
    token = asyncio.run(_rotate(settings))
    typer.echo(f"{settings.web_origin}/#bootstrap={token}")


async def _rotate(settings: Settings) -> str:
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        factory = cast(
            Callable[[], AuthUnitOfWork],
            lambda: PostgresUnitOfWork(session_factory),
        )
        service = AuthService(factory)
        return await service.rotate()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    app()
