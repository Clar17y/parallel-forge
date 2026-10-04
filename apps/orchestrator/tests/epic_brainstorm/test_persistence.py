import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.epic_brainstorm import BriefInput
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brainstorm import EpicBrainstormService
from forge.domain.epic_brainstorm import BrainstormConflict, FrozenBriefContent, FrozenRequirement
from forge.domain.subscription import RouteBinding, RouteSpec, TaskBudget
from forge.persistence.models.epic_brainstorm import BrainstormJobRow
from forge.persistence.models.project import Project
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository


class BriefFixture:
    def __init__(self, epic_id, project_id):
        self.current = BriefInput(
            epic_id=epic_id,
            project_id=project_id,
            epic_version=1,
            draft_digest="a" * 64,
            accepted_revision_id=None,
            accepted_digest=None,
            draft_content=FrozenBriefContent(
                problem="Original problem",
                requirements=(
                    FrozenRequirement(
                        requirement_id=uuid4(),
                        text="Preserve this",
                        acceptance_criteria=("Verified",),
                    ),
                ),
                open_questions=("Still open?",),
            ),
        )
        self.saved = []

    async def input(self, epic_id, *, for_update=False):
        assert epic_id == self.current.epic_id
        return self.current

    async def save_proposal_revision(self, epic_id, *, expected_version, source_job_id, proposal):
        assert expected_version == self.current.epic_version
        result = uuid4()
        self.saved.append((source_job_id, proposal.digest, result))
        self.current = replace(self.current, epic_version=expected_version + 1)
        return result


@pytest.mark.asyncio
async def test_conversation_and_job_reopen_replay_and_cross_project(brainstorm_session_factory):
    project_id, epic_id, other_project = uuid4(), uuid4(), uuid4()
    async with brainstorm_session_factory() as session, session.begin():
        session.add(
            Project(
                id=project_id,
                canonical_path=f"/tmp/{project_id}",
                github_repository="example/repo",
                default_branch="main",
            )
        )
    brief = BriefFixture(epic_id, project_id)
    route = RouteSpec(provider="fake", client="fake", model="fixture")
    binding = RouteBinding(requested=route, effective=route)
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())

    def service():
        return EpicBrainstormService(
            brainstorm_session_factory,
            lambda _session: brief,
            route=binding,
            budget=TaskBudget(max_provider_attempts=2),
        )

    conversation_id, version = await service().create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="create", text="Explore this idea"
    )
    assert version == 2
    assert await service().create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="create", text="Explore this idea"
    ) == (conversation_id, version)
    with pytest.raises(BrainstormConflict, match="payload"):
        await service().create(
            epic_id=epic_id, project_id=project_id, actor=actor, key="create", text="Different idea"
        )
    turns = await service().turns(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id
    )
    assert turns[0].text == "Explore this idea"
    receipt = await service().submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=turns[0].turn_id,
        expected_epic_version=1,
        expected_conversation_version=2,
        actor=actor,
        key="submit",
    )
    assert (
        await service().submit(
            epic_id=epic_id,
            project_id=project_id,
            conversation_id=conversation_id,
            prompt_turn_id=turns[0].turn_id,
            expected_epic_version=1,
            expected_conversation_version=2,
            actor=actor,
            key="submit",
        )
        == receipt
    )
    assert (
        await service().observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    ).state == "queued"
    original = brief.current.draft_content
    brief.current = replace(brief.current, draft_content=FrozenBriefContent(problem="Later edit"))
    async with brainstorm_session_factory() as session:
        row = await session.get(BrainstormJobRow, receipt.job_id)
        assert row is not None
        frozen = PostgresBrainstormRepository.decode_snapshot(row)
        assert frozen.draft_content == original
        assert frozen.draft_content.requirements[0].acceptance_criteria == ("Verified",)
    with pytest.raises(ValueError):
        await service().observe(epic_id=epic_id, project_id=other_project, job_id=receipt.job_id)


@pytest.mark.asyncio
async def test_same_key_concurrent_create_replays_after_postgres_transaction(
    brainstorm_session_factory,
):
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
    brief = BriefFixture(epic_id, project_id)
    entered, release = asyncio.Event(), asyncio.Event()
    original_input = brief.input
    calls = 0

    async def delayed_input(epic_id, *, for_update=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=2)
        return await original_input(epic_id, for_update=for_update)

    brief.input = delayed_input
    route = RouteSpec(provider="fake", client="fake", model="fixture")
    service = EpicBrainstormService(
        brainstorm_session_factory,
        lambda _session: brief,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=2),
    )
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    request = {
        "epic_id": epic_id,
        "project_id": project_id,
        "actor": actor,
        "key": "same",
        "text": "Idea",
    }
    first = asyncio.create_task(service.create(**request))
    await asyncio.wait_for(entered.wait(), timeout=2)
    second = asyncio.create_task(service.create(**request))
    await asyncio.sleep(0.1)
    release.set()
    first_result, second_result = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert first_result == second_result
    assert calls == 1
    with pytest.raises(BrainstormConflict, match="payload"):
        await service.create(**{**request, "text": "Different"})


