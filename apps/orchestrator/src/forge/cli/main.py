"""Forge operator CLI entry point."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

import typer

from forge.application.services.auth import AuthService, AuthUnitOfWork
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings

app = typer.Typer(add_completion=False, no_args_is_help=True)
operator_app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(operator_app, name="operator")


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
