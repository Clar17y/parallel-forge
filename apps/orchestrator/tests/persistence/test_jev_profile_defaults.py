"""Jev defaults follow the run's immutable profile, with project overrides."""

from dataclasses import replace
from uuid import uuid4

import pytest
import pytest_asyncio
from forge.api.schemas.subscription_profiles import ProfileResponse
from forge.application.ports.jev import JevRequest
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.jev import JevService
from forge.application.services.jev_reporting import JevReportingService
from forge.application.services.subscription_profiles import (
    ProfileBody,
    ProfileVersionRequest,
    SubscriptionProfileService,
)
from forge.domain.policy import JevPolicy
from forge.domain.subscription import ExecutionEnvelope, OperatorProfile
from forge.persistence.models import ProjectPolicyVersion, Run
from forge.persistence.repositories.mutations import MutationConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import text, update
from test_jev_budget import FakeProvider, create_jev_run

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(autouse=True)
async def clear_subscription_data(session_factory):
    yield
    async with session_factory() as session, session.begin():
        await session.execute(text("TRUNCATE subscription_profile_versions, jev_evaluations CASCADE"))


async def bind_profile(session_factory, run, policy):
    profile = OperatorProfile(profile_id=uuid4(), version=1, preferences=(), jev=policy)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription.select_project_profile(run.project_id, profile)
        await work.subscription.freeze_envelope(ExecutionEnvelope(
            run_id=run.id, profile_id=profile.profile_id, profile_version=1,
            safety_policy_version=1, routes=(),
        ))
        await work.commit()
    return profile


def request_for(run, *, kind="semantic_search"):
    return JevRequest(
        run_id=run.id, policy_version=1, operation_key=kind, kind=kind,
        worktree_digest="a" * 64, state={"objective": "needle"},
        questions={"m0": {"type": "score", "instructions": "x", "criteria": ["no", "some", "yes"]}},
    )


@pytest.mark.asyncio
async def test_profile_default_is_frozen_and_reported_before_any_call(session_factory, persisted_run):
    policy = JevPolicy(mode="on", allow_remote=True, max_requests_per_run=7)
    profile = await bind_profile(session_factory, persisted_run, policy)
    async with PostgresUnitOfWork(session_factory) as work:
        newer = replace(profile, version=2, jev=JevPolicy(mode="off"))
        await work.subscription.append_profile(newer, expected_current_version=1)
        await work.subscription.select_project_profile(persisted_run.project_id, newer)
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        assert await work.jev.policy_for_run(persisted_run.id, policy_version=1) == policy
    report = await JevReportingService(lambda: PostgresUnitOfWork(session_factory)).report(
        persisted_run.id
    )
    assert report["requested_mode"] == "on"
    assert report["availability"] == "no_samples"
    assert report["remaining_requests"] == 7
    assert report["calls"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["search_ranking", "semantic_search", "review_focus"])
