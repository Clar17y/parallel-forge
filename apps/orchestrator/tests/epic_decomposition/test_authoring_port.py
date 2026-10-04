"""Unit tests for authoring port compatibility and adapter boundaries."""

from uuid import uuid4

import pytest
from forge.application.ports.epic_brainstorm import BriefInput
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brainstorm import (
    BoundEpicAuthoringAdapter,
    EpicBrainstormService,
)
from forge.domain.epic_brainstorm import AuthoringJobSnapshot, BrainstormConflict
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
