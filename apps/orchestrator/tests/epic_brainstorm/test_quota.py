import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import forge.persistence.repositories.epic_brainstorm as repository_module
import pytest
from forge.application.services.epic_brainstorm import EpicBrainstormService
from forge.domain.subscription import RouteBinding, RouteSpec, TaskBudget, UnknownTelemetryPolicy
from forge.domain.subscription_quota import QuotaPolicy
from forge.persistence.models.epic_brainstorm import BrainstormAttemptRow, BrainstormJobRow
from forge.persistence.models.subscription_quota import (
    SubscriptionQuotaObservation,
)
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository
from forge.worker.epic_brainstorm import EpicBrainstormWorker
from sqlalchemy import delete, select

from apps.orchestrator.tests.epic_brainstorm.test_worker import BriefFixture, FakeGateway, prepared


async def _submit_route_job(factory, epic_id, project_id, actor, budget, model, suffix):
    route = RouteSpec(provider="fake", client="fake", model=model)
    service = EpicBrainstormService(
        factory,
        lambda _: BriefFixture(epic_id, project_id),
        route=RouteBinding(requested=route, effective=route),
        budget=budget,
    )
    conversation_id, version = await service.create(
        epic_id=epic_id,
        project_id=project_id,
        actor=actor,
        key=f"create-{suffix}",
        text="Another idea",
    )
    turn = (
        await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    )[0]
    receipt = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key=f"submit-{suffix}",
    )
    return service, receipt


@pytest.mark.asyncio
async def test_unproved_changed_route_cannot_share_held_cost_under_concurrent_claims(
    brainstorm_session_factory,
) -> None:
    budget = TaskBudget(max_provider_attempts=4, max_cost_minor=100)
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory, budget=budget
    )
    _, second = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "other", "other"
    )

    async def claim(owner):
        async with brainstorm_session_factory() as session, session.begin():
            result = await PostgresBrainstormRepository(session).claim(owner)
            return result[0].id if result else None

    await asyncio.gather(claim("currency-a"), claim("currency-b"))
    async with brainstorm_session_factory() as session:
        attempts = (await session.scalars(select(BrainstormAttemptRow))).all()
        assert len(attempts) == 1
        assert attempts[0].reservation["estimated_api_cost_minor"] == 100
    first_outcome = await service.observe(
        epic_id=epic_id, project_id=project_id, job_id=first.job_id
    )
    second_outcome = await service.observe(
        epic_id=epic_id, project_id=project_id, job_id=second.job_id
    )
    assert {first_outcome.state, second_outcome.state} == {"running", "failed"}
    assert (
        next(item.failure for item in (first_outcome, second_outcome) if item.state == "failed")
        == "input_conflict"
    )
    held = next(item for item in (first_outcome, second_outcome) if item.state == "running")
    assert held.currency is None
    assert held.cumulative_usage.estimated_api_cost_minor == 0
    assert held.held_reservations.estimated_api_cost_minor == 0
    assert held.held_reasons.estimated_api_cost_minor == "unsettled_or_unknown"


