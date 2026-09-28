"""Forge operator CLI entry point."""

from __future__ import annotations

import asyncio
import json
import math
import time
import urllib.request
import webbrowser
from collections.abc import Awaitable, Callable
from typing import cast

import typer

from forge.api.security import parse_web_origin
from forge.application.services.auth import AuthService, AuthUnitOfWork
from forge.cli.evaluations import eval_app
from forge.cli.jev import jev_app
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
app.add_typer(jev_app, name="jev")


@app.callback()
def main() -> None:
    """Run Forge operator commands."""


@app.command()
def status() -> None:
    """Report that the local Forge CLI is available."""

    Settings(process_role="cli")
    typer.echo("Forge CLI is ready.")


def _validate_timeout(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise typer.BadParameter("timeout must be a finite positive number.")
    return value


def _operator_settings() -> Settings:
    """Validate the credential destination before probing or issuing any token."""
    try:
        settings = Settings(process_role="cli")
        parse_web_origin(settings.web_origin)
    except Exception:  # noqa: BLE001 - configuration failures expose no credentials
        typer.echo(
            "Could not load Forge configuration. Check the local environment settings.",
            err=True,
        )
        raise typer.Exit(1) from None
    return settings


def wait_for_dashboard(settings: Settings, timeout: float = 30.0) -> None:
    """Poll the local web origin /api/health endpoint until the API reports ready."""

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")

    parse_web_origin(settings.web_origin)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            req_timeout = min(2.0, remaining)
            with opener.open(
                f"{settings.web_origin}/api/health",
                timeout=req_timeout,
            ) as response:
                health = json.loads(response.read(4096))
                if (
                    response.status == 200
                    and isinstance(health, dict)
                    and health.get("status") == "ok"
                    and health.get("role") == "api"
                ):
                    return
        except (OSError, ValueError):
            pass
        sleep_time = min(0.25, max(0.0, deadline - time.monotonic()))
        if sleep_time <= 0:
            break
        time.sleep(sleep_time)
    raise TimeoutError("Forge dashboard is not ready")


@operator_app.command("open")
def open_operator(
    print_url: bool = typer.Option(
        False,
        "--print-url",
        "--headless",
        help="Print the one-time sign-in URL instead of opening a browser.",
    ),
    wait: bool = typer.Option(
        True,
        "--wait/--no-wait",
        help="Wait for the dashboard to become ready before opening or issuing the URL.",
    ),
    timeout: float = typer.Option(
        30.0,
        "--timeout",
        callback=_validate_timeout,
        help="Maximum seconds to wait for dashboard readiness.",
    ),
) -> None:
    """Open Forge in a browser with a fresh, non-revoking sign-in link."""

    _validate_timeout(timeout)
    settings = _operator_settings()

    if wait:
        try:
            wait_for_dashboard(settings, timeout=timeout)
        except Exception:  # noqa: BLE001 - dashboard probe failures are sanitized
            typer.echo(
                "Forge is not ready. Start its services and check the local service logs.",
                err=True,
            )
            raise typer.Exit(1) from None

    try:
        token = asyncio.run(_issue_bootstrap(settings))
    except Exception:  # noqa: BLE001 - persistence failures expose no credentials or DSNs
        typer.echo(
            "Could not create the local sign-in link. Check the Forge database connection.",
            err=True,
        )
        raise typer.Exit(1) from None

    url = f"{settings.web_origin}/#bootstrap={token}"

    if print_url:
        typer.echo(url)
        return

    opened = False
    try:
        opened = bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001 - browser invocation errors fall back to safe print guidance
        opened = False

    if not opened:
        typer.echo(
            "The browser could not open. Run this helper with --print-url to get a five-minute sign-in link.",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo("Opened Forge with a fresh sign-in link. Existing sessions remain valid.")


@operator_app.command("rotate")
def rotate_operator() -> None:
    """Revoke current local credentials and print one fresh bootstrap URL."""

    settings = _operator_settings()

    try:
        token = asyncio.run(_rotate(settings))
    except Exception:  # noqa: BLE001 - persistence failures expose no credentials or DSNs
        typer.echo(
            "Could not rotate operator credentials. Check the Forge database connection.",
            err=True,
        )
        raise typer.Exit(1) from None
    typer.echo(f"{settings.web_origin}/#bootstrap={token}")


async def _with_auth_service[T](
    settings: Settings,
    action: Callable[[AuthService], Awaitable[T]],
) -> T:
    """Execute an action with AuthService and ensure engine disposal."""
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        factory = cast(
            Callable[[], AuthUnitOfWork],
            lambda: PostgresUnitOfWork(session_factory),
        )
        service = AuthService(factory)
        return await action(service)
    finally:
        await engine.dispose()


async def _issue_bootstrap(settings: Settings) -> str:
    return await _with_auth_service(settings, lambda service: service.issue_bootstrap())


async def _rotate(settings: Settings) -> str:
    return await _with_auth_service(settings, lambda service: service.rotate())


if __name__ == "__main__":
    app()
