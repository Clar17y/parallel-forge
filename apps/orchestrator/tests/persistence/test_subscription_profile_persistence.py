"""PostgreSQL serialization coverage for immutable operator profiles."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_profiles import (
    ProfileBody,
    ProfileVersionRequest,
    ProjectProfileSelectionRequest,
    SubscriptionProfileService,
)
from forge.domain.subscription import (
    ExecutionEnvelope,
    OperatorProfile,
    RolePreference,
    RouteBinding,
    RouteSpec,
    SpecialistPurpose,
)
from forge.persistence.repositories.mutations import MutationConflict
from forge.persistence.repositories.subscription import (
    PostgresSubscriptionRepository,
    SubscriptionConflict,
)
from sqlalchemy import text


def _profile(profile_id=None, version=1) -> OperatorProfile:
    return OperatorProfile(
        profile_id=profile_id or uuid4(),
        version=version,
        preferences=(
            RolePreference(
                purpose=SpecialistPurpose.PRIMARY,
                preferred_route=RouteSpec(
                    provider="openai", client="codex", model=f"model-{version}"
                ),
            ),
        ),
    )


def _body(model: str) -> ProfileBody:
    return ProfileBody.model_validate(
        {
            "preferences": [
                {
                    "purpose": "primary",
                    "preferred_route": {"provider": "openai", "client": "codex", "model": model},
                }
            ]
        }
    )


@pytest.mark.integration
async def test_profile_append_and_project_selection_are_serialized_by_postgres(
    session_factory, persisted_run
) -> None:
    first = _profile()
    async with session_factory() as session:
        repo = PostgresSubscriptionRepository(session)
        assert await repo.store_profile(first) == first
        await session.commit()

    async def append() -> bool:
        async with session_factory() as session:
            repo = PostgresSubscriptionRepository(session)
            try:
                await repo.append_profile(replace(first, version=2), expected_current_version=1)
            except SubscriptionConflict:
                await session.rollback()
                return False
            await session.commit()
            return True

    assert (await asyncio.gather(append(), append())).count(True) == 1
    async with session_factory() as session:
        second = await PostgresSubscriptionRepository(session).profile(first.profile_id, 2)
    assert second.version == 2

    async with session_factory() as session:
        repo = PostgresSubscriptionRepository(session)
        await repo.select_project_profile_expected(
            persisted_run.project_id, first, expected_profile_id=None, expected_profile_version=None
        )
        await session.commit()
    async with session_factory() as session:
        repo = PostgresSubscriptionRepository(session)
        with pytest.raises(SubscriptionConflict, match="stale"):
            await repo.select_project_profile_expected(
                persisted_run.project_id,
                second,
                expected_profile_id=None,
                expected_profile_version=None,
            )
        await session.rollback()
    # The v0.2 migration deliberately refuses a downgrade while disposable
    # subscription rows exist; leave the fixture database removable.
    async with session_factory() as session:
        await session.execute(text("DELETE FROM project_subscription_profiles"))
        await session.execute(text("DELETE FROM subscription_envelopes"))
        await session.execute(text("DELETE FROM subscription_profile_versions"))
        await session.commit()


@pytest.mark.integration
async def test_profile_service_receipts_replays_and_competing_project_selection_are_durable(
    session_factory, persisted_run
) -> None:
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    service = SubscriptionProfileService(lambda: PostgresUnitOfWork(session_factory))
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    first = await service.create(
        actor=actor, idempotency_key="profile-create", request=_body("one")
    )
    assert (
        await service.create(actor=actor, idempotency_key="profile-create", request=_body("one"))
        == first
    )
    with pytest.raises(MutationConflict):
        await service.create(
            actor=actor, idempotency_key="profile-create", request=_body("different")
        )
    second = await service.append(
        actor=actor,
        profile_id=first.profile_id,
        idempotency_key="profile-append",
        request=ProfileVersionRequest.model_validate(
            {"expected_current_version": 1, **_body("two").model_dump(mode="json")}
        ),
    )
    assert (
        await service.append(
            actor=actor,
            profile_id=first.profile_id,
            idempotency_key="profile-append",
            request=ProfileVersionRequest.model_validate(
                {"expected_current_version": 1, **_body("two").model_dump(mode="json")}
            ),
        )
        == second
    )
    third = await service.append(
        actor=actor,
        profile_id=first.profile_id,
        idempotency_key="profile-append-three",
        request=ProfileVersionRequest.model_validate(
            {"expected_current_version": 2, **_body("three").model_dump(mode="json")}
        ),
    )
    route = first.preferences[0].preferred_route
    envelope = ExecutionEnvelope(
        run_id=persisted_run.id,
        profile_id=first.profile_id,
        profile_version=first.version,
        safety_policy_version=1,
        routes=((SpecialistPurpose.PRIMARY, RouteBinding(requested=route, effective=route)),),
    )
    async with session_factory() as session:
        repo = PostgresSubscriptionRepository(session)
        assert await repo.freeze_envelope(envelope) == envelope
        await session.commit()

    async def select(profile: OperatorProfile, key: str) -> bool:
        try:
            await service.select(
                actor=actor,
                project_id=persisted_run.project_id,
                idempotency_key=key,
                request=ProjectProfileSelectionRequest(
                    profile_id=profile.profile_id,
                    profile_version=profile.version,
                    expected_profile_id=None,
                    expected_profile_version=None,
                ),
            )
        except SubscriptionConflict:
            return False
        return True

    assert (
        await asyncio.gather(select(second, "select-two"), select(third, "select-three"))
    ).count(True) == 1
    selected = await service.selected(persisted_run.project_id)
    assert selected in {second, third}
    async with session_factory() as session:
        assert (
            await PostgresSubscriptionRepository(session).envelope_for_run(persisted_run.id)
            == envelope
        )
    async with session_factory() as session:
        await session.execute(text("DELETE FROM project_subscription_profiles"))
        await session.execute(text("DELETE FROM subscription_envelopes"))
        await session.execute(text("DELETE FROM subscription_profile_versions"))
        await session.execute(text("DELETE FROM api_mutations"))
        await session.commit()