@pytest.mark.asyncio
async def test_same_route_retry_and_queued_sibling_keep_observed_cost_unit(
    brainstorm_session_factory,
) -> None:
    budget = TaskBudget(max_provider_attempts=4, max_cost_minor=100)
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory, budget=budget
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("currency-first")
        assert claimed is not None
        job, attempt = claimed
        attempt.process_settled = True
        attempt.usage_known = True
        attempt.usage = {
            "duration_ms": 10,
            "tool_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_api_cost_minor": 7,
            "currency": "USD",
        }
        job.state, job.failure = "failed", "unavailable"
        job.version += 1
    _, sibling = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "fixture", "sibling"
    )
    queued = await service.observe(epic_id=epic_id, project_id=project_id, job_id=sibling.job_id)
    assert queued.currency == "USD"
    assert queued.cumulative_usage.estimated_api_cost_minor == 7
    failed = await service.observe(epic_id=epic_id, project_id=project_id, job_id=first.job_id)
    await service.retry(
        epic_id=epic_id,
        project_id=project_id,
        job_id=first.job_id,
        expected_job_version=failed.job_version,
        actor=actor,
        key="same-unit-retry",
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("currency-retry")
        assert claimed is not None
        job, attempt = claimed
        assert job.id == first.job_id
        assert attempt.reservation["estimated_api_cost_minor"] == 93
    active = await service.observe(epic_id=epic_id, project_id=project_id, job_id=first.job_id)
    assert active.currency == "USD"
    assert active.cumulative_usage.estimated_api_cost_minor == 7
    assert active.held_reservations.estimated_api_cost_minor == 93


@pytest.mark.asyncio
@pytest.mark.parametrize("second_currency", ("USD", "EUR", None))
async def test_historical_route_unit_proof_or_conflict(
    brainstorm_session_factory,
    second_currency,
) -> None:
    budget = TaskBudget(max_provider_attempts=4, max_cost_minor=100)
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory, budget=budget
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("currency-first")
        assert claimed is not None
        job, attempt = claimed
        attempt.process_settled = True
        attempt.usage_known = True
        attempt.usage = {
            "duration_ms": 10,
            "tool_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_api_cost_minor": 7,
            "currency": "USD",
        }
        job.state, job.failure = "failed", "unavailable"
    _, second = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "other", "second"
    )
    async with brainstorm_session_factory() as session, session.begin():
        row = await session.get(BrainstormJobRow, second.job_id)
        assert row is not None
        seeded_id = uuid4()
        session.add(
            BrainstormAttemptRow(
                id=seeded_id,
                job_id=row.id,
                number=1,
                state="settled",
                owner="historical",
                fence=uuid4(),
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                launch_intent=False,
                process_started=False,
                process_settled=True,
                reservation={"estimated_api_cost_minor": 0},
                usage_known=True,
                usage={
                    "duration_ms": 10,
                    "tool_call_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "estimated_api_cost_minor": 5,
                    "currency": second_currency,
                },
                tool_calls_used=0,
            )
        )
        row.current_attempt_id = seeded_id
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("currency-second")
        if second_currency == "USD":
            assert claimed is not None
            job, attempt = claimed
            assert job.id == second.job_id
            assert attempt.reservation["estimated_api_cost_minor"] == 88
        else:
            assert claimed is None
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=first.job_id)
    if second_currency == "USD":
        assert outcome.currency == "USD"
        assert outcome.cumulative_usage.estimated_api_cost_minor == 12
    else:
        assert outcome.currency is None
        assert outcome.cumulative_usage.estimated_api_cost_minor == 0
        assert outcome.held_reasons.estimated_api_cost_minor == "unsettled_or_unknown"


@pytest.mark.asyncio
async def test_exact_zero_cost_does_not_bind_future_route_currency(
    brainstorm_session_factory,
) -> None:
    budget = TaskBudget(max_provider_attempts=3, max_cost_minor=100)
    _, epic_id, project_id, actor, _ = await prepared(brainstorm_session_factory, budget=budget)
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("zero-cost")
        assert claimed is not None
        job, attempt = claimed
        attempt.process_settled = True
        attempt.usage_known = True
        attempt.usage = {
            "duration_ms": 10,
            "tool_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_api_cost_minor": 0,
            "currency": "USD",
        }
        job.state = "failed"
    _, second = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "other", "zero-next"
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("new-route")
        assert claimed is not None and claimed[0].id == second.job_id
        assert claimed[1].reservation["estimated_api_cost_minor"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("next_model", ("fixture", "other"))
async def test_settled_never_launched_zero_cost_releases_money_without_unit(
    brainstorm_session_factory, next_model
) -> None:
    budget = TaskBudget(max_provider_attempts=3, max_cost_minor=100)
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory, budget=budget
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("zero-no-launch")
        assert claimed is not None
        job, attempt = claimed
        attempt_id, fence = attempt.id, attempt.fence
        assert not attempt.launch_intent and not attempt.process_settled
    await service.cancel(
        epic_id=epic_id,
        project_id=project_id,
        job_id=first.job_id,
        expected_job_version=job.version,
        actor=actor,
        key=f"cancel-zero-{next_model}",
    )
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="zero-no-launch",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    await worker._settle_not_launched(first.job_id, attempt_id, fence, "cancelled")
    async with brainstorm_session_factory() as session:
        stored = await session.get(BrainstormAttemptRow, attempt_id)
        assert stored is not None and stored.process_settled and not stored.launch_intent
        assert stored.usage_known and stored.usage["estimated_api_cost_minor"] == 0
        assert stored.usage["currency"] is None
    settled = await service.observe(epic_id=epic_id, project_id=project_id, job_id=first.job_id)
    assert settled.currency is None
    assert settled.usage_known is True
    assert settled.usage is not None and settled.usage.estimated_api_cost_minor == 0
    assert settled.held_reservations.estimated_api_cost_minor == 0
    _, second = await _submit_route_job(
        brainstorm_session_factory,
        epic_id,
        project_id,
        actor,
        budget,
        next_model,
        f"zero-no-launch-{next_model}",
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("after-zero")
        assert claimed is not None and claimed[0].id == second.job_id
        assert claimed[1].reservation["estimated_api_cost_minor"] == 100


@pytest.mark.asyncio
async def test_unproved_settled_zero_keeps_cost_hold(brainstorm_session_factory) -> None:
    budget = TaskBudget(max_provider_attempts=3, max_cost_minor=100)
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory, budget=budget
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("uncertain-zero")
        assert claimed is not None
        job, attempt = claimed
        attempt.process_settled = True
        attempt.usage_known = False
        attempt.usage = {"estimated_api_cost_minor": 0, "currency": "USD"}
        job.state = "failed"
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=first.job_id)
    assert outcome.usage_known is False
    assert outcome.held_reservations.estimated_api_cost_minor == 0
    assert outcome.held_reasons.estimated_api_cost_minor == "unsettled_or_unknown"
    _, second = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "fixture", "uncertain-zero"
    )
    async with brainstorm_session_factory() as session, session.begin():
        assert await PostgresBrainstormRepository(session).claim("after-uncertain-zero") is None
    denied = await service.observe(epic_id=epic_id, project_id=project_id, job_id=second.job_id)
    assert (denied.state, denied.failure) == ("failed", "budget_exhausted")


