from dataclasses import replace
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
