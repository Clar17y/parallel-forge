from uuid import uuid4

import pytest
from forge.api.schemas.epic_brainstorm import JobSubmit
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brainstorm import EpicBrainstormService
from forge.domain.epic_brainstorm import BrainstormConflict
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    ReasoningEffort,
    RouteBinding,
    RouteSpec,
    TaskBudget,
)
from forge.persistence.models.project import Project
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository
from pydantic import ValidationError

from apps.orchestrator.tests.epic_brainstorm.test_persistence import BriefFixture


@pytest.mark.asyncio
async def test_explicit_route_is_durable_replayed_and_conflicts_on_change(
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
    default = RouteSpec(provider="fake", client="fake", model="fixture")
    selected = RouteSpec(provider="openai", client="codex_app_server", model="gpt-6-astra")
    service = EpicBrainstormService(
        brainstorm_session_factory,
        lambda _session: BriefFixture(epic_id, project_id),
        route=RouteBinding(requested=default, effective=default),
        budget=TaskBudget(max_provider_attempts=2),
    )
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor, key="create-route", text="Idea"
    )
    turn = (
        await service.turns(epic_id=epic_id, project_id=project_id, conversation_id=conversation_id)
    )[0]
    args = {
        "epic_id": epic_id,
        "project_id": project_id,
        "conversation_id": conversation_id,
        "prompt_turn_id": turn.turn_id,
        "expected_epic_version": 1,
        "expected_conversation_version": version,
        "actor": actor,
        "key": "selected-route",
    }
    receipt = await service.submit(**args, requested_route=selected)
    assert await service.submit(**args, requested_route=selected) == receipt
    with pytest.raises(BrainstormConflict, match="idempotency key payload conflicts"):
        await service.submit(
            **args,
            requested_route=RouteSpec(
                provider="openai", client="codex_app_server", model="gpt-6-luna"
            ),
        )
    async with brainstorm_session_factory() as session:
        row = await PostgresBrainstormRepository(session).job(epic_id, project_id, receipt.job_id)
        snapshot = PostgresBrainstormRepository.decode_snapshot(row)
        assert snapshot.route.effective == selected
    assert (
        await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    ).route == selected


def test_submit_route_contract_rejects_unknown_fields_and_paid_or_foreign_clients():
    base = {
        "project_id": str(uuid4()),
        "prompt_turn_id": str(uuid4()),
        "expected_epic_version": 1,
        "expected_conversation_version": 2,
    }
    route = {
        "provider": "openai",
        "client": "codex_app_server",
        "model": "gpt-6-astra",
        "effort": ReasoningEffort.LOW,
    }
    with pytest.raises(ValidationError):
        JobSubmit.model_validate({**base, "requested_route": {**route, "api_key": "synthetic"}})
    with pytest.raises(ValidationError):
        JobSubmit.model_validate({**base, "requested_route": {**route, "effort": "unsupported"}})
    from forge.application.services.epic_brainstorm import _validate_requested_route

    for invalid in (
        RouteSpec(**route, auth_mode=AuthMode.API_KEY),
        RouteSpec(**route, billing_mode=BillingMode.PAID_OPT_IN),
        RouteSpec(provider="openai", client="gemini_cli", model="gpt-6-astra"),
    ):
        with pytest.raises(ValueError, match="supported local subscription"):
            _validate_requested_route(invalid)