@pytest.mark.asyncio
@pytest.mark.parametrize("observed_currency", ("USD", "EUR", None))
async def test_unsettled_cost_unit_is_observed_without_releasing_its_hold(
    brainstorm_session_factory, observed_currency
) -> None:
    budget = TaskBudget(max_provider_attempts=4, max_cost_minor=100)
    service, epic_id, project_id, actor, _ = await prepared(
        brainstorm_session_factory, budget=budget
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("prior-usd")
        assert claimed is not None
        job, attempt = claimed
        attempt.process_settled = True
        attempt.usage_known = True
        attempt.usage = {
            "duration_ms": 1,
            "tool_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_api_cost_minor": 7,
            "currency": "USD",
        }
        job.state = "failed"
    _, second = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "fixture", "partial"
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("partial")
        assert claimed is not None and claimed[0].id == second.job_id
        claimed[1].usage_known = False
        claimed[1].usage = {
            "duration_ms": None,
            "tool_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_api_cost_minor": 5,
            "currency": observed_currency,
        }
    current = await service.observe(epic_id=epic_id, project_id=project_id, job_id=second.job_id)
    async with brainstorm_session_factory() as session:
        attempts = (await session.scalars(select(BrainstormAttemptRow))).all()
        assert sum(item.reservation["estimated_api_cost_minor"] for item in attempts) == 193
    if observed_currency == "USD":
        assert current.currency == "USD"
        assert current.held_reservations.estimated_api_cost_minor == 93
        assert current.usage is not None and current.usage.estimated_api_cost_minor == 5
    else:
        assert current.currency is None
        assert current.held_reservations.estimated_api_cost_minor == 0
        assert current.usage is not None and current.usage.estimated_api_cost_minor is None
        assert current.held_reasons.estimated_api_cost_minor == "unsettled_or_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_currency,allowed", (("USD", True), ("EUR", False), (None, False)))
async def test_unsettled_currency_evidence_fences_next_admission(
    brainstorm_session_factory, partial_currency, allowed
) -> None:
    budget = TaskBudget(max_provider_attempts=4, max_cost_minor=100)
    service, epic_id, project_id, actor, _ = await prepared(
        brainstorm_session_factory, budget=budget
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("settled-usd")
        assert claimed is not None
        job, attempt = claimed
        attempt.process_settled = True
        attempt.usage_known = True
        attempt.usage = {
            "duration_ms": 10,
            "tool_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_api_cost_minor": 7,
            "currency": "USD",
        }
        job.state = "failed"
    _, partial_job = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "fixture", "unit-partial"
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("partial-unit")
        assert claimed is not None and claimed[0].id == partial_job.job_id
        attempt = claimed[1]
        attempt.reservation = {
            **{key: 0 for key in attempt.reservation},
            "estimated_api_cost_minor": 20,
        }
        attempt.usage_known = True
        attempt.usage = {"estimated_api_cost_minor": 5, "currency": partial_currency}
    _, next_job = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "fixture", "unit-next"
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("after-partial-unit")
        assert (claimed is not None) is allowed
        if claimed is not None:
            assert claimed[0].id == next_job.job_id
            assert claimed[1].reservation["estimated_api_cost_minor"] == 73
    if not allowed:
        denied = await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=next_job.job_id
        )
        assert (denied.state, denied.failure) == ("failed", "input_conflict")


