"""Local operator reads of worker registration; this command launches no client."""

import asyncio
from enum import StrEnum
from typing import Annotated

import typer

from forge.api.schemas.subscription_runtime import (
    SubscriptionRuntimeRouteView,
    SubscriptionRuntimeStatusPage,
)
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.repositories.subscription_runtime_status import (
    SubscriptionRuntimeStatusStore,
)
from forge.settings import Settings

runtime_app = typer.Typer(add_completion=False, no_args_is_help=True)


class StatusFormat(StrEnum):
    JSON = "json"
    TEXT = "text"


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
    output_format: Annotated[StatusFormat, typer.Option("--format")] = StatusFormat.JSON,
) -> None:
    """Show current, stale or stopped subscription readiness snapshots."""
    try:
        value = asyncio.run(_status(offset, limit))
        typer.echo(
            value.model_dump_json() if output_format is StatusFormat.JSON else _render_text(value)
        )
    except Exception:  # noqa: BLE001 - never echo connection or configuration values
        typer.echo("subscription runtime status unavailable", err=True)
        raise typer.Exit(1) from None


def _render_text(value: SubscriptionRuntimeStatusPage) -> str:
    lines = [
        f"observed={value.observed_at.isoformat()} fresh_for_seconds={value.fresh_for_seconds}",
        "Read-only retained metadata; this command launches no provider client and reserves no quota.",
    ]
    if not value.workers:
        lines.append("No worker reports; readiness is unknown.")
    for worker in value.workers:
        lines.append(
            f"worker={worker.worker_instance_id} state={worker.state} "
            f"last_seen={worker.last_seen_at.isoformat()}"
        )
        if not worker.routes:
            lines.append("  No routes in this report.")
        for route in worker.routes:
            lines.extend(_render_route(route))
    if value.has_more:
        lines.append("More worker reports exist; inspect the next page before drawing conclusions.")
    return "\n".join(lines)


def _render_route(route: SubscriptionRuntimeRouteView) -> list[str]:
    lines = [
        f"  {route.provider} / {route.client} / {route.model} / {route.effort}",
        (
            f"    configured={'yes' if route.configured else 'no'} "
            f"admitted={'yes' if route.admitted else 'no'} "
            f"state={route.effective_reason}"
        ),
        f"    action={_guidance(route)}",
    ]
    if route.quota == "unknown":
        lines.append("    quota=unknown; not a zero-balance or availability claim")
    elif route.quota == "eligible":
        lines.append("    quota=eligible-to-attempt; not a remaining-allowance balance")
    else:
        suffix = []
        if route.quota_reset_at is not None:
            suffix.append(f"reset={route.quota_reset_at.isoformat()}")
        if route.quota_next_probe_at is not None:
            suffix.append(f"next_probe={route.quota_next_probe_at.isoformat()}")
        lines.append("    quota=blocked" + (" " + " ".join(suffix) if suffix else ""))
    if route.evidence:
        lines.extend(
            f"    evidence={item.scope} id={item.evidence_id} revision {item.revision} "
            f"observed={item.observed_at.isoformat()} expires={item.expires_at.isoformat()}"
            for item in route.evidence
        )
    else:
        lines.append("    evidence=none")
    return lines


def _guidance(route: SubscriptionRuntimeRouteView) -> str:
    reason = route.effective_reason
    if reason == "ready":
        return "Capability evidence matches; invocation still rechecks authority."
    if reason == "missing_executable":
        return "Install the configured official client, then restart or refresh the worker."
    if reason in {"executable_digest_mismatch", "version_mismatch"}:
        return "Restore the pinned official-client build or update the manifest and publish fresh evidence."
    if reason == "unsupported_model_or_effort":
        return "Choose a supported model and effort or revise the exact installation."
    if reason in {
        "signed_out",
        "account_authentication_unproved",
        "subscription_route_unbound",
    }:
        if route.client == "codex_app_server":
            return "Run `codex login` in Codex with the intended personal ChatGPT account, then refresh."
        if route.client == "claude_code":
            return "Run `claude auth login`, verify with `claude auth status`, then refresh."
        if route.client in {"antigravity_cli", "gemini_cli"}:
            return "Sign in with Google inside Antigravity, then refresh; sign-in does not prove isolation."
        return "Complete sign-in inside the configured official client, then refresh."
    if reason == "isolation_unproved":
        return "Keep blocked until an authorized capability workflow proves complete isolation."
    if reason == "evidence_missing":
        return (
            "Run subscription-capabilities status and verify offline, then explicitly authorize "
            "bounded evidence publication."
        )
    if reason == "evidence_stale_or_invalid":
        return "Inspect the installation and publish fresh bounded evidence."
    if reason == "provider_unsupported":
        return "Choose an admitted official client; sign-in alone cannot enable this route."
    if reason == "configuration_invalid":
        return "Correct the closed installation manifest and exact quota mapping, then restart."
    if reason == "stale_worker":
        return "Start or restart the Forge worker and wait for a current report."
    if reason == "quota_exhausted":
        return "Wait for the retained reset or next-probe time; no paid fallback is selected."
    return "Inspect the installation and refresh; unknown never grants admission."
