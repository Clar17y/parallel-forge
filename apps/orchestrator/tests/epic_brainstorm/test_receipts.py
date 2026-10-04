import json
from hashlib import sha256
from uuid import uuid4

import pytest
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brainstorm import EpicBrainstormService
from forge.domain.epic_brainstorm import BrainstormConflict
from forge.domain.subscription import RouteBinding, RouteSpec, TaskBudget
from forge.persistence.models.epic_brainstorm import BrainstormAuditRow, BrainstormReceiptRow
from forge.persistence.models.project import Project
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository
from sqlalchemy import select

from apps.orchestrator.tests.epic_brainstorm.test_persistence import BriefFixture


@pytest.mark.asyncio
async def test_secret_shaped_key_is_rejected_before_all_mutation_receipts(
    brainstorm_session_factory,
) -> None:
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
    route = RouteSpec(provider="fake", client="fake", model="fixture")
    service = EpicBrainstormService(
        brainstorm_session_factory,
        lambda _session: BriefFixture(epic_id, project_id),
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="safe-create", text="Idea"
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
        key="safe-submit",
    )
    unsafe = "api_key=" + "syntheticvalue123456"
    calls = (
        service.create(
            epic_id=epic_id, project_id=project_id, actor=actor, key=unsafe, text="Idea"
        ),
        service.append(
            epic_id=epic_id,
            project_id=project_id,
            conversation_id=conversation_id,
            expected_version=version,
            actor=actor,
            key=unsafe,
            text="Next",
        ),
        service.submit(
            epic_id=epic_id,
            project_id=project_id,
            conversation_id=conversation_id,
            prompt_turn_id=turn.turn_id,
            expected_epic_version=1,
            expected_conversation_version=version,
            actor=actor,
            key=unsafe,
        ),
        service.cancel(
            epic_id=epic_id,
            project_id=project_id,
            job_id=receipt.job_id,
            expected_job_version=receipt.job_version,
            actor=actor,
            key=unsafe,
        ),
        service.retry(
            epic_id=epic_id,
            project_id=project_id,
            job_id=receipt.job_id,
            expected_job_version=receipt.job_version,
            actor=actor,
            key=unsafe,
        ),
        service.adopt(
            epic_id=epic_id,
            project_id=project_id,
            job_id=receipt.job_id,
            proposal_digest="0" * 64,
            expected_job_version=receipt.job_version,
            expected_epic_version=1,
            actor=actor,
            key=unsafe,
        ),
    )
    for call in calls:
        with pytest.raises(ValueError, match="idempotency key") as error:
            await call
        assert unsafe not in str(error.value)
    async with brainstorm_session_factory() as session:
        receipts = (await session.scalars(select(BrainstormReceiptRow))).all()
        audits = (await session.scalars(select(BrainstormAuditRow))).all()
        assert all(unsafe not in row.key for row in receipts)
        assert all(unsafe not in json.dumps(row.response) for row in receipts)
        assert all(unsafe not in json.dumps(row.detail) for row in audits)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    ("sk-ant-api03-synthetic", "xoxb-synthetic", "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.sig"),
)
async def test_opaque_keys_are_hashed_and_replay_remains_detached(
    brainstorm_session_factory, key
) -> None:
    epic_id, project_id = uuid4(), uuid4()
    async with brainstorm_session_factory() as session, session.begin():
        session.add(
            Project(
                id=project_id,
                canonical_path=f"/tmp/{project_id}",
                github_repository="example/repo",
                default_branch="main",
            )
        )
    route = RouteSpec(provider="fake", client="fake", model="fixture")
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())

    def service():
        return EpicBrainstormService(
            brainstorm_session_factory,
            lambda _: BriefFixture(epic_id, project_id),
            route=RouteBinding(requested=route, effective=route),
            budget=TaskBudget(max_provider_attempts=2),
        )

    conversation_id, version = await service().create(
        epic_id=epic_id, project_id=project_id, actor=actor, key=key, text="Idea"
    )
    assert await service().create(
        epic_id=epic_id, project_id=project_id, actor=actor, key=key, text="Idea"
    ) == (conversation_id, version)
    turn = (
        await service().turns(
            epic_id=epic_id, project_id=project_id, conversation_id=conversation_id
        )
    )[0]
    receipt = await service().submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key=f"{key}-submit",
    )
    replay = await service().submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key=f"{key}-submit",
    )
    assert replay == receipt and replay.replay_key == f"{key}-submit"
    with pytest.raises(BrainstormConflict, match="payload conflicts"):
        await service().submit(
            epic_id=epic_id,
            project_id=project_id,
            conversation_id=conversation_id,
            prompt_turn_id=turn.turn_id,
            expected_epic_version=1,
            expected_conversation_version=version,
            actor=actor,
            key=key,
        )
    cancelled = await service().cancel(
        epic_id=epic_id,
        project_id=project_id,
        job_id=receipt.job_id,
        expected_job_version=receipt.job_version,
        actor=actor,
        key=f"{key}-cancel",
    )
    assert (
        await service().cancel(
            epic_id=epic_id,
            project_id=project_id,
            job_id=receipt.job_id,
            expected_job_version=receipt.job_version,
            actor=actor,
            key=f"{key}-cancel",
        )
        == cancelled
    )
    async with brainstorm_session_factory() as session:
        rows = (await session.scalars(select(BrainstormReceiptRow))).all()
        assert {row.key for row in rows} == {
            sha256(value.encode()).hexdigest() for value in (key, f"{key}-submit", f"{key}-cancel")
        }
        assert all(key not in json.dumps(row.response) for row in rows)
        assert all("replay_key" not in row.response for row in rows)
        stored = next(row for row in rows if "job_id" in row.response)
        repository = PostgresBrainstormRepository(session)
        detached = await repository.replay(epic_id, f"{key}-submit", stored.request_digest)
        assert detached is not None and detached["replay_key"] == f"{key}-submit"
        assert "replay_key" not in stored.response
