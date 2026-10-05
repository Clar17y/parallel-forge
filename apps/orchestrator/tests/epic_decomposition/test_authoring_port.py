"""Unit tests for authoring port compatibility and adapter boundaries."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from forge.application.ports.epic_brainstorm import BriefInput
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brainstorm import (
    BoundEpicAuthoringAdapter,
    EpicBrainstormService,
)
from forge.domain.epic_brainstorm import (
    AuthoringJobSnapshot,
    AuthoringOutcome,
    BrainstormConflict,
    BrainstormNotFound,
)
from forge.domain.subscription import RouteBinding, RouteSpec, TaskBudget


class FakeBriefPort:
    def __init__(self, epic_id, project_id):
        self.epic_id = epic_id
        self.project_id = project_id

    async def input(self, epic_id, *, for_update=False):
        return BriefInput(
            epic_id=self.epic_id,
            project_id=self.project_id,
            epic_version=1,
            draft_digest="a" * 64,
            accepted_revision_id=None,
            accepted_digest=None,
        )


@pytest.mark.asyncio
async def test_decomposition_authoring_adapter_rejects_cross_subject_binding() -> None:
    epic_id = uuid4()
    project_id = uuid4()
    other_epic = uuid4()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())

    service = EpicBrainstormService(
        None,
        lambda _: FakeBriefPort(epic_id, project_id),
        budget=TaskBudget(max_provider_attempts=1),
    )
    adapter = BoundEpicAuthoringAdapter(
        service,
        epic_id=epic_id,
        project_id=project_id,
        actor=actor,
        idempotency_key="key",
    )

    route = RouteSpec(provider="fake", client="fake", model="fixture")
    job = AuthoringJobSnapshot(
        job_id=uuid4(),
        epic_id=other_epic,  # Mismatched epic!
        project_id=project_id,
        conversation_id=uuid4(),
        kind="decomposition",
        input_brief_revision_id=None,
        input_brief_digest=None,
        input_draft_digest="a" * 64,
        expected_epic_version=1,
        conversation_version=1,
        prompt_turn_id=uuid4(),
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=1),
        reservation_id=uuid4(),
    )

    with pytest.raises(BrainstormConflict, match="authoring subject binding conflicts"):
        await adapter.submit(job)


@pytest.mark.asyncio
async def test_observe_checks_kind_with_single_job_read_and_legacy_behavior(monkeypatch) -> None:
    epic_id = uuid4()
    project_id = uuid4()
    job_id = uuid4()

    mock_row = MagicMock()
    mock_outcome = AuthoringOutcome(
        job_id=job_id,
        job_version=1,
        state="proposed",
    )
    job_reads = 0

    class FakeRepository:
        def __init__(self, session):
            self.session = session

        async def job(self, eid, pid, jid):
            nonlocal job_reads
            if jid != job_id:
                raise BrainstormNotFound("job not found")
            job_reads += 1
            return mock_row

        def decode_snapshot(self, row):
            return self.session.snapshot

        async def outcome(self, row):
            return mock_outcome

    import forge.application.services.epic_brainstorm as brainstorm_svc_mod
    monkeypatch.setattr(brainstorm_svc_mod, "PostgresBrainstormRepository", FakeRepository)

    class FakeSession:
        def __init__(self, snapshot):
            self.snapshot = snapshot

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

    session_checkouts = 0
    current_snapshot = MagicMock(kind="brainstorm")

    def session_factory():
        nonlocal session_checkouts
        session_checkouts += 1
        return FakeSession(current_snapshot)

    service = EpicBrainstormService(
        session_factory,
        lambda _: FakeBriefPort(epic_id, project_id),
        budget=TaskBudget(max_provider_attempts=1),
    )

    # 1. Unknown job raises BrainstormNotFound before kind check
    with pytest.raises(BrainstormNotFound, match="job not found"):
        await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=uuid4(), kind="decomposition"
        )

    # 2. Kind conflict: requested decomposition on brainstorm job
    current_snapshot.kind = "brainstorm"
    with pytest.raises(BrainstormConflict, match="authoring job kind conflicts with route"):
        await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=job_id, kind="decomposition"
        )

    # 3. Legacy behavior: kind=None does not reject brainstorm job
    outcome = await service.observe(
        epic_id=epic_id, project_id=project_id, job_id=job_id
    )
    assert outcome == mock_outcome

    # 4. Matching kind: requested decomposition on decomposition job; single session & single job read
    current_snapshot.kind = "decomposition"
    session_checkouts = 0
    job_reads = 0
    outcome = await service.observe(
        epic_id=epic_id, project_id=project_id, job_id=job_id, kind="decomposition"
    )
    assert outcome == mock_outcome
    assert session_checkouts == 1
    assert job_reads == 1
