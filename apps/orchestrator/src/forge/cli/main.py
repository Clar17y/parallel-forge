"""Forge operator CLI entry point."""

import typer

from forge.settings import Settings

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.callback()
def main() -> None:
    """Run Forge operator commands."""


@app.command()
def status() -> None:
    """Report that the local Forge CLI is available."""

    Settings(process_role="cli")
    typer.echo("Forge CLI is ready.")
