"""PostgreSQL operator quota report and task projection coverage."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.api.schemas.subscription_quota import QuotaExhaustionReportRequest
from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.application.services.subscription_quota import SubscriptionQuotaService
from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.subscription import (
    ExecutionEnvelope,
    LogicalTaskContract,
    OperatorProfile,
    RolePreference,
    RouteBinding,
    RouteMapping,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
)
from forge.domain.subscription_quota import QuotaPolicy, QuotaRoutePool
from forge.persistence.models.api import ApiMutation, OperatorAuditEvent
from forge.persistence.models.subscription import (
    SubscriptionEnvelope,
    SubscriptionProfileVersion,
    SubscriptionTask,
)
from forge.persistence.models.subscription_quota import (
    SubscriptionQuotaObservation,
    SubscriptionQuotaPool,
)
from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery
from forge.persistence.repositories.mutations import MutationConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import delete, func, select


@pytest.mark.integration
async def test_operator_report_roundtrip_replay_and_known_reset(session_factory) -> None:
    actor = LocalOperatorProfileActor()
    actor_id = actor.actor_id
    now = [datetime(2026, 9, 12, 12, tzinfo=UTC)]
    policy = QuotaPolicy(unknown_reset_cooldown_seconds=600)
    service = SubscriptionQuotaService(
        lambda: PostgresUnitOfWork(
            session_factory, quota_policy=policy, quota_clock=lambda: now[0]
        ),
        now=lambda: now[0],
    )
    request = QuotaExhaustionReportRequest(
        provider="openai", account="team-a", pool="allowance", reason="token=secret"
    )
    try:
        first = await service.report_exhaustion(
            actor=actor, idempotency_key="quota-op-1", request=request
        )
        assert first.status == "blocked"
        assert first.retry_basis == "probe_cooldown"
        assert first.next_eligible_at == now[0] + timedelta(seconds=600)

        now[0] += timedelta(days=2)
        replay = await service.report_exhaustion(
            actor=actor, idempotency_key="quota-op-1", request=request
        )
        assert replay.next_eligible_at == first.next_eligible_at

        async with session_factory() as session:
            observations = await session.scalar(
                select(func.count()).select_from(SubscriptionQuotaObservation)
            )
            audits = await session.scalar(
                select(func.count())
                .select_from(OperatorAuditEvent)
                .where(
                    OperatorAuditEvent.actor_id == actor_id,
                    OperatorAuditEvent.event_type == "subscription.quota_exhaustion_reported",
                )
            )
            mutation = await session.scalar(
                select(ApiMutation).where(
                    ApiMutation.actor_id == actor_id,
                    ApiMutation.action == "subscription.quota.report_exhaustion",
                )
            )
            assert observations == 1 and audits == 1
            assert mutation is not None and mutation.resource_kind == "subscription_quota_pool"

        with pytest.raises(MutationConflict):
            await service.report_exhaustion(
                actor=actor,
                idempotency_key="quota-op-1",
                request=request.model_copy(update={"reason": "different"}),
            )

        known = await service.report_exhaustion(
            actor=actor,
            idempotency_key="quota-op-2",
            request=QuotaExhaustionReportRequest(
                provider="openai",
                account="team-a",
                pool="known",
                reason="monthly_limit",
                reset_at=now[0] + timedelta(hours=2),
            ),
        )
        assert known.retry_basis == "known_reset"
        assert known.reset_at == now[0] + timedelta(hours=2)
    finally:
        async with session_factory() as session:
            await session.execute(delete(ApiMutation).where(ApiMutation.actor_id == actor_id))
            await session.execute(
                delete(SubscriptionQuotaObservation).where(
                    SubscriptionQuotaObservation.actor_id == actor_id
                )
            )
            await session.execute(delete(SubscriptionQuotaPool))
            await session.commit()


@pytest.mark.integration
async def test_task_projection_maps_pool_and_selected_fallback(
    session_factory, persisted_run
) -> None:
    preferred = RouteSpec(provider="openai", client="codex_app_server", model="preferred")
    fallback = RouteSpec(provider="anthropic", client="claude_code", model="fallback")
    binding = RouteBinding(
        requested=preferred,
        effective=fallback,
        mapping_applied=RouteMapping(
            requested=preferred,
            effective=fallback,
            approved_by="profile:test",
            approval_id="profile:test:1",
            reason="confirmed quota exhaustion",
        ),
    )
    policy = QuotaPolicy(
        route_pools=(
            QuotaRoutePool(
                provider="anthropic",
                client="claude_code",
                account="team-a",
                pool="allowance",
            ),
        )
    )
    profile = OperatorProfile(
        profile_id=uuid4(),
        version=1,
        preferences=(
            RolePreference(
                purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION, preferred_route=preferred,
                fallback_routes=(fallback,),
            ),
        ),
    )
    primary = LogicalTaskContract(
        run_id=persisted_run.id,
        task_id=uuid4(),
        purpose=SpecialistPurpose.PRIMARY,
        route=RouteBinding(requested=preferred, effective=preferred, is_primary=True),
        budget=TaskBudget(),
        owned_paths=("src",),
    )
    task = LogicalTaskContract(
        run_id=persisted_run.id,
        task_id=uuid4(),
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=binding,
        budget=TaskBudget(),
        parent_task_id=primary.task_id,
        owned_paths=("src",),
    )
    try:
        async with PostgresUnitOfWork(session_factory, quota_policy=policy) as work:
            await work.subscription.store_profile(profile)
            await work.subscription.freeze_envelope(
                ExecutionEnvelope(
                    run_id=task.run_id,
                    profile_id=profile.profile_id,
                    profile_version=1,
                    safety_policy_version=1,
                    routes=(
                        (SpecialistPurpose.PRIMARY, primary.route),
                        (SpecialistPurpose.ROUTINE_IMPLEMENTATION, RouteBinding(requested=preferred, effective=preferred)),
                    ),
                    allowed_fallbacks=((SpecialistPurpose.ROUTINE_IMPLEMENTATION, (fallback,)),),
                )
            )
            await work.subscription.create_task(primary, idempotency_key="quota-projection-primary")
            await work.subscription.create_task(task, idempotency_key="quota-projection-task")
            await work.quota.report_exhaustion(
                policy.key_for(fallback),
                QuotaExhaustion(
                    observed_at=datetime(2026, 9, 12, tzinfo=UTC), reason="provider_result"
                ),
                actor_id=uuid4(),
                idempotency_key="quota-projection-report",
            )
            # A task's pool remains visible beyond the first global status page.
            work.session.add_all(
                [
                    SubscriptionQuotaPool(provider="aaa", account=f"pool-{i}", pool="weekly")
                    for i in range(101)
                ]
            )
            await work.commit()
        page = await SubscriptionTaskQuery(session_factory, quota_policy=policy).tasks(task.run_id)
        assert page is not None
        projected = next(item for item in page["tasks"] if item["task_id"] == task.task_id)
        assert projected["fallback_selected"] is True
        assert projected["effective_route"]["model"] == "fallback"
        assert projected["quota_status"]["account"] == "team-a"
        assert projected["quota_status"]["pool"] == "allowance"
        primary_status = next(item for item in page["tasks"] if item["task_id"] == primary.task_id)[
            "quota_status"
        ]
        assert primary_status["status"] == "unknown" and primary_status["observed_at"] is None
        async with session_factory() as session:
            row = await session.get(SubscriptionQuotaPool, ("anthropic", "team-a", "allowance"))
            row.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        eligible = await SubscriptionTaskQuery(session_factory, quota_policy=policy).tasks(
            task.run_id
        )
        status = next(item for item in eligible["tasks"] if item["task_id"] == task.task_id)[
            "quota_status"
        ]
        assert status["status"] == "eligible"
        # A success proves only that this request ran; remaining allowance stays unknown.
        async with session_factory() as session:
            row = await session.get(SubscriptionQuotaPool, ("anthropic", "team-a", "allowance"))
            row.blocked = False
            row.recovered_at = datetime.now(UTC)
            await session.commit()
        recovered = await SubscriptionTaskQuery(session_factory, quota_policy=policy).tasks(
            task.run_id
        )
        status = next(item for item in recovered["tasks"] if item["task_id"] == task.task_id)[
            "quota_status"
        ]
        assert status["status"] == "unknown"
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(SubscriptionTask).where(SubscriptionTask.run_id == task.run_id)
            )
            await session.execute(
                delete(SubscriptionEnvelope).where(SubscriptionEnvelope.run_id == task.run_id)
            )
            await session.execute(
                delete(SubscriptionProfileVersion).where(
                    SubscriptionProfileVersion.profile_id == profile.profile_id
                )
            )
            await session.execute(delete(SubscriptionQuotaObservation))
            await session.execute(delete(SubscriptionQuotaPool))
            await session.commit()
