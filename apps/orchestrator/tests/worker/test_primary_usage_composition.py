"""Root agent usage binding retains admitted whole-run composition."""

from uuid import uuid4

import pytest
from forge.application.services.task_usage_composition import bind_agent_execution_usage
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentRequest
from forge.domain.task_usage_contract import RouteCohort, RouteMember


class BindingStore:
    def __init__(self) -> None:
        self.binding = None
        self.binds = 0

    async def binding_for_source(self, project_id, kind, source_id):
        if self.binding is not None:
            assert self.binding.subject.project_id == project_id
            assert self.binding.source.kind == kind
            assert self.binding.source.source_id == source_id
        return self.binding

    async def bind(self, binding):
        self.binding = binding
        self.binds += 1


@pytest.mark.asyncio
async def test_root_binding_preserves_whole_run_cohort_on_retry() -> None:
    project_id, run_id, execution_id = uuid4(), uuid4(), uuid4()
    admitted = RouteCohort.from_members(
        (
            RouteMember(role="primary", provider="google", model="gemini", ordinal=0),
            RouteMember(role="routine_implementation", provider="openai", model="codex", ordinal=1),
        )
    )
    request = AgentRequest.model_construct(
        execution_id=execution_id,
        run_id=run_id,
        role=AgentRole.PLANNER,
        provider="google",
        model="gemini",
        reasoning_effort="medium",
    )
    store = BindingStore()
    first = await bind_agent_execution_usage(
        store,
        request=request,
        project_id=project_id,
        configured_route=admitted,
        admitted_phase_id="plan",
    )
    assert first.configured_route == admitted
    assert first.effective_route.members[0].role == "planner"
    assert first.effective_route.members[0].model == "gemini"
    replay = await bind_agent_execution_usage(
        store,
        request=request,
        project_id=project_id,
        configured_route=admitted,
        admitted_phase_id="plan",
    )
    assert replay.work_unit_id == first.work_unit_id
    assert store.binds == 2
    with pytest.raises(ValueError, match="differs"):
        await bind_agent_execution_usage(
            store,
            request=request.model_copy(update={"model": "changed"}),
            project_id=project_id,
            configured_route=admitted,
            admitted_phase_id="plan",
        )
