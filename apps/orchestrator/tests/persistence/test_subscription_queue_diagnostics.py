"""Persistence integration tests for subscription queue worker diagnostics."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.api.schemas.subscription_tasks import SubscriptionTaskPage
from forge.domain.run import RunState
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    ExecutionEnvelope,
    LogicalTaskContract,
    OperatorProfile,
    ReasoningEffort,
    RolePreference,
    RouteBinding,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
)
from forge.domain.subscription_readiness import (
    ReadinessQuota,
    ReadinessReason,
    ReadinessWarning,
    SubscriptionRouteReadiness,
)
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import (
    SubscriptionEnvelope,
    SubscriptionProfileVersion,
    SubscriptionTask,
)
from forge.persistence.queries.subscription_queue_diagnostics import (
    assess_queue_diagnostics,
)
from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery
from forge.persistence.repositories.subscription_runtime_status import (
    SubscriptionRuntimeStatusStore,
)
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import delete


async def _clean_run(session_factory, run_id, profile_ids=()):
    async with session_factory() as session:
        await session.execute(
            delete(SubscriptionScheduledTask).where(SubscriptionScheduledTask.run_id == run_id)
        )
        await session.execute(
            delete(SubscriptionTask).where(SubscriptionTask.run_id == run_id)
        )
        await session.execute(
            delete(SubscriptionEnvelope).where(SubscriptionEnvelope.run_id == run_id)
        )
        if profile_ids:
            await session.execute(
                delete(SubscriptionProfileVersion).where(
                    SubscriptionProfileVersion.profile_id.in_(profile_ids)
                )
            )
        await session.commit()

GOOGLE_ROUTE = RouteSpec(
    provider="google",
    client="antigravity_cli",
    model="gemini-3.8-flash-medium",
    effort=ReasoningEffort.MEDIUM,
    auth_mode=AuthMode.SUBSCRIPTION,
    billing_mode=BillingMode.ALLOWANCE_ONLY,
)

CODEX_ROUTE = RouteSpec(
    provider="openai",
    client="codex_app_server",
    model="gpt-5",
    effort=ReasoningEffort.NONE,
    auth_mode=AuthMode.SUBSCRIPTION,
    billing_mode=BillingMode.ALLOWANCE_ONLY,
)

MUSE_ROUTE = RouteSpec(
    provider="muse",
    client="muse_client",
    model="muse-chat",
    effort=ReasoningEffort.NONE,
    auth_mode=AuthMode.SUBSCRIPTION,
    billing_mode=BillingMode.ALLOWANCE_ONLY,
)


async def _create_test_run_with_task(
    session_factory,
    persisted_run,
    *,
    route=GOOGLE_ROUTE,
    purpose=SpecialistPurpose.PRIMARY,
    state="queued",
    pause_requested=False,
    cancel_requested=False,
):
    binding = RouteBinding(requested=route, effective=route)
    profile = OperatorProfile(
        profile_id=uuid4(),
        version=1,
        preferences=(RolePreference(purpose=purpose, preferred_route=route),),
    )
    task = LogicalTaskContract(
        run_id=persisted_run.id,
        task_id=uuid4(),
        purpose=purpose,
        route=binding,
        budget=TaskBudget(),
        owned_paths=("src",),
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription.store_profile(profile)
        await work.subscription.freeze_envelope(
            ExecutionEnvelope(
                run_id=task.run_id,
                profile_id=profile.profile_id,
                profile_version=1,
                safety_policy_version=1,
                routes=((purpose, binding),),
            )
        )
        await work.subscription.create_task(task, idempotency_key=f"queue-diag-{task.task_id}")
        await work.commit()

    async with session_factory() as session:
        session.add(
            SubscriptionScheduledTask(
                id=uuid4(),
                run_id=task.run_id,
                task_id=task.task_id,
                worktree_id="diag-worktree",
                provider=route.provider,
                state=state,
                owned_paths=["src"],
                pause_requested=pause_requested,
                cancel_requested=cancel_requested,
            )
        )
        await session.commit()
    return task, profile


@pytest.mark.integration
async def test_diagnostics_distinguish_no_current_worker(session_factory, persisted_run):
    task, profile = await _create_test_run_with_task(session_factory, persisted_run)
    try:
        query = SubscriptionTaskQuery(session_factory)
        result = await query.tasks(persisted_run.id)
        page = SubscriptionTaskPage.model_validate(result)

        assert page.queue_diagnostic is not None
        diag = page.queue_diagnostic
        assert diag.status == "no_current_worker"
        assert diag.task_id == task.task_id
        assert diag.purpose == "primary"
        assert diag.route.provider == "google"
        assert diag.route.client == "antigravity_cli"
        assert diag.route.model == "gemini-3.8-flash-medium"
        assert "no Forge worker is currently reporting" in diag.message
        assert "Start the Forge worker" in diag.guidance
        assert "FORGE_SUBSCRIPTION_INSTALLATIONS_PATH" in diag.guidance
        assert ReadinessWarning.APPROVED_TOOLS_UNPROVED.value in diag.warnings

        # Check task-level diagnostic matches
        assert page.tasks[0].queue_diagnostic is not None
        assert page.tasks[0].queue_diagnostic.status == "no_current_worker"
    finally:
        await _clean_run(session_factory, persisted_run.id, (profile.profile_id,))


@pytest.mark.integration
async def test_diagnostics_distinguish_no_matching_registered_route(
    session_factory, persisted_run
):
    """Worker has Codex and Muse registered, but task requested Google Antigravity."""
    _, profile = await _create_test_run_with_task(session_factory, persisted_run)
    worker_id = uuid4()
    store = SubscriptionRuntimeStatusStore(session_factory)
    await store.report(
        worker_id,
        (
            SubscriptionRouteReadiness(
                CODEX_ROUTE, True, True, ReadinessReason.READY, quota=ReadinessQuota.ELIGIBLE
            ),
            SubscriptionRouteReadiness(
                MUSE_ROUTE, True, True, ReadinessReason.READY, quota=ReadinessQuota.ELIGIBLE
            ),
        ),
    )
    try:
        query = SubscriptionTaskQuery(session_factory)
        result = await query.tasks(persisted_run.id)
        page = SubscriptionTaskPage.model_validate(result)

        assert page.queue_diagnostic is not None
        diag = page.queue_diagnostic
        assert diag.status == "no_matching_registered_route"
        assert "no current worker has registered this route" in diag.message
        assert "codex" in diag.guidance and "muse" in diag.guidance
        assert "FORGE_SUBSCRIPTION_INSTALLATIONS_PATH" in diag.guidance
        assert len(diag.available_routes) == 2
    finally:
        await store.stop(worker_id)
        await _clean_run(session_factory, persisted_run.id, (profile.profile_id,))


@pytest.mark.integration
@pytest.mark.parametrize("override", [
    {"effort": ReasoningEffort.LOW},
    {"billing_mode": BillingMode.PAID_OPT_IN},
])
async def test_diagnostics_route_mismatch_on_effort_or_billing(
    session_factory, persisted_run, override
):
    """Registration must match both effort and billing policy."""
    _, profile = await _create_test_run_with_task(session_factory, persisted_run)
    worker_id = uuid4()
    store = SubscriptionRuntimeStatusStore(session_factory)
    # Register a route with a different effort or billing policy.
    mismatched_route = replace(GOOGLE_ROUTE, **override)
    await store.report(
        worker_id,
        (
            SubscriptionRouteReadiness(
                mismatched_route, True, True, ReadinessReason.READY, quota=ReadinessQuota.ELIGIBLE
            ),
        ),
    )
    try:
        query = SubscriptionTaskQuery(session_factory)
        result = await query.tasks(persisted_run.id)
        page = SubscriptionTaskPage.model_validate(result)

        # A different effort or billing policy does not match.
        assert page.queue_diagnostic is not None
        assert page.queue_diagnostic.status == "no_matching_registered_route"
    finally:
        await store.stop(worker_id)
        await _clean_run(session_factory, persisted_run.id, (profile.profile_id,))


@pytest.mark.integration
async def test_diagnostics_matched_unavailable_and_operator_trusted_ready(
    session_factory, persisted_run
):
    """Worker registered route but signed out; then becomes operator_trusted (ready)."""
    _, profile = await _create_test_run_with_task(session_factory, persisted_run)
    worker_id = uuid4()
    store = SubscriptionRuntimeStatusStore(session_factory)

    # 1. Signed out
    await store.report(
        worker_id,
        (
            SubscriptionRouteReadiness(
                GOOGLE_ROUTE,
                True,
                False,
                ReadinessReason.SIGNED_OUT,
                quota=ReadinessQuota.UNKNOWN,
            ),
        ),
    )
    try:
        query = SubscriptionTaskQuery(session_factory)
        result = await query.tasks(persisted_run.id)
        page = SubscriptionTaskPage.model_validate(result)

        assert page.queue_diagnostic is not None
        diag = page.queue_diagnostic
        assert diag.status == "matched_unavailable"
        assert diag.reason == "signed_out"
        assert "currently unavailable" in diag.message
        assert "Sign in with Google" in diag.guidance

        # 2. Recovered to operator_trusted with quota unknown (ready to attempt)
        await store.report(
            worker_id,
            (
                SubscriptionRouteReadiness(
                    GOOGLE_ROUTE,
                    True,
                    True,
                    ReadinessReason.OPERATOR_TRUSTED,
                    quota=ReadinessQuota.UNKNOWN,
                ),
            ),
        )
        recovered_result = await query.tasks(persisted_run.id)
        recovered_page = SubscriptionTaskPage.model_validate(recovered_result)

        assert recovered_page.queue_diagnostic is not None
        rec_diag = recovered_page.queue_diagnostic
        assert rec_diag.status == "matching_ready"
        assert ReadinessWarning.APPROVED_TOOLS_UNPROVED.value in rec_diag.warnings
        assert ReadinessWarning.OPERATOR_TRUSTED.value in rec_diag.warnings
    finally:
        await store.stop(worker_id)
        await _clean_run(session_factory, persisted_run.id, (profile.profile_id,))


@pytest.mark.integration
async def test_diagnostics_multiple_workers_and_stale_or_stopped_isolation(
    session_factory, persisted_run
):
    """Stale and stopped workers are ignored; ready worker wins over unavailable worker."""
    _, profile = await _create_test_run_with_task(session_factory, persisted_run)
    now = datetime(2026, 10, 16, 20, 0, 0, tzinfo=UTC)

    # Worker 1: stopped
    w1 = uuid4()
    # Worker 2: stale (seen 60 seconds ago)
    w2 = uuid4()
    # Worker 3: current (seen 5 seconds ago) with signed_out
    w3 = uuid4()

    clock = lambda: now
    store = SubscriptionRuntimeStatusStore(session_factory, clock=clock)

    # w1 reports and stops
    await store.report(
        w1,
        (
            SubscriptionRouteReadiness(
                GOOGLE_ROUTE, True, True, ReadinessReason.READY, quota=ReadinessQuota.ELIGIBLE
            ),
        ),
    )
    await store.stop(w1)

    # w2 reports in the past
    past_clock = lambda: now - timedelta(seconds=60)
    past_store = SubscriptionRuntimeStatusStore(session_factory, clock=past_clock)
    await past_store.report(
        w2,
        (
            SubscriptionRouteReadiness(
                GOOGLE_ROUTE, True, True, ReadinessReason.READY, quota=ReadinessQuota.ELIGIBLE
            ),
        ),
    )

    # w3 reports current, but signed_out
    await store.report(
        w3,
        (
            SubscriptionRouteReadiness(
                GOOGLE_ROUTE,
                True,
                False,
                ReadinessReason.SIGNED_OUT,
                quota=ReadinessQuota.UNKNOWN,
            ),
        ),
    )

    try:
        result = await assess_queue_diagnostics(
            session_factory, persisted_run.id, now=now
        )
        _, page_diag = result
        assert page_diag is not None
        assert page_diag["status"] == "matched_unavailable"
        assert page_diag["reason"] == "signed_out"

        # Worker 4: arrives current and ready -> ready wins!
        w4 = uuid4()
        await store.report(
            w4,
            (
                SubscriptionRouteReadiness(
                    GOOGLE_ROUTE, True, True, ReadinessReason.READY, quota=ReadinessQuota.ELIGIBLE
                ),
            ),
        )

        res2 = await assess_queue_diagnostics(
            session_factory, persisted_run.id, now=now
        )
        _, page_diag2 = res2
        assert page_diag2 is not None
        assert page_diag2["status"] == "matching_ready"
        await store.stop(w4)
    finally:
        await store.stop(w2)
        await store.stop(w3)
        await _clean_run(session_factory, persisted_run.id, (profile.profile_id,))


@pytest.mark.integration
async def test_diagnostics_suppression_on_nonqueued_paused_cancelled_or_terminal(
    session_factory, persisted_run
):
    """Diagnostics suppress when task is leased, paused, cancelled, or run is terminal."""
    # 1. Leased task
    _, p1 = await _create_test_run_with_task(
        session_factory, persisted_run, state="leased"
    )
    query = SubscriptionTaskQuery(session_factory)
    res_leased = await query.tasks(persisted_run.id)
    assert res_leased["queue_diagnostic"] is None
    assert res_leased["tasks"][0]["queue_diagnostic"] is None
    await _clean_run(session_factory, persisted_run.id, (p1.profile_id,))

    # 2. Paused task
    _, p2 = await _create_test_run_with_task(
        session_factory, persisted_run, state="queued", pause_requested=True
    )
    res_paused = await query.tasks(persisted_run.id)
    assert res_paused["queue_diagnostic"] is None
    assert res_paused["tasks"][0]["queue_diagnostic"] is None
    await _clean_run(session_factory, persisted_run.id, (p2.profile_id,))

    # 3. Cancel-requested task
    _, p3 = await _create_test_run_with_task(
        session_factory, persisted_run, state="queued", cancel_requested=True
    )
    res_cancelled = await query.tasks(persisted_run.id)
    assert res_cancelled["queue_diagnostic"] is None
    assert res_cancelled["tasks"][0]["queue_diagnostic"] is None
    await _clean_run(session_factory, persisted_run.id, (p3.profile_id,))

    # 4. Terminal run
    _, p4 = await _create_test_run_with_task(
        session_factory, persisted_run, state="queued"
    )
    async with session_factory() as session:
        run = await session.get(Run, persisted_run.id)
        run.state = RunState.COMPLETED
        await session.commit()

    res_terminal = await query.tasks(persisted_run.id)
    assert res_terminal["queue_diagnostic"] is None
    assert res_terminal["tasks"][0]["queue_diagnostic"] is None

    # Clean up
    await _clean_run(session_factory, persisted_run.id, (p4.profile_id,))


@pytest.mark.integration
async def test_inventory_bound_does_not_hide_a_matching_worker(session_factory, persisted_run):
    """Serialize a bounded inventory but match against every current registration."""
    _, profile = await _create_test_run_with_task(session_factory, persisted_run)
    store = SubscriptionRuntimeStatusStore(session_factory)
    worker_id = uuid4()
    other_worker = uuid4()
    routes = [replace(CODEX_ROUTE, model=f"model-{index}") for index in range(64)]
    await store.report(worker_id, tuple(
        SubscriptionRouteReadiness(route, True, True, ReadinessReason.READY)
        for route in routes
    ))
    await store.report(other_worker, (SubscriptionRouteReadiness(
        replace(CODEX_ROUTE, model="other-model"), True, True, ReadinessReason.READY
    ),))
    try:
        query = SubscriptionTaskQuery(session_factory)
        page = SubscriptionTaskPage.model_validate(await query.tasks(persisted_run.id))
        assert page.queue_diagnostic is not None
        assert len(page.queue_diagnostic.available_routes) <= 64
        assert page.queue_diagnostic.status == "no_matching_registered_route"
        await store.report(other_worker, (
            SubscriptionRouteReadiness(GOOGLE_ROUTE, True, True, ReadinessReason.READY),
        ))
        recovered = SubscriptionTaskPage.model_validate(await query.tasks(persisted_run.id))
        assert recovered.queue_diagnostic is not None
        assert recovered.queue_diagnostic.status == "matching_ready"
    finally:
        await store.stop(other_worker)
        await store.stop(worker_id)
        await _clean_run(session_factory, persisted_run.id, (profile.profile_id,))