@pytest.mark.asyncio
@pytest.mark.parametrize("observed_cost,hold", ((0, 20), (None, 20), (0, None), (None, None)))
async def test_zero_or_missing_partial_cost_still_conflicts_with_prior_unit(
    brainstorm_session_factory, observed_cost, hold
) -> None:
    budget = TaskBudget(
        max_provider_attempts=4,
        max_cost_minor=100,
        unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=4),
    )
    service, epic_id, project_id, actor, _ = await prepared(
        brainstorm_session_factory, budget=budget
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("unit-prior")
        assert claimed is not None
        job, attempt = claimed
        attempt.process_settled = True
        attempt.usage_known = True
        attempt.usage = {
            "duration_ms": 10,
            "tool_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_api_cost_minor": 7,
            "currency": "USD",
        }
        job.state = "failed"
    _, current = await _submit_route_job(
        brainstorm_session_factory,
        epic_id,
        project_id,
        actor,
        budget,
        "fixture",
        "unit-zero-partial",
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("unit-zero-partial")
        assert claimed is not None and claimed[0].id == current.job_id
        attempt = claimed[1]
        attempt_id, fence = attempt.id, attempt.fence
        attempt.reservation = {
            **{key: 0 for key in attempt.reservation},
            "estimated_api_cost_minor": hold,
        }
        attempt.usage_known = True
        attempt.usage = {"estimated_api_cost_minor": observed_cost, "currency": "EUR"}
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=current.job_id)
    assert outcome.currency is None
    assert outcome.usage_known is False
    assert outcome.usage is not None and outcome.usage.estimated_api_cost_minor is None
    assert outcome.held_reservations.estimated_api_cost_minor == 0
    assert outcome.held_reasons.estimated_api_cost_minor == "unsettled_or_unknown"
    _, denied_receipt = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "fixture", "unit-denied"
    )
    async with brainstorm_session_factory() as session, session.begin():
        assert await PostgresBrainstormRepository(session).claim("unit-denied") is None
    denied = await service.observe(
        epic_id=epic_id, project_id=project_id, job_id=denied_receipt.job_id
    )
    assert (denied.state, denied.failure) == ("failed", "input_conflict")
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="unit-settlement",
        gateway_factory=lambda _: FakeGateway(),
        reader_factory=lambda _: object(),
    )
    await worker._settle_not_launched(current.job_id, attempt_id, fence, "interrupted")
    settled = await service.observe(epic_id=epic_id, project_id=project_id, job_id=current.job_id)
    assert settled.process_settled and settled.currency == "USD"
    assert settled.cumulative_usage.estimated_api_cost_minor == 7
    assert settled.held_reservations.estimated_api_cost_minor == 0
    _, admitted_receipt = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "fixture", "unit-after"
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("unit-after")
        assert claimed is not None and claimed[0].id == admitted_receipt.job_id
        assert claimed[1].reservation["estimated_api_cost_minor"] == 93