@pytest.mark.asyncio
async def test_command_lock_is_key_scoped_and_rollback_releases_it(brainstorm_session_factory):
    epic_id = uuid4()
    acquired = asyncio.Event()

    async def acquire(key):
        async with brainstorm_session_factory() as session, session.begin():
            await PostgresBrainstormRepository(session).lock_command(epic_id, key)
            acquired.set()

    with pytest.raises(RuntimeError, match="rollback"):
        async with brainstorm_session_factory() as session, session.begin():
            await PostgresBrainstormRepository(session).lock_command(epic_id, "shared")
            await asyncio.wait_for(acquire("different"), timeout=2)
            acquired.clear()
            waiting = asyncio.create_task(acquire("shared"))
            await asyncio.sleep(0.1)
            assert not acquired.is_set() and not waiting.done()
            raise RuntimeError("rollback")
    await asyncio.wait_for(waiting, timeout=2)
    assert acquired.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dimension", "value"),
    (
        ("max_duration_seconds", (2**63 - 1) // 1000 + 1),
        ("max_tool_calls", 2**63),
        ("max_input_tokens", 2**63),
        ("max_output_tokens", 2**63),
        ("max_cost_minor", 2**63),
    ),
)
async def test_unrepresentable_budget_is_rejected_before_job_submission(
    brainstorm_session_factory,
    dimension,
    value,
):
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
        budget=TaskBudget(max_provider_attempts=1, **{dimension: value}),
    )
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="create", text="Idea"
    )
    turn = (
        await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    )[0]
    with pytest.raises(BrainstormConflict, match="usage range"):
        await service.submit(
            epic_id=epic_id,
            project_id=project_id,
            conversation_id=conversation_id,
            prompt_turn_id=turn.turn_id,
            expected_epic_version=1,
            expected_conversation_version=version,
            actor=actor,
            key="submit",
        )


def test_two_safe_reservations_keep_exact_raw_held_arithmetic() -> None:
    limit = 2**63 - 1
    attempts = [
        SimpleNamespace(
            usage_known=False,
            usage={"input_tokens": None},
            reservation={"input_tokens": limit},
            process_settled=False,
            tool_calls_used=0,
        )
        for _ in range(2)
    ]
    charged, held, unknown = PostgresBrainstormRepository._charges(attempts)
    assert charged["input_tokens"] == 0
    assert held["input_tokens"] == 2 * limit
    assert unknown == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", (None, 2**63, -1))
async def test_sibling_outcome_marks_unreserved_unknown_cumulative_dimensions(
    bad_value: int | None,
) -> None:
    unknown = SimpleNamespace(
        id=uuid4(),
        usage_known=False,
        usage={
            "duration_ms": 100,
            "duration_lower_bound_ms": 100,
            "tool_call_count": 0,
            "input_tokens": bad_value,
            "output_tokens": bad_value,
            "estimated_api_cost_minor": bad_value,
            "currency": "USD",
        },
        reservation={"duration_ms": 1000, "tool_call_count": 1},
        process_settled=True,
        tool_calls_used=0,
    )
    known = SimpleNamespace(
        id=uuid4(),
        usage_known=True,
        usage={
            "duration_ms": 50,
            "duration_lower_bound_ms": 50,
            "tool_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_api_cost_minor": 0,
            "currency": "USD",
        },
        reservation={},
        process_settled=True,
        tool_calls_used=0,
    )
    session = SimpleNamespace(get=AsyncMock(return_value=known))
    repository = PostgresBrainstormRepository(session)
    repository._epic_attempts = AsyncMock(return_value=[unknown, known])
    row = SimpleNamespace(
        id=uuid4(),
        epic_id=uuid4(),
        current_attempt_id=known.id,
        version=1,
        state="failed",
        proposal_digest=None,
        proposal=None,
        adopted_revision_id=None,
        failure="invalid_output",
    )
    outcome = await repository.outcome(row)
    assert outcome.usage_known is True
    assert outcome.cumulative_usage.input_tokens == 0
    assert outcome.held_reservations.input_tokens == 0
    for field in ("input_tokens", "output_tokens", "estimated_api_cost_minor"):
        assert getattr(outcome.held_reasons, field) == "unsettled_or_unknown"
