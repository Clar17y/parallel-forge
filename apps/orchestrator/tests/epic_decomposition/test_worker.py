"""Gateway result validation before durable authoring publication."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.domain.epic_brainstorm import AuthoringJobSnapshot, FrozenBriefContent, FrozenRequirement
from forge.domain.subscription import RouteBinding, RouteSpec, TaskBudget
from forge.tools.epic_brainstorm import BrainstormReadOnlyTools
from forge.worker.epic_decomposition import ValidatedDecompositionGateway

from .support import SupervisedGateway


def _snapshot() -> AuthoringJobSnapshot:
    route = RouteSpec(provider="fake", client="fake", model="fixture")
    return AuthoringJobSnapshot(
        job_id=uuid4(), epic_id=uuid4(), project_id=uuid4(), conversation_id=uuid4(),
        kind="decomposition", input_brief_revision_id=uuid4(), input_brief_digest="a" * 64,
        input_draft_digest="b" * 64,
        accepted_content=FrozenBriefContent(
            problem="Problem", requirements=(FrozenRequirement(requirement_id=uuid4(), text="Need", acceptance_criteria=("Done",)),),
            assumptions=("Keep assumption",), open_questions=("Open choice?",),
        ),
        expected_epic_version=1, conversation_version=2, prompt_turn_id=uuid4(),
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=1), reservation_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_actual_gateway_result_is_validated_after_supervised_process() -> None:
    job = _snapshot()
    gateway = ValidatedDecompositionGateway(SupervisedGateway())
    lifecycle = AsyncMock()
    result = await gateway.execute(
        job, (), BrainstormReadOnlyTools(AsyncMock()),
        cancelled=AsyncMock(return_value=False), lifecycle=lifecycle,
    )
    assert result.failure is None
    assert result.proposal is not None
    assert result.proposal.assumptions == job.accepted_content.assumptions
    assert lifecycle.started.called and lifecycle.finished.called
    assert lifecycle.finished.call_args.args[1].stop_confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda proposal: proposal.model_copy(update={"epic_id": uuid4()}),
        lambda proposal: proposal.model_copy(update={"assumptions": ()}),
        lambda proposal: proposal.model_copy(update={"items": ()}),
        lambda proposal: proposal.model_copy(update={"resolved_turn_ids": (uuid4(),)}),
        lambda proposal: proposal.model_copy(update={"summary": "", "problem": ""}),
        lambda proposal: proposal.model_copy(update={"summary": "x" * 9000}),
    ],
)
async def test_invalid_actual_gateway_result_keeps_telemetry_and_fails(mutate) -> None:
    gateway = ValidatedDecompositionGateway(SupervisedGateway(mutate))
    result = await gateway.execute(
        _snapshot(), (), BrainstormReadOnlyTools(AsyncMock()),
        cancelled=AsyncMock(return_value=False), lifecycle=AsyncMock(),
    )
    assert result.proposal is None
    assert result.failure == "invalid_output"
    assert result.telemetry is not None
