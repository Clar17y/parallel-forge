"""Usage evidence retained when both structured-output attempts fail."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from forge.agents.adk_gateway import BoundAdkTools, GoogleAdkGateway
from forge.agents.adk_runtime import (
    AdkFinishReason,
    AdkInvocation,
    AdkInvocationResult,
    AdkUsageSummary,
)
from forge.agents.errors import AgentBudgetExceeded, AgentOutputInvalid
from forge.agents.prompt_loader import LoadedPrompt
from forge.application.services.planning import PlanningService, _safe_usage
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentBudget,
    AgentRequest,
    PlannerInput,
    PolicySummary,
    UntrustedContent,
    UntrustedSourceKind,
)
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.policy import AgentModelPolicy
from forge.domain.run import RunState
from forge.observability.usage import PricingCatalog, UsageRecord


class _Runtime:
    def __init__(self, results: list[AdkInvocationResult]) -> None:
        self._results = results

    async def invoke(self, _invocation: AdkInvocation) -> AdkInvocationResult:
        return self._results.pop(0)


class _Prompts:
    def verify_unchanged(self, request: AgentRequest) -> LoadedPrompt:
        return LoadedPrompt(
            role=request.role,
            version=request.instruction_version,
            instruction=request.system_instruction,
            digest=request.instruction_digest,
        )


class _Tools:
    def tools_for(self, _request: AgentRequest) -> BoundAdkTools:
        return BoundAdkTools(names=(), tools=())


def _request() -> AgentRequest:
    instruction = "<!-- forge-instruction-version: 1 -->\nReturn JSON only."
    return AgentRequest(
        execution_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        role=AgentRole.PLANNER,
        context=PlannerInput(
            original_task=UntrustedContent.from_text(
                "task", source_kind=UntrustedSourceKind.TASK, source_reference="task"
            ),
            base_commit="a" * 40,
            repository_tree=UntrustedContent.from_text(
                "(empty)", source_kind=UntrustedSourceKind.REPOSITORY_TREE, source_reference="."
            ),
            policy_summary=PolicySummary(policy_id=uuid4(), policy_version=1),
        ),
        provider="google",
        model="gemini-2.5-flash",
        instruction_version="1",
        system_instruction=instruction,
        instruction_digest=sha256(instruction.encode()).hexdigest(),
        allowed_tools=(),
        budget=AgentBudget(
            max_input_tokens=1_000,
            max_output_tokens=1_000,
            max_tool_calls=10,
            max_duration_seconds=10,
            max_cost_minor=100,
        ),
    )


def _result(
    *, input_tokens: int, output_tokens: int, duration_ms: int, tool_calls: int
) -> AdkInvocationResult:
    return AdkInvocationResult(
        finish_reason=AdkFinishReason.COMPLETED,
        output_text="{}",
        usage=AdkUsageSummary(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_call_count=tool_calls,
        ),
        provider_request_id="provider-request",
        duration_ms=duration_ms,
    )


def test_agent_output_invalid_detaches_usage_without_exposing_detail() -> None:
    measured = UsageRecord(provider="google", model="gemini", input_tokens=1)
    error = AgentOutputInvalid("untrusted output: TOP-SECRET", usage=measured)

    assert error.usage == measured
    assert error.usage is not measured
    assert "TOP-SECRET" not in str(error)
    assert "TOP-SECRET" not in repr(error)
    with pytest.raises(TypeError):
        AgentOutputInvalid(usage=cast(object, object()))


def test_budget_exceeded_detaches_measured_unknown_price_usage_without_detail() -> None:
    measured = UsageRecord(
        provider="google",
        model="gemini",
        input_tokens=11,
        pricing_version="v1",
        currency="USD",
        unknown_price_reason="pricing_unavailable",
    )
    error = AgentBudgetExceeded("provider response: TOP-SECRET", usage=measured)

    assert error.usage == measured
    assert error.usage is not measured
    assert "TOP-SECRET" not in str(error)
    assert "TOP-SECRET" not in repr(error)


@pytest.mark.asyncio
async def test_second_invalid_output_carries_aggregated_priced_usage() -> None:
    request = _request()
    gateway = GoogleAdkGateway(
        _Runtime(
            [
                _result(input_tokens=100, output_tokens=20, duration_ms=30, tool_calls=1),
                _result(input_tokens=200, output_tokens=40, duration_ms=50, tool_calls=2),
            ]
        ),
        _Prompts(),  # type: ignore[arg-type]
        _Tools(),  # type: ignore[arg-type]
        PricingCatalog.from_mapping(
            version="v1",
            entries={
                "google:gemini-2.5-flash": {
                    "input_per_million": "1",
                    "output_per_million": "2",
                    "cached_input_per_million": "0",
                }
            },
        ),
    )

    with pytest.raises(AgentOutputInvalid) as raised:
        await gateway.execute(request)

    usage = raised.value.usage
    assert usage is not None
    assert usage.input_tokens == 300
    assert usage.output_tokens == 60
    assert usage.duration_ms == 80
    assert usage.tool_call_count == 3
    assert usage.pricing_version == "v1"
    assert usage.estimated_cost_minor == 0
    assert usage.run_id == request.run_id
    assert usage.agent_execution_id == request.execution_id
    assert "{}" not in str(raised.value)
    assert "{}" not in repr(raised.value)


class _FailingGateway:
    def __init__(self, *, mismatched_identity: bool) -> None:
        self.mismatched_identity = mismatched_identity

    async def execute(self, request: AgentRequest) -> object:
        raise AgentOutputInvalid(
            usage=UsageRecord(
                provider=request.provider,
                model=request.model,
                prompt_version=request.instruction_version,
                input_tokens=300,
                output_tokens=60,
                duration_ms=80,
                tool_call_count=3,
                pricing_version="v1",
                estimated_cost_minor=7,
                currency="USD",
                run_id=uuid4() if self.mismatched_identity else request.run_id,
                agent_execution_id=request.execution_id,
            )
        )


class _Work:
    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _FailureCapturingPlanningService(PlanningService):
    def __init__(self, gateway: _FailingGateway) -> None:
        super().__init__(gateway, object(), object(), lambda _policy: object())  # type: ignore[arg-type]
        self.captured_usage: UsageRecord | None = None

    async def _snapshot(self, _work: object, command: CommandEnvelope) -> object:
        return SimpleNamespace(
            run=SimpleNamespace(id=command.run_id, state=RunState.CREATED, version=0),
            task=SimpleNamespace(id=uuid4()),
            policy=SimpleNamespace(
                planner_model=AgentModelPolicy(
                    provider="google", model="gemini-2.5-flash", max_cost_minor=100
                )
            ),
        )

    async def _read_context(self, _binding: object) -> PlannerInput:
        return _request().context

    def _load_prompt(self) -> LoadedPrompt:
        instruction = "<!-- forge-instruction-version: 1 -->\nReturn JSON only."
        return LoadedPrompt(
            role=AgentRole.PLANNER,
            version="1",
            instruction=instruction,
            digest=sha256(instruction.encode()).hexdigest(),
        )

    async def _store_json(self, _data: bytes) -> object:
        return object()

    async def _admit(self, *args: object) -> object:
        return SimpleNamespace(is_new=True)

    async def _failure(self, *args: object) -> str:
        self.captured_usage = _safe_usage(args[-2], args[3])  # type: ignore[arg-type]
        return "failure-recorded"


def _command() -> CommandEnvelope:
    now = datetime.now(UTC)
    return CommandEnvelope(
        id=uuid4(),
        run_id=uuid4(),
        command_type="start_planning",
        idempotency_key="test-failed-usage",
        payload={},
        status=CommandStatus.LEASED,
        expected_run_version=0,
        actor_id=None,
        payload_schema_version=1,
        attempt=1,
        available_at=now,
        lease_owner="worker",
        lease_expires_at=now,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatched_identity", [False, True])
async def test_planning_execute_passes_failed_gateway_usage_through_safe_binding(
    mismatched_identity: bool,
) -> None:
    service = _FailureCapturingPlanningService(
        _FailingGateway(mismatched_identity=mismatched_identity)
    )

    assert await service.execute(_command(), _Work()) == "failure-recorded"
    usage = service.captured_usage
    assert usage is not None
    if mismatched_identity:
        assert usage.input_tokens == 0
        assert usage.estimated_cost_minor is None
        assert usage.unknown_price_reason == "gateway_usage_unavailable"
    else:
        assert usage.input_tokens == 300
        assert usage.output_tokens == 60
        assert usage.duration_ms == 80
        assert usage.tool_call_count == 3
        assert usage.estimated_cost_minor == 7