@pytest.mark.asyncio
async def test_matching_zero_observation_preserves_prior_unit_and_finite_hold(
    brainstorm_session_factory,
) -> None:
    budget = TaskBudget(max_provider_attempts=4, max_cost_minor=100)
    service, epic_id, project_id, actor, _ = await prepared(
        brainstorm_session_factory, budget=budget
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("matched-prior")
        assert claimed is not None
        job, attempt = claimed
        attempt.process_settled = True
        attempt.usage_known = True
        attempt.usage = {
            "duration_ms": 10,
            "tool_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_api_cost_minor": 7,
            "currency": "USD",
        }
        job.state = "failed"
    _, partial = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "fixture", "matched-zero"
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("matched-zero")
        assert claimed is not None and claimed[0].id == partial.job_id
        attempt = claimed[1]
        attempt.reservation = {
            **{key: 0 for key in attempt.reservation},
            "estimated_api_cost_minor": 20,
        }
        attempt.usage_known = True
        attempt.usage = {"estimated_api_cost_minor": 0, "currency": "USD"}
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=partial.job_id)
    assert outcome.currency == "USD"
    assert outcome.cumulative_usage.estimated_api_cost_minor == 7
    assert outcome.held_reservations.estimated_api_cost_minor == 20
    _, sibling = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "fixture", "matched-next"
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("matched-next")
        assert claimed is not None and claimed[0].id == sibling.job_id
        assert claimed[1].reservation["estimated_api_cost_minor"] == 73


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hold,reported_unit", ((20, "USD"), (20, None), (None, "USD"), (None, None))
)
@pytest.mark.parametrize("observed_cost", (0, None))
async def test_no_prior_unit_exposure_pins_only_its_route(
    brainstorm_session_factory, hold, reported_unit, observed_cost
) -> None:
    budget = TaskBudget(
        max_provider_attempts=4,
        max_cost_minor=100 if hold is not None else None,
        unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=4),
    )
    service, epic_id, project_id, actor, _ = await prepared(
        brainstorm_session_factory, budget=budget
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("unit-provisional")
        assert claimed is not None
        _, attempt = claimed
        attempt.reservation = {
            **{key: 0 for key in attempt.reservation},
            "estimated_api_cost_minor": hold,
        }
        attempt.usage_known = True
        attempt.usage = {"estimated_api_cost_minor": observed_cost, "currency": reported_unit}
    first = await service.observe(epic_id=epic_id, project_id=project_id, job_id=attempt.job_id)
    assert first.currency is None
    assert first.held_reservations.estimated_api_cost_minor == 0
    assert first.held_reasons.estimated_api_cost_minor == "unsettled_or_unknown"
    _, same = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "fixture", "unit-same"
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("unit-same")
        assert claimed is not None and claimed[0].id == same.job_id
        if hold is not None:
            assert claimed[1].reservation["estimated_api_cost_minor"] == 80
    _, other = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "other", "unit-switch"
    )
    async with brainstorm_session_factory() as session, session.begin():
        assert await PostgresBrainstormRepository(session).claim("unit-switch") is None
    denied = await service.observe(epic_id=epic_id, project_id=project_id, job_id=other.job_id)
    assert (denied.state, denied.failure) == ("failed", "input_conflict")


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_reserve", (None, "missing", "invalid", "invalid-unit"))
async def test_unknown_or_invalid_cost_reservation_cannot_claim_finite_budget(
    brainstorm_session_factory, raw_reserve
) -> None:
    budget = TaskBudget(
        max_provider_attempts=3,
        max_cost_minor=100,
        unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=3),
    )
    service, epic_id, project_id, actor, _ = await prepared(
        brainstorm_session_factory, budget=budget
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("unit-legacy")
        assert claimed is not None
        _, attempt = claimed
        reservation = {key: 0 for key in attempt.reservation}
        if raw_reserve == "invalid":
            reservation["estimated_api_cost_minor"] = -1
        elif raw_reserve == "invalid-unit":
            reservation["estimated_api_cost_minor"] = 20
        elif raw_reserve is None:
            reservation["estimated_api_cost_minor"] = None
        else:
            reservation.pop("estimated_api_cost_minor")
        attempt.reservation = reservation
        attempt.usage_known = True
        attempt.usage = {
            "estimated_api_cost_minor": 0,
            "currency": "usd" if raw_reserve == "invalid-unit" else "USD",
        }
    first = await service.observe(epic_id=epic_id, project_id=project_id, job_id=attempt.job_id)
    assert first.currency is None
    assert first.held_reasons.estimated_api_cost_minor == "unsettled_or_unknown"
    _, sibling = await _submit_route_job(
        brainstorm_session_factory,
        epic_id,
        project_id,
        actor,
        budget,
        "fixture",
        "unit-legacy-next",
    )
    async with brainstorm_session_factory() as session, session.begin():
        assert await PostgresBrainstormRepository(session).claim("unit-legacy-next") is None
    denied = await service.observe(epic_id=epic_id, project_id=project_id, job_id=sibling.job_id)
    assert (denied.state, denied.failure) == ("failed", "input_conflict")


@pytest.mark.asyncio
async def test_legacy_held_cost_on_unproved_route_masks_projection_and_blocks_admission(
    brainstorm_session_factory,
) -> None:
    budget = TaskBudget(max_provider_attempts=4, max_cost_minor=100)
    service, epic_id, project_id, actor, first = await prepared(
        brainstorm_session_factory, budget=budget
    )
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("known-usd")
        assert claimed is not None
        job, attempt = claimed
        attempt.process_settled = True
        attempt.usage_known = True
        attempt.usage = {
            "duration_ms": 10,
            "tool_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_api_cost_minor": 7,
            "currency": "USD",
        }
        job.state = "failed"
    _, second = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "other", "held-other"
    )
    async with brainstorm_session_factory() as session, session.begin():
        row = await session.get(BrainstormJobRow, second.job_id)
        assert row is not None
        pending_id = uuid4()
        session.add(
            BrainstormAttemptRow(
                id=pending_id,
                job_id=row.id,
                number=1,
                state="admitted",
                owner="historical",
                fence=uuid4(),
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                launch_intent=False,
                process_started=False,
                process_settled=False,
                reservation={"estimated_api_cost_minor": 20},
                usage_known=False,
                usage=None,
                tool_calls_used=0,
            )
        )
        row.current_attempt_id = pending_id
        row.state = "running"
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=first.job_id)
    assert outcome.currency is None
    assert outcome.cumulative_usage.estimated_api_cost_minor == 0
    assert outcome.held_reservations.estimated_api_cost_minor == 0
    assert outcome.held_reasons.estimated_api_cost_minor == "unsettled_or_unknown"
    _, third = await _submit_route_job(
        brainstorm_session_factory, epic_id, project_id, actor, budget, "fixture", "third"
    )
    async with brainstorm_session_factory() as session, session.begin():
        assert await PostgresBrainstormRepository(session).claim("third-worker") is None
    denied = await service.observe(epic_id=epic_id, project_id=project_id, job_id=third.job_id)
    assert (denied.state, denied.failure) == ("failed", "input_conflict")


