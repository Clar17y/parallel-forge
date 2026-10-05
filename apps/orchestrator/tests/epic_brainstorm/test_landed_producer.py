"""Contract test against landed #69, executed from the disposable source overlay."""

import asyncio
import inspect
from uuid import uuid4

import pytest

pytest.importorskip("forge.domain.epic_brief")

from forge.application.services.epic_brainstorm import EpicBriefBrainstormAdapter
from forge.domain.epic_brainstorm import BrainstormConflict, BrainstormProposal
from forge.domain.epic_brief import BriefContent, BriefRequirement
from forge.persistence.models.project import Project
from forge.persistence.repositories.epic_brief import PostgresEpicBriefRepository


@pytest.mark.asyncio
async def test_landed_brief_revision_preserves_ids_criteria_and_pending_choices(session_factory):
    if (
        "source_job_id"
        not in inspect.signature(PostgresEpicBriefRepository.save_revision).parameters
    ):
        pytest.skip("PostgresEpicBriefRepository.save_revision lacks proposed source_job_id hook")
    project_id, epic_id, requirement_id, source_job_id = (uuid4() for _ in range(4))
    draft = BriefContent(
        problem="Original",
        requirements=[
            BriefRequirement(
                requirement_id=requirement_id,
                text="Existing",
                acceptance_criteria=["Preserved check"],
            )
        ],
        open_questions=["Human choice pending"],
    )
    async with session_factory() as session, session.begin():
        session.add(
            Project(
                id=project_id,
                canonical_path=f"/tmp/{project_id}",
                github_repository="example/repo",
                default_branch="main",
            )
        )
        await session.flush()
        await PostgresEpicBriefRepository(session).create(
            epic_id=epic_id, project_id=project_id, title="Epic", draft=draft
        )
    proposal = BrainstormProposal(
        turn_id=uuid4(),
        problem="Refined",
        requirements=("New",),
        requirement_criteria={"New": ("New check",)},
    )
    async with session_factory() as session, session.begin():
        adapter = EpicBriefBrainstormAdapter(session)
        snapshot = await adapter.input(epic_id)
        assert snapshot.draft_content.requirements[0].requirement_id == requirement_id
        assert snapshot.draft_content.open_questions == ("Human choice pending",)
        revision_id = await adapter.save_proposal_revision(
            epic_id,
            expected_version=snapshot.epic_version,
            source_job_id=source_job_id,
            proposal=proposal,
        )
        revision = await PostgresEpicBriefRepository(session).get_revision(epic_id, revision_id)
        assert revision.source_job_id == source_job_id
        assert revision.content.requirements[0].requirement_id == requirement_id
        assert revision.content.requirements[0].acceptance_criteria == ["Preserved check"]
        assert revision.content.requirements[1].acceptance_criteria == ["New check"]
        assert revision.content.open_questions == ["Human choice pending"]
    async with session_factory() as session:
        adapter = EpicBriefBrainstormAdapter(session)
        snapshot = await adapter.input(epic_id)
        assert snapshot.accepted_revision_id is None
        with pytest.raises(BrainstormConflict, match="changed"):
            await adapter.save_proposal_revision(
                epic_id, expected_version=1, source_job_id=uuid4(), proposal=proposal
            )
    async with session_factory() as session, session.begin():
        repository = PostgresEpicBriefRepository(session)
        revision = await repository.get_revision(epic_id, revision_id)
        await repository.adopt_revision(epic_id, version=2, revision=revision)
        accepted = await EpicBriefBrainstormAdapter(session).input(epic_id)
        assert accepted.accepted_revision_id == revision_id
        assert accepted.accepted_digest == revision.content_digest
        assert accepted.accepted_content.requirements[0].requirement_id == requirement_id
        assert accepted.accepted_content.requirements[1].acceptance_criteria == ("New check",)
    unicode_draft = BriefContent(
        problem="界" * 5000,
        outcomes=[f"Outcome {number}" for number in range(64)],
        requirements=draft.requirements,
        open_questions=["Human choice pending"],
    )
    async with session_factory() as session, session.begin():
        current = await PostgresEpicBriefRepository(session).get(epic_id)
        await PostgresEpicBriefRepository(session).update_draft(
            epic_id, version=current.version, title="Epic", draft=unicode_draft
        )
        unicode_input = await EpicBriefBrainstormAdapter(session).input(epic_id)
        assert unicode_input.draft_content.problem == unicode_draft.problem
        with pytest.raises(BrainstormConflict, match="brief limits"):
            await EpicBriefBrainstormAdapter(session).save_proposal_revision(
                epic_id,
                expected_version=current.version + 1,
                source_job_id=uuid4(),
                proposal=BrainstormProposal(
                    turn_id=uuid4(), problem="Refined", outcomes=("One extra",)
                ),
            )
        fresh = await PostgresEpicBriefRepository(session).get(epic_id)
        assert fresh.draft.open_questions == ["Human choice pending"]


@pytest.mark.asyncio
async def test_proposed_brainstorm_poll_isolates_one_job_error():
    import forge.worker.main

    if not hasattr(forge.worker.main, "_poll_brainstorms"):
        pytest.skip("forge.worker.main lacks proposed _poll_brainstorms hook")
    _poll_brainstorms = forge.worker.main._poll_brainstorms

    stop = asyncio.Event()

    class Worker:
        calls = 0

        async def run_once(self, *, stop_event):
            assert stop_event is stop
            self.calls += 1
            if self.calls == 1:
                raise BrainstormConflict("bounded conflict")
            stop.set()
            return uuid4()

    worker = Worker()
    await asyncio.wait_for(_poll_brainstorms(worker, stop, 0.01), 2)
    assert worker.calls == 2
