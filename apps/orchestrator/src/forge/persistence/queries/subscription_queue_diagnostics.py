"""Observational queue diagnostics for subscription tasks and worker matching."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.run import RunState
from forge.domain.subscription import (
    ExecutionEnvelope,
    LogicalTaskContract,
    RouteSpec,
    SpecialistPurpose,
    decode_subscription_record,
)
from forge.domain.subscription_execution import run_allows_subscription_attempt
from forge.domain.subscription_quota import (
    QuotaPolicy,
    task_candidate_routes,
)
from forge.domain.subscription_readiness import (
    ReadinessReason,
    ReadinessWarning,
)
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import (
    SubscriptionEnvelope,
    SubscriptionTask,
)
from forge.persistence.models.subscription_runtime_status import (
    SubscriptionWorkerStatus,
)
from forge.persistence.repositories.subscription_quota import (
    PostgresSubscriptionQuotaRepository,
)
from forge.persistence.repositories.subscription_runtime_status import (
    RUNTIME_FRESH_SECONDS,
    _display_routes,
)

CLIENT_SETUP_PATH = "/subscription-profiles#client-setup"

_REASON_DESCRIPTIONS: dict[str, str] = {
    ReadinessReason.SIGNED_OUT.value: "client is signed out",
    ReadinessReason.MISSING_EXECUTABLE.value: "client executable is missing",
    ReadinessReason.EXECUTABLE_DIGEST_MISMATCH.value: "client build differs",
    ReadinessReason.VERSION_MISMATCH.value: "client version differs",
    ReadinessReason.UNSUPPORTED_MODEL_OR_EFFORT.value: "model or effort is unsupported",
    ReadinessReason.ACCOUNT_AUTHENTICATION_UNPROVED.value: "account authentication is unproved",
    ReadinessReason.SUBSCRIPTION_ROUTE_UNBOUND.value: "subscription route is unproved",
    ReadinessReason.ISOLATION_UNPROVED.value: "isolation is unproved",
    ReadinessReason.EVIDENCE_MISSING.value: "capability evidence is missing",
    ReadinessReason.EVIDENCE_STALE_OR_INVALID.value: "capability evidence is stale or invalid",
    ReadinessReason.PROVIDER_UNSUPPORTED.value: "provider client is unsupported",
    ReadinessReason.CONFIGURATION_INVALID.value: "installation configuration is invalid",
    ReadinessReason.QUOTA_EXHAUSTED.value: "retained quota is exhausted",
    ReadinessReason.UNKNOWN.value: "readiness is unknown",
}


def _route_wire(route: RouteSpec | dict[str, Any]) -> dict[str, str]:
    if isinstance(route, RouteSpec):
        return {
            "provider": route.provider,
            "client": route.client,
            "model": route.model,
            "effort": route.effort.value,
            "auth_mode": route.auth_mode.value,
            "billing_mode": route.billing_mode.value,
        }
    return {
        "provider": str(route.get("provider", "")),
        "client": str(route.get("client", "")),
        "model": str(route.get("model", "")),
        "effort": str(route.get("effort", "")),
        "auth_mode": str(route.get("auth_mode", "")),
        "billing_mode": str(route.get("billing_mode", "")),
    }


def _route_label(route: RouteSpec | dict[str, Any]) -> str:
    wire = _route_wire(route)
    effort = wire["effort"]
    effort_suffix = f" ({effort} effort)" if effort and effort != "none" else ""
    return f"{wire['provider']} / {wire['client']} / {wire['model']}{effort_suffix}"


def _guidance_for_reason(reason: str | None, client: str) -> str:
    if reason == ReadinessReason.SIGNED_OUT.value:
        if client in ("antigravity_cli", "gemini_cli"):
            return (
                "Sign in with Google inside the official Antigravity client, then refresh. "
                "The separate approved-tool warning still applies."
            )
        if client == "codex_app_server":
            return (
                "Run codex login in the official Codex client and select the intended "
                "personal ChatGPT account, then refresh."
            )
        if client == "claude_code":
            return (
                "Run claude auth login in the official Claude client, verify with "
                "claude auth status, then refresh."
            )
        return "Sign in using the official client, then refresh."
    if reason == ReadinessReason.MISSING_EXECUTABLE.value:
        return "Install the official client executable and ensure it is available in the worker's PATH."
    if reason == ReadinessReason.CONFIGURATION_INVALID.value:
        return (
            "Inspect the worker's installation manifest and configuration, correct any issues, "
            "and restart the worker."
        )
    if reason == ReadinessReason.QUOTA_EXHAUSTED.value:
        return (
            "Wait for the retained reset or next-probe time. Forge will not switch to "
            "API billing, credits, or a paid fallback."
        )
    if reason == ReadinessReason.UNSUPPORTED_MODEL_OR_EFFORT.value:
        return "Select a supported model and reasoning effort configured for this provider client."
    return f"Inspect worker registration and client setup at {CLIENT_SETUP_PATH}."


def _route_matches(task_route: RouteSpec, registered: dict[str, Any]) -> bool:
    return (
        registered.get("provider") == task_route.provider
        and registered.get("client") == task_route.client
        and registered.get("model") == task_route.model
        and registered.get("effort") == task_route.effort.value
        and registered.get("auth_mode") == task_route.auth_mode.value
        and registered.get("billing_mode") == task_route.billing_mode.value
    )


async def assess_queue_diagnostics(
    session_or_factory: AsyncSession | async_sessionmaker[AsyncSession],
    run_id: UUID,
    *,
    now: datetime | None = None,
    quota_policy: QuotaPolicy | None = None,
) -> tuple[dict[UUID, dict[str, Any] | None], dict[str, Any] | None]:
    if isinstance(session_or_factory, async_sessionmaker):
        async with session_or_factory() as session:
            return await _assess_queue_diagnostics(
                session, run_id, now=now, quota_policy=quota_policy
            )
    return await _assess_queue_diagnostics(
        session_or_factory, run_id, now=now, quota_policy=quota_policy
    )


async def _assess_queue_diagnostics(
    session: AsyncSession,
    run_id: UUID,
    *,
    now: datetime | None = None,
    quota_policy: QuotaPolicy | None = None,
) -> tuple[dict[UUID, dict[str, Any] | None], dict[str, Any] | None]:
    run = await session.get(Run, run_id)
    if run is None or run.state in (RunState.COMPLETED, RunState.CANCELLED, RunState.FAILED):
        return {}, None
    if not run_allows_subscription_attempt(run.state, run.pending_gate):
        return {}, None

    if now is None:
        db_now = await session.scalar(select(func.clock_timestamp()))
        now = db_now if isinstance(db_now, datetime) else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    policy = quota_policy or QuotaPolicy()
    quota_repo = PostgresSubscriptionQuotaRepository(
        session, policy=policy, clock=lambda: now
    )

    stored_envelope = await session.get(SubscriptionEnvelope, run_id)
    envelope = (
        decode_subscription_record(stored_envelope.payload)
        if stored_envelope is not None
        else None
    )
    if not isinstance(envelope, ExecutionEnvelope):
        envelope = None

    task_rows = list(
        (
            await session.execute(
                select(SubscriptionTask, SubscriptionScheduledTask)
                .outerjoin(
                    SubscriptionScheduledTask,
                    (
                        (SubscriptionScheduledTask.run_id == SubscriptionTask.run_id)
                        & (SubscriptionScheduledTask.task_id == SubscriptionTask.id)
                    ),
                )
                .where(SubscriptionTask.run_id == run_id)
                .order_by(SubscriptionTask.created_at, SubscriptionTask.id)
            )
        ).all()
    )
    if not task_rows:
        return {}, None

    # Assess every current worker without reading historical reports.
    current_workers = list(
        (
            await session.scalars(
                select(SubscriptionWorkerStatus)
                .where(
                    SubscriptionWorkerStatus.stopped_at.is_(None),
                    SubscriptionWorkerStatus.last_seen_at <= now,
                    SubscriptionWorkerStatus.last_seen_at
                    > now - timedelta(seconds=RUNTIME_FRESH_SECONDS),
                )
                .order_by(SubscriptionWorkerStatus.last_seen_at.desc())
            )
        ).all()
    )

    # Keep every registration for readiness matching: workers can disagree about
    # the same route. Only the displayed inventory is deduplicated and bounded.
    registered_routes: list[dict[str, Any]] = [
        route for worker in current_workers for route in _display_routes(worker.routes, stale=False)
    ]
    available_routes: list[dict[str, Any]] = []
    seen_route_keys: set[tuple[str, str, str, str, str, str]] = set()
    for r in registered_routes:
        key = (
            str(r.get("provider", "")),
            str(r.get("client", "")),
            str(r.get("model", "")),
            str(r.get("effort", "")),
            str(r.get("auth_mode", "")),
            str(r.get("billing_mode", "")),
        )
        if key not in seen_route_keys:
            seen_route_keys.add(key)
            available_routes.append(r)
    available_routes_wire = [_route_wire(r) for r in available_routes[:64]]

    scheduled_by_id = {row[0].id: row[1] for row in task_rows}
    task_diags: dict[UUID, dict[str, Any] | None] = {}
    primary_diag: dict[str, Any] | None = None

    for task_row, scheduled in task_rows:
        contract = decode_subscription_record(task_row.payload)
        if not isinstance(contract, LogicalTaskContract):
            continue

        task_state = scheduled.state if scheduled is not None else task_row.state
        is_queued = task_state == "queued"
        is_paused = task_row.pause_requested or (
            scheduled is not None and scheduled.pause_requested
        )
        is_cancelled = task_row.cancel_requested or (
            scheduled is not None and scheduled.cancel_requested
        )

        # Check dependency suppression: if task has unfinished dependencies, suppress
        has_pending_dep = False
        if contract.dependency_task_ids:
            for dep_id in contract.dependency_task_ids:
                dep_scheduled = scheduled_by_id.get(dep_id)
                if dep_scheduled is not None and dep_scheduled.state != "terminal":
                    has_pending_dep = True
                    break

        if not is_queued or is_paused or is_cancelled or has_pending_dep:
            task_diags[task_row.id] = None
            continue

        task_route = contract.route.effective
        route_wire = _route_wire(task_route)

        warnings: list[str] = []
        if task_route.client in ("antigravity_cli", "gemini_cli"):
            warnings.append(ReadinessWarning.APPROVED_TOOLS_UNPROVED.value)

        common = {
            "task_id": task_row.id,
            "purpose": contract.purpose.value,
            "route": route_wire,
            "warnings": warnings,
            "available_routes": available_routes_wire,
        }

        if not current_workers:
            diag = {
                **common,
                "status": "no_current_worker",
                "reason": None,
                "message": (
                    f"Queued {contract.purpose.value} task requires {_route_label(task_route)}, "
                    "but no Forge worker is currently reporting."
                ),
                "guidance": (
                    "Start the Forge worker to begin processing queued tasks. If you configured "
                    "clients in .env or an installations manifest, check that the worker process "
                    "is loading the intended manifest (for example, verify "
                    "FORGE_SUBSCRIPTION_INSTALLATIONS_PATH if set in the worker's environment)."
                ),
            }
        else:
            candidates = task_candidate_routes(contract, envelope)
            diag = None
            cand0_diagnostic = None

            for candidate in candidates:
                cand_route = candidate.route
                matching_routes = [
                    route for route in registered_routes if _route_matches(cand_route, route)
                ]

                # Preserve report ordering while accepting both admitted readiness reasons.
                ready_match = next(
                    (r for r in matching_routes if r.get("effective_reason") in (
                        ReadinessReason.READY.value, ReadinessReason.OPERATOR_TRUSTED.value
                    )),
                    None,
                )

                pool_status = await quota_repo.status(policy.key_for(cand_route))
                quota_eligible = pool_status.allows_attempt

                if ready_match is not None and quota_eligible:
                    eff_reason = ready_match.get("effective_reason")
                    ready_warnings = []
                    if cand_route.client in ("antigravity_cli", "gemini_cli"):
                        ready_warnings.append(ReadinessWarning.APPROVED_TOOLS_UNPROVED.value)
                    if (
                        eff_reason == ReadinessReason.OPERATOR_TRUSTED.value
                        and ReadinessWarning.OPERATOR_TRUSTED.value not in ready_warnings
                    ):
                        ready_warnings.append(ReadinessWarning.OPERATOR_TRUSTED.value)

                    if candidate.kind == "fallback":
                        message = (
                            f"A current worker has registered approved fallback route "
                            f"{_route_label(cand_route)} and is ready to attempt."
                        )
                    elif candidate.kind == "preferred":
                        message = (
                            f"A current worker has registered recovered preferred route "
                            f"{_route_label(cand_route)} and is ready to attempt."
                        )
                    else:
                        message = (
                            f"A current worker has registered {_route_label(cand_route)} "
                            "and is ready to attempt."
                        )

                    diag = {
                        **common,
                        "status": "matching_ready",
                        "route": _route_wire(cand_route),
                        "reason": eff_reason,
                        "message": message,
                        "guidance": (
                            "Worker registration is satisfied. Other scheduler conditions, "
                            "dependencies, quota, and capacity still apply."
                        ),
                        "warnings": ready_warnings,
                    }
                    break

                if candidate.kind == "current":
                    if not matching_routes:
                        routes_summary = (
                            " · ".join(
                                f"{r['provider']} / {r['client']} / {r['model']}"
                                for r in available_routes[:64]
                            )
                            if available_routes
                            else "None"
                        )
                        cand0_diagnostic = {
                            **common,
                            "status": "no_matching_registered_route",
                            "reason": None,
                            "message": (
                                f"Queued {contract.purpose.value} task requires {_route_label(task_route)}, "
                                "but no current worker has registered this route."
                            ),
                            "guidance": (
                                f"Current worker(s) registered: {routes_summary}. "
                                + (
                                    "Showing the first 64 routes. "
                                    if len(available_routes) > 64
                                    else ""
                                )
                                + "Check your worker configuration "
                                "and client setup. If you configured clients in .env or an installations manifest, "
                                "check that the worker process is loading the intended manifest (for example, verify "
                                "FORGE_SUBSCRIPTION_INSTALLATIONS_PATH if set in the worker's environment)."
                            ),
                        }
                    elif ready_match is not None and not quota_eligible:
                        unavail_reason = ReadinessReason.QUOTA_EXHAUSTED.value
                        reason_desc = _REASON_DESCRIPTIONS.get(unavail_reason, unavail_reason)
                        cand0_diagnostic = {
                            **common,
                            "status": "matched_unavailable",
                            "reason": unavail_reason,
                            "message": (
                                f"Queued {contract.purpose.value} task requires {_route_label(task_route)}. "
                                f"A current worker registered this route, but it is currently unavailable: {reason_desc}."
                            ),
                            "guidance": _guidance_for_reason(unavail_reason, task_route.client),
                        }
                    else:
                        first_match = matching_routes[0]
                        unavail_reason = first_match.get(
                            "effective_reason", ReadinessReason.UNKNOWN.value
                        )
                        reason_desc = _REASON_DESCRIPTIONS.get(unavail_reason, unavail_reason)
                        cand0_diagnostic = {
                            **common,
                            "status": "matched_unavailable",
                            "reason": unavail_reason,
                            "message": (
                                f"Queued {contract.purpose.value} task requires {_route_label(task_route)}. "
                                f"A current worker registered this route, but it is currently unavailable: {reason_desc}."
                            ),
                            "guidance": _guidance_for_reason(unavail_reason, task_route.client),
                        }

            if diag is None:
                diag = cand0_diagnostic

        task_diags[task_row.id] = diag
        if contract.purpose is SpecialistPurpose.PRIMARY:
            primary_diag = diag

    page_diag = primary_diag
    if page_diag is None:
        # Fall back to first unready diagnostic if no primary task had one
        page_diag = next(
            (d for d in task_diags.values() if d and d["status"] != "matching_ready"), None
        )

    return task_diags, page_diag