@pytest.mark.asyncio
async def test_successful_current_revision_probe_recovers_with_allowed_unknown_cost(
    brainstorm_session_factory,
):
    _, _, _, _, receipt = await prepared(brainstorm_session_factory)
    async with brainstorm_session_factory() as session, session.begin():
        row = await session.get(BrainstormJobRow, receipt.job_id)
        repository = PostgresBrainstormRepository(session)
        snapshot = repository.decode_snapshot(row)
        pool = await repository.quota_pool(snapshot)
        pool.blocked = True
        pool.revision = 3
        pool.reason = "usage_exhausted"
        pool.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
    async with brainstorm_session_factory() as session, session.begin():
        repository = PostgresBrainstormRepository(session)
        claimed = await repository.claim("worker-a")
        assert claimed is not None
        job, attempt = claimed
        attempt.process_settled = True
        attempt.usage_known = False
        attempt.usage = {"input_tokens": 4, "output_tokens": 5, "estimated_api_cost_minor": None}
        job.state = "proposed"
        await repository.quota_settle(job, attempt, exhausted=False, reset_at=None, succeeded=True)
    async with brainstorm_session_factory() as session, session.begin():
        repository = PostgresBrainstormRepository(session)
        row = await session.get(BrainstormJobRow, receipt.job_id)
        pool = await repository.quota_pool(repository.decode_snapshot(row))
        recovered = not pool.blocked and pool.recovered_at is not None
        pool.blocked = False
        pool.probe_attempt_id = None
    assert recovered


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [False, True])
async def test_exhaustion_uses_known_reset_or_controlled_cooldown(
    brainstorm_session_factory, monkeypatch, known
):
    clock = [datetime(2026, 10, 4, tzinfo=UTC)]

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(repository_module, "datetime", ClockDateTime)
    _, _, _, _, receipt = await prepared(brainstorm_session_factory)
    policy = QuotaPolicy(unknown_reset_cooldown_seconds=67)
    reset = clock[0] + timedelta(hours=2) if known else None
    async with brainstorm_session_factory() as session, session.begin():
        repository = PostgresBrainstormRepository(session, quota_policy=policy)
        claimed = await repository.claim("worker-a")
        assert claimed is not None
        job, attempt = claimed
        await repository.quota_settle(job, attempt, exhausted=True, reset_at=reset)
        pool = await repository.quota_pool(repository.decode_snapshot(job))
        assert pool.blocked and pool.next_eligible_at == (reset or clock[0] + timedelta(seconds=67))
        assert pool.retry_basis == ("known_reset" if known else "probe_cooldown")
        assert pool.probe_attempt_id is None
    async with brainstorm_session_factory() as session, session.begin():
        row = await session.get(BrainstormJobRow, receipt.job_id)
        repository = PostgresBrainstormRepository(session, quota_policy=policy)
        pool = await repository.quota_pool(repository.decode_snapshot(row))
        await session.execute(
            delete(SubscriptionQuotaObservation).where(
                SubscriptionQuotaObservation.source_attempt_id == attempt.id
            )
        )
        pool.blocked = False


