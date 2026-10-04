"""Exercise the proposed main worker composition against a durable claim."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.epic_brainstorm import BrainstormGatewayResult, BriefInput
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brainstorm import EpicBrainstormService
from forge.domain.subscription import RouteBinding, RouteSpec, TaskBudget
from forge.domain.subscription_quota import QuotaPolicy
from forge.persistence.models.epic_brainstorm import BrainstormAttemptRow
from forge.persistence.models.project import Project
from sqlalchemy import select


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", (False, True))
async def test_proposed_worker_composes_bounded_owner_and_claims(
    brainstorm_session_factory, monkeypatch, registered: bool
) -> None:
    from forge.worker import main

    if not hasattr(main, "_poll_brainstorms"):
        pytest.skip("proposed brainstorm registration is absent in the fixture/raw tree")

    project_id, epic_id = uuid4(), uuid4()
    async with brainstorm_session_factory() as session, session.begin():
        session.add(
            Project(
                id=project_id,
                canonical_path=f"/tmp/{project_id}",
                github_repository="example/repo",
                default_branch="main",
            )
        )

    class Brief:
        async def input(self, _epic_id, *, for_update=False):
            return BriefInput(
                epic_id=epic_id,
                project_id=project_id,
                epic_version=1,
                draft_digest="a" * 64,
                accepted_revision_id=None,
                accepted_digest=None,
            )

    route = RouteSpec(provider="fake", client="fake", model="fixture")
    service = EpicBrainstormService(
        brainstorm_session_factory,
        lambda _session: Brief(),
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=1),
    )
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="create", text="Idea"
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
        key="submit",
    )
    seen: list[str] = []
    stop = asyncio.Event()

    class FakeEngine:
        dispose = AsyncMock()

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            pass

        async def drain(self):
            pass

    class Gateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="invalid_output")

    async def poll_regular(_worker, stop_event, _interval):
        if not registered:
            stop_event.set()
        await stop_event.wait()

    async def poll_brainstorms(worker, stop_event, _interval):
        seen.append(worker.owner)
        assert await worker.run_once(stop_event=stop_event) == receipt.job_id
        stop_event.set()

    monkeypatch.setattr(main, "create_engine", lambda _url: FakeEngine())
    monkeypatch.setattr(main, "create_session_factory", lambda _engine: brainstorm_session_factory)
    monkeypatch.setattr(main, "run_startup_recovery", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "Worker", FakeWorker)
    monkeypatch.setattr(main, "_poll", poll_regular)
    monkeypatch.setattr(main, "_poll_brainstorms", poll_brainstorms)
    base = "same-prefix-" + "x" * 242
    settings = SimpleNamespace(
        database_url="unused",
        subscription_installations_path=None,
        subscription_quota_policy=QuotaPolicy(),
    )
    await main.run_worker(
        settings=settings,
        handlers={},
        stop_event=stop,
        worker_id=base,
        brainstorm_gateway_factory=(lambda _: Gateway()) if registered else None,
        brainstorm_reader_factory=(lambda _: object()) if registered else None,
    )
    async with brainstorm_session_factory() as session:
        attempts = (await session.scalars(select(BrainstormAttemptRow))).all()
    if registered:
        assert len(seen) == len(attempts) == 1
        assert seen[0] == attempts[0].owner and len(seen[0]) <= 128
    else:
        assert not seen and not attempts