async def test_profile_default_authorizes_each_jev_consumer(session_factory, persisted_run, kind):
    policy = JevPolicy(mode="on", allow_remote=True)
    await bind_profile(session_factory, persisted_run, policy)
    provider = FakeProvider()
    provider.release.set()
    service = JevService(lambda: PostgresUnitOfWork(session_factory), provider)
    request = request_for(persisted_run, kind=kind)
    assert (await service.evaluate(request, policy=policy)).status == (
        "ranked" if kind == "search_ranking" else "succeeded"
    )
    assert (await service.evaluate(request, policy=policy)).status == "cached"
    assert provider.calls == 1
    with pytest.raises(ValueError, match="does not authorize"):
        await service.evaluate(request, policy=policy.model_copy(update={"max_requests_per_run": 99}))
    with pytest.raises(ValueError, match="version does not match"):
        await service.evaluate(replace(request, policy_version=2), policy=policy)
    assert provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_explicit_project_policy_overrides_profile(session_factory, mode):
    override = JevPolicy(mode=mode, allow_remote=False)
    run = await create_jev_run(session_factory, override)
    default = JevPolicy(mode="on", allow_remote=True)
    await bind_profile(session_factory, run, default)
    async with PostgresUnitOfWork(session_factory) as work:
        assert await work.jev.policy_for_run(run.id, policy_version=1) == override
    provider = FakeProvider()
    service = JevService(lambda: PostgresUnitOfWork(session_factory), provider)
    with pytest.raises(ValueError, match="does not authorize"):
        await service.evaluate(request_for(run), policy=default)
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_profile_remote_consent_is_required(session_factory, persisted_run):
    policy = JevPolicy(mode="on", allow_remote=False)
    await bind_profile(session_factory, persisted_run, policy)
    provider = FakeProvider()
    service = JevService(lambda: PostgresUnitOfWork(session_factory), provider)
    assert (await service.evaluate(request_for(persisted_run), policy=policy)).status == "unavailable"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_selected_profile_cannot_enable_a_run_without_a_bound_envelope(
    session_factory, persisted_run,
):
    policy = JevPolicy(mode="on", allow_remote=True)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription.select_project_profile(persisted_run.project_id, OperatorProfile(
            profile_id=uuid4(), version=1, preferences=(), jev=policy,
        ))
        assert await work.jev.policy_for_run(persisted_run.id, policy_version=1) is None
        await work.commit()
    provider = FakeProvider()
    service = JevService(lambda: PostgresUnitOfWork(session_factory), provider)
    with pytest.raises(ValueError, match="does not authorize"):
        await service.evaluate(request_for(persisted_run), policy=policy)
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_profile_service_replays_and_removes_defaults_in_new_versions(session_factory):
    service = SubscriptionProfileService(lambda: PostgresUnitOfWork(session_factory))
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    body = ProfileBody.model_validate({
        "preferences": [{"purpose": "primary", "preferred_route": {
            "provider": "openai", "client": "codex", "model": "gpt-test",
        }}],
        "jev": {"mode": "on", "allow_remote": True, "timeout_seconds": 2.5},
    })
    first = await service.create(actor=actor, idempotency_key="with-jev", request=body)
    assert await service.create(actor=actor, idempotency_key="with-jev", request=body) == first
    assert ProfileResponse.from_profile(first).jev == body.jev
    with pytest.raises(MutationConflict):
        await service.create(
            actor=actor, idempotency_key="with-jev", request=body.model_copy(update={"jev": None}),
        )
    request = ProfileVersionRequest.model_validate({
        **body.model_dump(mode="json"), "expected_current_version": 1, "jev": None,
    })
    second = await service.append(
        actor=actor, profile_id=first.profile_id, idempotency_key="remove-jev", request=request,
    )
    assert second.version == 2 and second.jev is None
    assert (await service.get(first.profile_id, 1)).jev == body.jev
    assert await service.append(
        actor=actor, profile_id=first.profile_id, idempotency_key="remove-jev", request=request,
    ) == second


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_off", [False, True])
async def test_changed_run_policy_binding_never_authorizes_profile_remote_calls(
    session_factory, persisted_run, explicit_off,
):
    policy = JevPolicy(mode="on", allow_remote=True)
    await bind_profile(session_factory, persisted_run, policy)
    async with session_factory() as session, session.begin():
        session.add(ProjectPolicyVersion(
            project_id=persisted_run.project_id, version=2, policy_digest="c" * 64,
            document_schema_version=1,
            document={"jev": {"mode": "off"}} if explicit_off else {},
        ))
        await session.flush()
        await session.execute(update(Run).where(Run.id == persisted_run.id).values(policy_version=2))
    reporter = JevReportingService(lambda: PostgresUnitOfWork(session_factory))
    if explicit_off:
        assert (await reporter.report(persisted_run.id))["requested_mode"] == "off"
    else:
        with pytest.raises(ValueError, match="profile policy version does not match"):
            await reporter.report(persisted_run.id)
    provider = FakeProvider()
    service = JevService(lambda: PostgresUnitOfWork(session_factory), provider)
    with pytest.raises(ValueError):
        await service.evaluate(replace(request_for(persisted_run), policy_version=2), policy=policy)
    assert provider.calls == 0