@pytest.mark.asyncio
@pytest.mark.parametrize("newer,successful", [(False, False), (True, True)])
async def test_failed_or_stale_probe_cannot_clear_pool(
    brainstorm_session_factory, newer, successful
):
    _, _, _, _, receipt = await prepared(brainstorm_session_factory)
    async with brainstorm_session_factory() as session, session.begin():
        job = await session.get(BrainstormJobRow, receipt.job_id)
        repository = PostgresBrainstormRepository(session)
        pool = await repository.quota_pool(repository.decode_snapshot(job))
        pool.blocked = True
        pool.revision = 1
        pool.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
    async with brainstorm_session_factory() as session, session.begin():
        repository = PostgresBrainstormRepository(session)
        claimed = await repository.claim("worker-a")
        assert claimed is not None
        job, attempt = claimed
        pool = await repository.quota_pool(repository.decode_snapshot(job))
        if newer:
            pool.revision += 1
        attempt.process_settled = True
        attempt.usage_known = True
        attempt.usage = {
            "input_tokens": 1,
            "output_tokens": 1,
            "tool_call_count": 0,
            "duration_ms": 1,
        }
        await repository.quota_settle(
            job, attempt, exhausted=False, reset_at=None, succeeded=successful
        )
        assert pool.blocked and pool.probe_attempt_id is None
        if not newer:
            assert pool.retry_basis == "probe_cooldown"
        pool.blocked = False


@pytest.mark.asyncio
async def test_stale_exhaustion_observes_without_overwriting_newer_pool(brainstorm_session_factory):
    _, _, _, _, _ = await prepared(brainstorm_session_factory)
    async with brainstorm_session_factory() as session, session.begin():
        repository = PostgresBrainstormRepository(session)
        claimed = await repository.claim("worker-a")
        assert claimed is not None
        job, attempt = claimed
        pool = await repository.quota_pool(repository.decode_snapshot(job))
        pool.revision += 1
        pool.observed_at = datetime.now(UTC) + timedelta(minutes=1)
        await repository.quota_settle(job, attempt, exhausted=True, reset_at=None)
        await repository.quota_settle(job, attempt, exhausted=True, reset_at=None)
        assert not pool.blocked and pool.revision == 1
        assert pool.probe_attempt_id is None
    async with brainstorm_session_factory() as session, session.begin():
        observations = (
            await session.scalars(
                select(SubscriptionQuotaObservation).where(
                    SubscriptionQuotaObservation.source_attempt_id == attempt.id
                )
            )
        ).all()
        assert len(observations) == 1
        await session.execute(
            delete(SubscriptionQuotaObservation).where(
                SubscriptionQuotaObservation.source_attempt_id == attempt.id
            )
        )


@pytest.mark.asyncio
async def test_fresh_active_attempt_exhaustion_blocks_after_pool_revision_advances(
    brainstorm_session_factory,
):
    _, _, _, _, _ = await prepared(brainstorm_session_factory)
    async with brainstorm_session_factory() as session, session.begin():
        repository = PostgresBrainstormRepository(session)
        claimed = await repository.claim("worker-a")
        assert claimed is not None
        job, attempt = claimed
        pool = await repository.quota_pool(repository.decode_snapshot(job))
        pool.revision += 2
        pool.blocked = False
        pool.observed_at = datetime.now(UTC) - timedelta(seconds=1)
        await repository.quota_settle(job, attempt, exhausted=True, reset_at=None)
        assert pool.blocked and pool.revision == 3
        await repository.quota_settle(job, attempt, exhausted=True, reset_at=None)
        assert pool.revision == 3
        pool.blocked = False
    async with brainstorm_session_factory() as session, session.begin():
        await session.execute(
            delete(SubscriptionQuotaObservation).where(
                SubscriptionQuotaObservation.source_attempt_id == attempt.id
            )
        )
