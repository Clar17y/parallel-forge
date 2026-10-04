from datetime import UTC, datetime, timedelta

import forge.persistence.repositories.epic_brainstorm as repository_module
import pytest
from forge.domain.subscription_quota import QuotaPolicy
from forge.persistence.models.epic_brainstorm import BrainstormJobRow
from forge.persistence.models.subscription_quota import (
    SubscriptionQuotaObservation,
)
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository
from sqlalchemy import delete, select

from apps.orchestrator.tests.epic_brainstorm.test_worker import prepared


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
