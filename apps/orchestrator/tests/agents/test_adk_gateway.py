"""Contract and boundary tests for GoogleAdkGateway and raw AdkRuntime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import forge.agents.adk_runtime as adk_runtime_module
import pytest
from forge.agents.adk_gateway import (
    BoundAdkTools,
    GoogleAdkGateway,
)
from forge.agents.adk_runtime import (
    AdkFinishReason,
    AdkInvocation,
    AdkInvocationResult,
    AdkRuntime,
    AdkRuntimeError,
    AdkUsageSummary,
    _close_stream,
    _ObservedStream,
    _over_budget,
)
from forge.agents.errors import (
    AgentBudgetExceeded,
    AgentGatewayError,
    AgentOutputInvalid,
    AgentPromptDrift,
    AgentRepairFailure,
)
from forge.agents.prompt_loader import LoadedPrompt, PromptChanged, PromptLoader
from forge.application.ports.provider_credentials import (
    ProviderCredentialResolverPort,
)
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentBudget,
    AgentFinishStatus,
    AgentRequest,
    AgentResult,
    DeveloperOutput,
    ReviewOutput,
)
from forge.domain.plan import PlanOutput
from forge.domain.tool import ToolName
from forge.observability.usage import PricingCatalog, UsageRecord
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.adk.tools.function_tool import FunctionTool
from google.genai import types

_AGENTS_DIR = str(Path(__file__).resolve().parent)
if _AGENTS_DIR not in sys.path:
    sys.path.insert(0, _AGENTS_DIR)

_INTEGRATION_DIR = str(Path(__file__).resolve().parents[4] / "tests" / "integration")
if _INTEGRATION_DIR not in sys.path:
    sys.path.insert(0, _INTEGRATION_DIR)

from test_adk_live_smoke import (
    assemble_planner_smoke,
    check_opt_in_preconditions,
)
from test_contracts import (
    build_agent_budget,
    build_agent_request,
    build_developer_output,
    build_plan_output,
    build_review_output,
)

# ---------------------------------------------------------------------------
# Test Helpers and Fakes
# ---------------------------------------------------------------------------


def _dummy_tool(name: ToolName) -> FunctionTool:
    async def fn() -> dict[str, str]:
        return {"status": "ok"}

    fn.__name__ = name.value
    return FunctionTool(fn)


def _pricing_catalog(
    *,
    version: str = "test-catalog-v1",
    model: str = "gemini-2.5-flash",
    input_rate: str = "1.00",
    output_rate: str = "2.00",
    cached_rate: str = "0.50",
    currency: str = "USD",
) -> PricingCatalog:
    return PricingCatalog.from_mapping(
        version=version,
        entries={
            f"google:{model}": {
                "input_per_million": input_rate,
                "output_per_million": output_rate,
                "cached_input_per_million": cached_rate,
            }
        },
        currency_minor_exponents={currency: 2},
    )


class _FailingPriceCatalog(PricingCatalog):
    def __init__(self, *, fail_on_call: int) -> None:
        base = _pricing_catalog()
        super().__init__(
            version=base.version,
            entries=base._entries,  # type: ignore[attr-defined]
            currency_minor_exponents=base._currency_minor_exponents,  # type: ignore[attr-defined]
        )
        self._fail_on_call = fail_on_call
        self._price_calls = 0

    def price(self, usage: UsageRecord, *, currency: str) -> UsageRecord:
        self._price_calls += 1
        if self._price_calls >= self._fail_on_call:
            raise RuntimeError("provider pricing failure: TOP-SECRET")
        return super().price(usage, currency=currency)


class _InvalidPriceCatalog(_FailingPriceCatalog):
    def price(self, usage: UsageRecord, *, currency: str) -> UsageRecord:
        return None  # type: ignore[return-value] - broken catalog boundary


class _IdentityMutatingCatalog(_FailingPriceCatalog):
    def __init__(self, field: str) -> None:
        super().__init__(fail_on_call=99)
        self.field = field

    def price(self, usage: UsageRecord, *, currency: str) -> UsageRecord:
        priced = super().price(usage, currency=currency)
        replacements = {
            "input_tokens": 0,
            "provider_request_id": "wrong-request",
            "id": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            "created_at": datetime(2001, 1, 1, tzinfo=UTC),
        }
        return replace(priced, **{self.field: replacements[self.field]})


class _FakeAdkRuntime:
    """Deterministic fake AdkRuntimeProtocol."""

    def __init__(self, outcomes: list[AdkInvocationResult | BaseException] | None = None) -> None:
        self.outcomes: list[AdkInvocationResult | BaseException] = list(outcomes or [])
        self.invocations: list[AdkInvocation] = []

    async def invoke(self, request: AdkInvocation) -> AdkInvocationResult:
        self.invocations.append(request)
        if not self.outcomes:
            raise RuntimeError("FakeAdkRuntime has no more scripted outcomes")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakePromptLoader:
    """PromptLoader seam for gateway tests."""

    def __init__(
        self,
        *,
        fail_drift_on_attempt: int | None = None,
        fail_io: bool = False,
    ) -> None:
        self.fail_drift_on_attempt = fail_drift_on_attempt
        self.fail_io = fail_io
        self.verify_calls: list[AgentRequest] = []

    def load(self, role: AgentRole) -> LoadedPrompt:
        instruction = (
            "<!-- forge-instruction-version: 1 -->\nYou are a specialist. Follow exact rules."
        )
        digest = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
        return LoadedPrompt(
            role=role,
            version="1",
            instruction=instruction,
            digest=digest,
        )

    def verify_unchanged(self, request: AgentRequest) -> LoadedPrompt:
        self.verify_calls.append(request)
        call_count = len(self.verify_calls)
        if self.fail_drift_on_attempt is not None and call_count >= self.fail_drift_on_attempt:
            raise PromptChanged()
        if self.fail_io:
            raise RuntimeError("disk I/O error reading prompt")
        return LoadedPrompt(
            role=request.role,
            version=request.instruction_version,
            instruction=request.system_instruction,
            digest=request.instruction_digest,
        )


def _make_loader(
    *,
    fail_drift_on_attempt: int | None = None,
    fail_io: bool = False,
) -> PromptLoader:
    return cast(
        PromptLoader,
        _FakePromptLoader(fail_drift_on_attempt=fail_drift_on_attempt, fail_io=fail_io),
    )


class _FakeAdkToolProvider:
    """Tool provider fake yielding bound ADK tools."""

    def __init__(
        self,
        *,
        mismatched_names: tuple[ToolName, ...] | None = None,
        raise_error: bool = False,
    ) -> None:
        self.mismatched_names = mismatched_names
        self.raise_error = raise_error
        self.calls: list[AgentRequest] = []

    def tools_for(self, request: AgentRequest) -> BoundAdkTools:
        self.calls.append(request)
        if self.raise_error:
            raise RuntimeError("failed to resolve tools")
        names = (
            self.mismatched_names if self.mismatched_names is not None else request.allowed_tools
        )
        tools = tuple(_dummy_tool(n) for n in names)
        return BoundAdkTools(names=names, tools=tools)


def _make_invocation_result(
    output_text: str | None,
    *,
    finish_reason: AdkFinishReason = AdkFinishReason.COMPLETED,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cached_tokens: int = 0,
    tool_calls: int = 1,
    cost_minor: int | None = None,
    provider_request_id: str | None = "req-smoke-001",
    duration_ms: int = 120,
) -> AdkInvocationResult:
    return AdkInvocationResult(
        finish_reason=finish_reason,
        output_text=output_text,
        usage=AdkUsageSummary(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            tool_call_count=tool_calls,
            cost_minor=cost_minor,
        ),
        provider_request_id=provider_request_id,
        duration_ms=duration_ms,
    )


def _make_request(
    role: AgentRole = AgentRole.PLANNER,
    *,
    budget: AgentBudget | None = None,
    model: str = "gemini-2.5-flash",
    provider: str = "google",
    allowed_tools: tuple[ToolName, ...] | None = None,
) -> AgentRequest:
    version = "1"
    instruction = (
        f"<!-- forge-instruction-version: {version} -->\nYou are a specialist. Follow exact rules."
    )
    return build_agent_request(
        role=role,
        provider=provider,
        model=model,
        instruction_version=version,
        system_instruction=instruction,
        budget=budget
        or build_agent_budget(
            max_input_tokens=10_000,
            max_output_tokens=2_000,
            max_tool_calls=50,
            max_duration_seconds=60,
            max_cost_minor=500,
        ),
        allowed_tools=allowed_tools,
    )


# ---------------------------------------------------------------------------
# Constructor & Type Invariant Tests
# ---------------------------------------------------------------------------


def test_gateway_constructor_requires_valid_protocols_and_metadata() -> None:
    """GoogleAdkGateway constructor strictly validates all ports and metadata."""
    runtime = _FakeAdkRuntime()
    prompt_loader = _make_loader()
    tool_provider = _FakeAdkToolProvider()
    catalog = _pricing_catalog()

    # Valid construction succeeds
    gateway = GoogleAdkGateway(runtime, prompt_loader, tool_provider, catalog)
    assert gateway is not None

    # Invalid runtime protocol
    with pytest.raises(AgentGatewayError):
        GoogleAdkGateway(cast(Any, object()), prompt_loader, tool_provider, catalog)

    # Invalid prompt loader (missing verify_unchanged)
    with pytest.raises(AgentGatewayError):
        GoogleAdkGateway(runtime, cast(Any, object()), tool_provider, catalog)

    # Invalid tool provider
    with pytest.raises(AgentGatewayError):
        GoogleAdkGateway(runtime, prompt_loader, cast(Any, object()), catalog)

    # Invalid pricing catalog
    with pytest.raises(AgentGatewayError):
        GoogleAdkGateway(runtime, prompt_loader, tool_provider, cast(Any, object()))

    # Invalid supported_provider or currency
    with pytest.raises(AgentGatewayError):
        GoogleAdkGateway(
            runtime, prompt_loader, tool_provider, catalog, supported_provider="Bad Provider!"
        )
    with pytest.raises(AgentGatewayError):
        GoogleAdkGateway(runtime, prompt_loader, tool_provider, catalog, currency="us-dollar")


# ---------------------------------------------------------------------------
# All Roles Valid Typed Output and Exact Identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_returns_valid_typed_output_for_planner() -> None:
    """Planner request returns PlanOutput with exact request/result identity."""
    plan = build_plan_output()
    runtime = _FakeAdkRuntime([_make_invocation_result(plan.model_dump_json())])
    loader = _make_loader()
    tools = _FakeAdkToolProvider()
    catalog = _pricing_catalog()
    gateway = GoogleAdkGateway(runtime, loader, tools, catalog)

    request = _make_request(AgentRole.PLANNER)
    result = await gateway.execute(request)

    assert isinstance(result, AgentResult)
    assert result.finish_status is AgentFinishStatus.SUCCEEDED
    assert isinstance(result.output, PlanOutput)
    assert result.output == plan
    assert result.execution_id == request.execution_id
    assert result.role is AgentRole.PLANNER
    assert result.parent_execution_id == request.parent_execution_id
    assert result.provider == request.provider
    assert result.model == request.model
    assert result.instruction_digest == request.instruction_digest
    assert result.duration_ms == 120
    assert result.tool_call_count == 1
    assert isinstance(result.usage, UsageRecord)
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 50
    assert result.usage.provider_request_id == "req-smoke-001"
    assert result.usage.run_id == request.run_id
    assert result.usage.agent_execution_id == request.execution_id


@pytest.mark.asyncio
async def test_gateway_returns_valid_typed_output_for_developer() -> None:
    """Developer request returns DeveloperOutput with exact identity."""
    dev_output = build_developer_output()
    runtime = _FakeAdkRuntime([_make_invocation_result(dev_output.model_dump_json())])
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog())

    request = _make_request(AgentRole.DEVELOPER)
    result = await gateway.execute(request)

    assert result.finish_status is AgentFinishStatus.SUCCEEDED
    assert isinstance(result.output, DeveloperOutput)
    assert result.output == dev_output
    assert result.role is AgentRole.DEVELOPER
    assert result.execution_id == request.execution_id


@pytest.mark.asyncio
async def test_gateway_returns_valid_typed_output_for_reviewer() -> None:
    """Reviewer request returns ReviewOutput with exact identity."""
    rev_output = build_review_output()
    runtime = _FakeAdkRuntime([_make_invocation_result(rev_output.model_dump_json())])
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog())

    request = _make_request(AgentRole.REVIEWER)
    result = await gateway.execute(request)

    assert result.finish_status is AgentFinishStatus.SUCCEEDED
    assert isinstance(result.output, ReviewOutput)
    assert result.output == rev_output
    assert result.role is AgentRole.REVIEWER
    assert result.execution_id == request.execution_id


# ---------------------------------------------------------------------------
# Repair Attempt: Malformed Output & Second-Invalid Escalation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_output_gets_exactly_one_repair_with_remaining_budgets() -> None:
    """First malformed output triggers one repair attempt with remaining budgets and error envelope."""
    plan = build_plan_output()
    malformed_json = '{"summary": "missing required steps, checks, risks"}'
    first_result = _make_invocation_result(
        malformed_json,
        input_tokens=200,
        output_tokens=100,
        duration_ms=400,
        tool_calls=2,
    )
    second_result = _make_invocation_result(
        plan.model_dump_json(),
        input_tokens=300,
        output_tokens=150,
        duration_ms=500,
        tool_calls=1,
    )
    runtime = _FakeAdkRuntime([first_result, second_result])
    loader = _make_loader()
    gateway = GoogleAdkGateway(runtime, loader, _FakeAdkToolProvider(), _pricing_catalog())

    budget = build_agent_budget(
        max_input_tokens=1000,
        max_output_tokens=500,
        max_tool_calls=10,
        max_duration_seconds=5,
        max_cost_minor=200,
    )
    request = _make_request(AgentRole.PLANNER, budget=budget)
    result = await gateway.execute(request)

    # Completed successfully on the second attempt
    assert result.finish_status is AgentFinishStatus.SUCCEEDED
    assert isinstance(result.output, PlanOutput)
    assert result.output == plan
    assert len(runtime.invocations) == 2

    # Verify first invocation vs second repair invocation
    first_inv, repair_inv = runtime.invocations[0], runtime.invocations[1]
    assert repair_inv.agent_name == "planner"
    assert repair_inv.model == request.model
    assert repair_inv.instruction == request.system_instruction
    assert repair_inv.output_schema is PlanOutput
    assert repair_inv.tools == first_inv.tools
    assert repair_inv.user_id == str(request.run_id)
    assert repair_inv.session_id == str(request.execution_id)

    # Remaining budgets verified
    assert repair_inv.max_input_tokens == 1000 - 200
    assert repair_inv.max_output_tokens == 500 - 100
    assert repair_inv.max_tool_calls == 10 - 2
    assert repair_inv.max_duration_ms == (5 * 1000) - 400

    # Repair payload contains original_payload and validation_errors
    repair_payload = json.loads(repair_inv.user_payload_json)
    assert "original_payload" in repair_payload
    assert "validation_errors" in repair_payload
    assert isinstance(repair_payload["validation_errors"], list)
    assert len(repair_payload["validation_errors"]) > 0
    err = repair_payload["validation_errors"][0]
    assert err["message"] == "output does not match the required role schema"
    assert "type" in err
    assert "loc" in err

    # Aggregate usage asserted (multi-attempt request drops single provider_request_id)
    assert result.usage.input_tokens == 200 + 300
    assert result.usage.output_tokens == 100 + 150
    assert result.duration_ms == 400 + 500
    assert result.tool_call_count == 2 + 1
    assert result.usage.provider_request_id is None


@pytest.mark.asyncio
async def test_second_invalid_output_escalates_to_agent_output_invalid() -> None:
    """When the second attempt also produces invalid output, raise AgentOutputInvalid."""
    runtime = _FakeAdkRuntime(
        [
            _make_invocation_result("{}"),
            _make_invocation_result('{"incomplete": true}'),
        ]
    )
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog())

    request = _make_request(AgentRole.PLANNER)
    with pytest.raises(AgentOutputInvalid):
        await gateway.execute(request)

    assert len(runtime.invocations) == 2


# ---------------------------------------------------------------------------
# Discriminating Boundary Tests: Equality vs Overshoot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit_name", "kwargs_equal", "kwargs_overshoot"),
    [
        ("input_tokens", {"input_tokens": 1000}, {"input_tokens": 1001}),
        ("output_tokens", {"output_tokens": 500}, {"output_tokens": 501}),
        ("tool_calls", {"tool_calls": 10}, {"tool_calls": 11}),
        ("duration_ms", {"duration_ms": 2000}, {"duration_ms": 2001}),
        ("cost_minor", {"cost_minor": 100}, {"cost_minor": 101}),
    ],
)
async def test_first_attempt_budget_boundary_equality_vs_overshoot(
    limit_name: str,
    kwargs_equal: dict[str, Any],
    kwargs_overshoot: dict[str, Any],
) -> None:
    """Exact equality to limit succeeds; overshoot by 1 returns BUDGET_EXCEEDED."""
    plan_json = build_plan_output().model_dump_json()
    budget = build_agent_budget(
        max_input_tokens=1000,
        max_output_tokens=500,
        max_tool_calls=10,
        max_duration_seconds=2,
        max_cost_minor=100,
    )

    # 1. Exact equality succeeds
    runtime_eq = _FakeAdkRuntime([_make_invocation_result(plan_json, **kwargs_equal)])
    gateway_eq = GoogleAdkGateway(
        runtime_eq, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog()
    )
    res_eq = await gateway_eq.execute(_make_request(AgentRole.PLANNER, budget=budget))
    assert res_eq.finish_status is AgentFinishStatus.SUCCEEDED
    assert isinstance(res_eq.output, PlanOutput)

    # 2. Overshoot by 1 returns BUDGET_EXCEEDED without output
    runtime_over = _FakeAdkRuntime([_make_invocation_result(plan_json, **kwargs_overshoot)])
    gateway_over = GoogleAdkGateway(
        runtime_over, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog()
    )
    res_over = await gateway_over.execute(_make_request(AgentRole.PLANNER, budget=budget))
    assert res_over.finish_status is AgentFinishStatus.BUDGET_EXCEEDED
    assert res_over.output is None


@pytest.mark.asyncio
async def test_repair_attempt_aggregate_budget_boundary() -> None:
    """Repair attempt overshooting remaining limits returns BUDGET_EXCEEDED."""
    plan_json = build_plan_output().model_dump_json()
    budget = build_agent_budget(
        max_input_tokens=1000,
        max_output_tokens=500,
        max_tool_calls=10,
        max_duration_seconds=2,
        max_cost_minor=100,
    )

    # First attempt uses 600 input tokens (leaving 400).
    # Second attempt uses 401 input tokens (overshooting remaining 400).
    runtime = _FakeAdkRuntime(
        [
            _make_invocation_result("{}", input_tokens=600),
            _make_invocation_result(plan_json, input_tokens=401),
        ]
    )
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog())
    result = await gateway.execute(_make_request(AgentRole.PLANNER, budget=budget))

    assert result.finish_status is AgentFinishStatus.BUDGET_EXCEEDED
    assert result.output is None
    assert len(runtime.invocations) == 2


# ---------------------------------------------------------------------------
# Cancellation & Non-Completed Finish Reasons: No Extra Attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_propagates_immediately_without_extra_attempt() -> None:
    """asyncio.CancelledError from runtime propagates immediately with no repair attempt."""
    runtime = _FakeAdkRuntime([asyncio.CancelledError()])
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog())

    with pytest.raises(asyncio.CancelledError):
        await gateway.execute(_make_request(AgentRole.PLANNER))

    assert len(runtime.invocations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adk_reason", "expected_status"),
    [
        (AdkFinishReason.CANCELLED, AgentFinishStatus.CANCELLED),
        (AdkFinishReason.TIMED_OUT, AgentFinishStatus.TIMED_OUT),
        (AdkFinishReason.BUDGET_EXHAUSTED, AgentFinishStatus.BUDGET_EXCEEDED),
        (AdkFinishReason.FAILED, AgentFinishStatus.FAILED),
    ],
)
async def test_non_completed_finish_reasons_return_status_without_repair(
    adk_reason: AdkFinishReason,
    expected_status: AgentFinishStatus,
) -> None:
    """Non-completed finish reasons return matching status with no repair attempt."""
    runtime = _FakeAdkRuntime([_make_invocation_result(output_text=None, finish_reason=adk_reason)])
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog())

    result = await gateway.execute(_make_request(AgentRole.PLANNER))
    assert result.finish_status is expected_status
    assert result.output is None
    assert len(runtime.invocations) == 1


# ---------------------------------------------------------------------------
# Fail Closed: Unknown Pricing, Prompt Drift & Binding Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_pricing_fails_closed_before_invocation() -> None:
    """Unknown model or currency in PricingCatalog raises AgentBudgetExceeded before invoke."""
    runtime = _FakeAdkRuntime()
    # Catalog only has gemini-2.5-flash
    catalog = _pricing_catalog(model="gemini-2.5-flash", currency="USD")
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), catalog)

    # Request with unlisted model
    req_unknown_model = _make_request(AgentRole.PLANNER, model="gemini-unknown-model")
    with pytest.raises(AgentBudgetExceeded):
        await gateway.execute(req_unknown_model)
    assert len(runtime.invocations) == 0


@pytest.mark.asyncio
async def test_missing_cached_price_fails_closed_before_invocation() -> None:
    """Every supported price dimension must be configured before provider work."""
    runtime = _FakeAdkRuntime()
    catalog = PricingCatalog.from_mapping(
        version="test-catalog-v1",
        entries={
            "google:gemini-2.5-flash": {
                "input_per_million": "1.00",
                "output_per_million": "2.00",
            }
        },
    )
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), catalog)

    with pytest.raises(AgentBudgetExceeded):
        await gateway.execute(_make_request(AgentRole.PLANNER))
    assert runtime.invocations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_result", [False, True])
async def test_first_attempt_pricing_failure_retains_measured_unknown_price_usage(
    invalid_result: bool,
) -> None:
    runtime = _FakeAdkRuntime(
        [_make_invocation_result(build_plan_output().model_dump_json(), provider_request_id="one")]
    )
    catalog = (_InvalidPriceCatalog if invalid_result else _FailingPriceCatalog)(fail_on_call=1)
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), catalog)

    with pytest.raises(AgentBudgetExceeded) as raised:
        await gateway.execute(_make_request(AgentRole.PLANNER))

    usage = raised.value.usage
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.tool_call_count, usage.duration_ms) == (
        100,
        50,
        1,
        120,
    )
    assert usage.provider_request_id == "one"
    assert usage.estimated_cost_minor is None
    assert usage.unknown_price_reason == "pricing_unavailable"
    assert [attempt.provider_request_id for attempt in raised.value.usage_attempts] == ["one"]
    assert "TOP-SECRET" not in str(raised.value)
    assert len(runtime.invocations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_on_call", [2, 3])
async def test_repair_pricing_failure_retains_each_measured_attempt(fail_on_call: int) -> None:
    runtime = _FakeAdkRuntime(
        [
            _make_invocation_result("{}", input_tokens=10, provider_request_id="one"),
            _make_invocation_result(
                build_plan_output().model_dump_json(), input_tokens=20, provider_request_id="two"
            ),
        ]
    )
    gateway = GoogleAdkGateway(
        runtime,
        _make_loader(),
        _FakeAdkToolProvider(),
        _FailingPriceCatalog(fail_on_call=fail_on_call),
    )

    with pytest.raises(AgentBudgetExceeded) as raised:
        await gateway.execute(_make_request(AgentRole.PLANNER))

    usage = raised.value.usage
    assert usage is not None
    assert usage.input_tokens == 30
    assert usage.provider_request_id is None
    assert usage.estimated_cost_minor is None
    assert [attempt.provider_request_id for attempt in raised.value.usage_attempts] == [
        "one",
        "two",
    ]
    assert [attempt.input_tokens for attempt in raised.value.usage_attempts] == [10, 20]
    assert len(runtime.invocations) == 2


@pytest.mark.asyncio
async def test_prompt_drift_rejected_before_effect() -> None:
    """PromptChanged from PromptLoader is raised before runtime invocation."""
    runtime = _FakeAdkRuntime()
    loader = _make_loader(fail_drift_on_attempt=1)
    gateway = GoogleAdkGateway(runtime, loader, _FakeAdkToolProvider(), _pricing_catalog())

    with pytest.raises(PromptChanged):
        await gateway.execute(_make_request(AgentRole.PLANNER))
    assert len(runtime.invocations) == 0


@pytest.mark.asyncio
async def test_prompt_drift_before_repair_attempt_halts_execution() -> None:
    """PromptChanged occurring after first attempt halts execution before repair invocation."""
    runtime = _FakeAdkRuntime([_make_invocation_result("{}")])
    # Fails prompt verification on attempt 3 (since verify is called twice before first invoke)
    loader = _make_loader(fail_drift_on_attempt=3)
    gateway = GoogleAdkGateway(runtime, loader, _FakeAdkToolProvider(), _pricing_catalog())

    with pytest.raises(AgentPromptDrift) as raised:
        await gateway.execute(_make_request(AgentRole.PLANNER))
    assert len(runtime.invocations) == 1
    assert raised.value.usage is not None
    assert raised.value.usage.provider_request_id == "req-smoke-001"
    assert raised.value.usage_attempts[0].input_tokens == 100


@pytest.mark.asyncio
async def test_repair_runtime_failure_retains_first_measured_attempt() -> None:
    runtime = _FakeAdkRuntime(
        [
            _make_invocation_result("{}", provider_request_id="first-billed-request"),
            RuntimeError("untrusted provider failure"),
        ]
    )
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog())

    with pytest.raises(AgentRepairFailure) as raised:
        await gateway.execute(_make_request(AgentRole.PLANNER))

    assert len(runtime.invocations) == 2
    assert raised.value.usage is not None
    assert raised.value.usage.input_tokens == 100
    assert [item.provider_request_id for item in raised.value.usage_attempts] == [
        "first-billed-request"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["input_tokens", "provider_request_id", "id", "created_at"])
async def test_catalog_cannot_replace_measured_usage_or_request_identity(field: str) -> None:
    runtime = _FakeAdkRuntime([_make_invocation_result(build_plan_output().model_dump_json())])
    gateway = GoogleAdkGateway(
        runtime, _make_loader(), _FakeAdkToolProvider(), _IdentityMutatingCatalog(field)
    )

    with pytest.raises(AgentBudgetExceeded) as raised:
        await gateway.execute(_make_request(AgentRole.PLANNER))

    assert raised.value.usage is not None
    assert raised.value.usage.input_tokens == 100
    assert raised.value.usage.provider_request_id == "req-smoke-001"
    assert raised.value.usage.estimated_cost_minor is None
    assert raised.value.usage.id is None
    assert raised.value.usage.created_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_version",
    ["", "x" * 97, "ghp_" + "A" * 36],
    ids=["empty", "oversize", "credential-shaped"],
)
async def test_invalid_catalog_version_preserves_safe_unknown_usage(invalid_version: str) -> None:
    catalog = _pricing_catalog()

    class _VersionChangingRuntime(_FakeAdkRuntime):
        async def invoke(self, request: AdkInvocation) -> AdkInvocationResult:
            result = await super().invoke(request)
            catalog.version = invalid_version
            return result

    runtime = _VersionChangingRuntime(
        [_make_invocation_result(build_plan_output().model_dump_json())]
    )
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), catalog)

    with pytest.raises(AgentBudgetExceeded) as raised:
        await gateway.execute(_make_request(AgentRole.PLANNER))

    assert len(runtime.invocations) == 1
    assert raised.value.usage is not None
    assert raised.value.usage.input_tokens == 100
    assert raised.value.usage.pricing_version == "unavailable-v1"
    assert raised.value.usage.estimated_cost_minor is None


@pytest.mark.asyncio
async def test_wrong_provider_rejected_before_effect() -> None:
    """Request with unsupported provider is rejected before runtime invocation."""
    runtime = _FakeAdkRuntime()
    gateway = GoogleAdkGateway(runtime, _make_loader(), _FakeAdkToolProvider(), _pricing_catalog())

    req = _make_request(AgentRole.PLANNER, provider="unsupported_provider")
    with pytest.raises(AgentGatewayError):
        await gateway.execute(req)
    assert len(runtime.invocations) == 0


@pytest.mark.asyncio
async def test_mismatched_tool_binding_rejected_before_effect() -> None:
    """Tool provider returning tool names mismatched with allowed_tools raises AgentGatewayError."""
    runtime = _FakeAdkRuntime()
    mismatched_tools = _FakeAdkToolProvider(mismatched_names=(ToolName.REPOSITORY_WRITE_FILE,))
    gateway = GoogleAdkGateway(runtime, _make_loader(), mismatched_tools, _pricing_catalog())

    req = _make_request(AgentRole.PLANNER, allowed_tools=(ToolName.REPOSITORY_READ_FILE,))
    with pytest.raises(AgentGatewayError):
        await gateway.execute(req)
    assert len(runtime.invocations) == 0


@pytest.mark.asyncio
async def test_tool_provider_error_fails_closed() -> None:
    """Tool provider exception raises AgentGatewayError before runtime invocation."""
    runtime = _FakeAdkRuntime()
    failing_tools = _FakeAdkToolProvider(raise_error=True)
    gateway = GoogleAdkGateway(runtime, _make_loader(), failing_tools, _pricing_catalog())

    with pytest.raises(AgentGatewayError):
        await gateway.execute(_make_request(AgentRole.PLANNER))
    assert len(runtime.invocations) == 0


# ---------------------------------------------------------------------------
# Raw AdkRuntime & Stream Boundary Verification
# ---------------------------------------------------------------------------


def test_observed_stream_final_authored_event_selection_and_thought_exclusion() -> None:
    """_ObservedStream selects final event by requested author and ignores thoughts."""
    observed = _ObservedStream()
    author = "planner"

    # Event 1: From another author (should be ignored for output_text)
    event_other = SimpleNamespace(
        author="other_agent",
        content=SimpleNamespace(parts=[SimpleNamespace(text="intermediate draft", thought=None)]),
        usage_metadata=None,
    )
    observed.observe(event_other, expected_author=author)
    assert observed.last_text is None

    # Event 2: Thought event (thought=True should be ignored)
    event_thought = SimpleNamespace(
        author=author,
        content=SimpleNamespace(parts=[SimpleNamespace(text="thinking hard", thought=True)]),
        usage_metadata=None,
    )
    observed.observe(event_thought, expected_author=author)
    assert observed.last_text is None

    # Event 3: Thought event with string reasoning
    event_thought_str = SimpleNamespace(
        author=author,
        content=SimpleNamespace(
            parts=[SimpleNamespace(text="internal reasoning", thought="reasoning")]
        ),
        usage_metadata=None,
    )
    observed.observe(event_thought_str, expected_author=author)
    assert observed.last_text is None

    # Event 4: First authored content
    event_first = SimpleNamespace(
        author=author,
        content=SimpleNamespace(parts=[SimpleNamespace(text="draft plan", thought=None)]),
        usage_metadata=None,
    )
    observed.observe(event_first, expected_author=author)
    assert observed.last_text == "draft plan"

    # Event 5: Final authored content
    event_final = SimpleNamespace(
        author=author,
        content=SimpleNamespace(parts=[SimpleNamespace(text="final plan output", thought=None)]),
        usage_metadata=None,
    )
    observed.observe(event_final, expected_author=author)
    assert observed.last_text == "final plan output"


@pytest.mark.asyncio
async def test_close_stream_calls_aclose_cleanly() -> None:
    """_close_stream invokes aclose on the stream without error."""
    closed = False

    class StreamWithClose:
        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    await _close_stream(StreamWithClose())
    assert closed is True


def test_over_budget_detects_all_dimensions() -> None:
    """_over_budget returns True for any exceeded dimension."""
    inv = AdkInvocation(
        agent_name="planner",
        model="gemini-2.5-flash",
        instruction="system prompt",
        output_schema=PlanOutput,
        tools=(),
        user_id="uid",
        session_id="sid",
        user_payload_json='{"test":true}',
        max_input_tokens=100,
        max_output_tokens=50,
        max_tool_calls=5,
        max_duration_ms=1000,
        max_cost_minor=50,
    )

    # Within budget
    ok_usage = AdkUsageSummary(input_tokens=100, output_tokens=50, tool_call_count=5, cost_minor=50)
    assert not _over_budget(ok_usage, inv)

    # Input exceeded
    assert _over_budget(
        AdkUsageSummary(input_tokens=101, output_tokens=50, tool_call_count=5, cost_minor=50),
        inv,
    )

    # Output exceeded
    assert _over_budget(
        AdkUsageSummary(input_tokens=100, output_tokens=51, tool_call_count=5, cost_minor=50),
        inv,
    )

    # Tool calls exceeded
    assert _over_budget(
        AdkUsageSummary(input_tokens=100, output_tokens=50, tool_call_count=6, cost_minor=50),
        inv,
    )

    # Cost exceeded
    assert _over_budget(
        AdkUsageSummary(input_tokens=100, output_tokens=50, tool_call_count=5, cost_minor=51),
        inv,
    )


def test_adk_runtime_constructor_validations() -> None:
    """AdkRuntime enforces valid resolver and secret reference."""

    class DummyResolver(ProviderCredentialResolverPort):
        async def resolve(self, reference: str) -> str:
            return "api-key"

    resolver = DummyResolver()

    # Valid secret reference succeeds
    runtime = AdkRuntime(resolver, "secret://forge/provider_key")
    assert runtime is not None

    # Invalid resolver type
    with pytest.raises(AdkRuntimeError):
        AdkRuntime(cast(Any, object()), "secret://forge/provider_key")

    # Invalid secret reference scheme
    with pytest.raises(AdkRuntimeError):
        AdkRuntime(resolver, "invalid-reference-format")


# ---------------------------------------------------------------------------
# AdkRuntime Offline Integration Tests (Patched SDK Stream)
# ---------------------------------------------------------------------------


class _ScriptedLlm(BaseLlm):
    """Offline scripted BaseLlm that yields programmed LlmResponse items."""

    responses: list[LlmResponse]
    closed: asyncio.Event | None = None
    started: asyncio.Event | None = None
    hang: bool = False

    async def generate_content_async(
        self, _request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        assert stream is False
        if self.started is not None:
            self.started.set()
        try:
            if self.hang:
                await asyncio.sleep(60)
            while self.responses:
                response = self.responses.pop(0)
                yield response
                if response.partial is not True:
                    return
        finally:
            if self.closed is not None:
                self.closed.set()


def _adk_invocation(
    *,
    agent_name: str = "offline_planner",
    model: str = "gemini-2.5-flash",
    instruction: str = "offline test instruction",
    output_schema: type[Any] = PlanOutput,
    tools: tuple[FunctionTool, ...] = (),
    max_input_tokens: int = 1_000,
    max_output_tokens: int = 1_000,
    max_tool_calls: int = 10,
    max_duration_ms: int = 5_000,
    max_cost_minor: int = 1_000,
) -> AdkInvocation:
    return AdkInvocation(
        agent_name=agent_name,
        model=model,
        instruction=instruction,
        output_schema=output_schema,
        tools=tools,
        user_id="offline-user",
        session_id="offline-session",
        user_payload_json='{"task":"offline"}',
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_tool_calls=max_tool_calls,
        max_duration_ms=max_duration_ms,
        max_cost_minor=max_cost_minor,
    )


@pytest.mark.asyncio
async def test_adk_runtime_invoke_final_authored_event_after_earlier_event_consumes_to_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AdkRuntime.invoke consumes stream to exhaustion and selects final authored event."""
    tool_called = False

    async def step_tool() -> dict[str, str]:
        nonlocal tool_called
        tool_called = True
        return {"status": "ok"}

    step_tool.__name__ = "step_tool"
    tool = FunctionTool(step_tool)

    valid_final_json = json.dumps(
        {
            "summary": "final completed plan",
            "steps": ["step 1"],
            "required_checks": ["check 1"],
            "risks": ["risk 1"],
        }
    )

    responses = [
        # Turn 1: model emits interim text and a tool call
        LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(id="c1", name="step_tool", args={})
                    ),
                    types.Part(text='{"summary":"earlier intermediate draft"}'),
                ],
            ),
            partial=False,
        ),
        # Turn 2: model emits the final authored plan
        LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text=valid_final_json)],
            ),
            partial=False,
        ),
    ]

    model = _ScriptedLlm(model="gemini-2.5-flash", responses=responses)
    monkeypatch.setattr(adk_runtime_module, "Gemini", lambda **_kwargs: model)

    class _Resolver(ProviderCredentialResolverPort):
        async def resolve(self, reference: str) -> str:
            assert reference == "secret://forge/offline_key"
            return "valid-api-key"

    runtime = AdkRuntime(_Resolver(), "secret://forge/offline_key")
    request = _adk_invocation(agent_name="offline_planner", tools=(tool,))

    result = await runtime.invoke(request)

    assert result.finish_reason is AdkFinishReason.COMPLETED
    assert result.output_text == valid_final_json
    assert tool_called is True
    assert len(model.responses) == 0


@pytest.mark.asyncio
async def test_adk_runtime_invoke_timeout_closes_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stream duration timeout triggers _close_stream and sets BUDGET_EXHAUSTED."""
    closed = asyncio.Event()
    model = _ScriptedLlm(model="gemini-2.5-flash", responses=[], closed=closed, hang=True)
    monkeypatch.setattr(adk_runtime_module, "Gemini", lambda **_kwargs: model)

    class _Resolver(ProviderCredentialResolverPort):
        async def resolve(self, reference: str) -> str:
            return "valid-api-key"

    runtime = AdkRuntime(_Resolver(), "secret://forge/offline_key")
    request = _adk_invocation(max_duration_ms=50)

    result = await runtime.invoke(request)

    assert result.finish_reason is AdkFinishReason.BUDGET_EXHAUSTED
    assert result.output_text is None
    assert closed.is_set() is True


@pytest.mark.asyncio
async def test_adk_runtime_invoke_token_budget_closes_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over-budget token usage immediately closes stream and sets BUDGET_EXHAUSTED."""
    closed = asyncio.Event()
    responses = [
        LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text='{"summary":"intermediate"}')],
            ),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=10,
                candidates_token_count=200,
            ),
            partial=True,
        ),
        LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part(text='{"summary":"unreachable"}')],
            ),
            partial=False,
        ),
    ]
    model = _ScriptedLlm(model="gemini-2.5-flash", responses=responses, closed=closed)
    monkeypatch.setattr(adk_runtime_module, "Gemini", lambda **_kwargs: model)

    class _Resolver(ProviderCredentialResolverPort):
        async def resolve(self, reference: str) -> str:
            return "valid-api-key"

    runtime = AdkRuntime(_Resolver(), "secret://forge/offline_key")
    request = _adk_invocation(max_output_tokens=50)

    result = await runtime.invoke(request)

    assert result.finish_reason is AdkFinishReason.BUDGET_EXHAUSTED
    assert result.output_text is None
    assert closed.is_set() is True


@pytest.mark.asyncio
async def test_adk_runtime_invoke_caller_cancellation_closes_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller cancellation triggers _close_stream and re-raises CancelledError."""
    started = asyncio.Event()
    closed = asyncio.Event()
    model = _ScriptedLlm(
        model="gemini-2.5-flash",
        responses=[],
        started=started,
        closed=closed,
        hang=True,
    )
    monkeypatch.setattr(adk_runtime_module, "Gemini", lambda **_kwargs: model)

    class _Resolver(ProviderCredentialResolverPort):
        async def resolve(self, reference: str) -> str:
            return "valid-api-key"

    runtime = AdkRuntime(_Resolver(), "secret://forge/offline_key")
    request = _adk_invocation(max_duration_ms=10_000)

    task = asyncio.create_task(runtime.invoke(request))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed.is_set() is True


@pytest.mark.asyncio
async def test_adk_runtime_invoke_fresh_sessions_and_credential_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each invocation uses a fresh session service and explicit isolated client kwargs."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    created_sessions: list[Any] = []
    gemini_kwargs_captured: list[dict[str, Any]] = []

    orig_in_memory = InMemorySessionService

    def spy_session_service(*args: Any, **kwargs: Any) -> Any:
        session = orig_in_memory(*args, **kwargs)  # type: ignore[no-untyped-call]
        created_sessions.append(session)
        return session

    monkeypatch.setattr(adk_runtime_module, "InMemorySessionService", spy_session_service)

    def fake_gemini(**kwargs: Any) -> Any:
        gemini_kwargs_captured.append(kwargs)
        valid_json = json.dumps(
            {
                "summary": "isolated plan",
                "steps": ["step 1"],
                "required_checks": ["check 1"],
                "risks": ["risk 1"],
            }
        )
        return _ScriptedLlm(
            model=kwargs.get("model", "gemini-2.5-flash"),
            responses=[
                LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part(text=valid_json)],
                    ),
                    partial=False,
                )
            ],
        )

    monkeypatch.setattr(adk_runtime_module, "Gemini", fake_gemini)

    class _DualResolver(ProviderCredentialResolverPort):
        async def resolve(self, reference: str) -> str:
            if reference == "secret://forge/tenant_alpha":
                return "alpha-api-key"
            if reference == "secret://forge/tenant_beta":
                return "beta-api-key"
            raise ValueError(f"unknown reference {reference}")

    resolver = _DualResolver()
    runtime_alpha = AdkRuntime(resolver, "secret://forge/tenant_alpha")
    runtime_beta = AdkRuntime(resolver, "secret://forge/tenant_beta")

    request_alpha = _adk_invocation(agent_name="agent_alpha")
    request_beta = _adk_invocation(agent_name="agent_beta")

    result_alpha = await runtime_alpha.invoke(request_alpha)
    result_beta = await runtime_beta.invoke(request_beta)

    assert result_alpha.finish_reason is AdkFinishReason.COMPLETED
    assert result_beta.finish_reason is AdkFinishReason.COMPLETED

    assert len(created_sessions) == 2
    assert created_sessions[0] is not created_sessions[1]

    assert len(gemini_kwargs_captured) == 2
    assert gemini_kwargs_captured[0]["client_kwargs"] == {
        "api_key": "alpha-api-key",
        "vertexai": False,
    }
    assert gemini_kwargs_captured[1]["client_kwargs"] == {
        "api_key": "beta-api-key",
        "vertexai": False,
    }

    assert os.environ.get("GEMINI_API_KEY") is None
    assert os.environ.get("GOOGLE_API_KEY") is None


# ---------------------------------------------------------------------------
# Live Smoke Assembly and Opt-In Precondition Offline Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_smoke_assembly_and_offline_execution() -> None:
    """assemble_planner_smoke produces a valid no-tools request and executes through gateway."""
    plan = build_plan_output(
        summary="live smoke structured plan",
        steps=("step 1", "step 2"),
        required_checks=("pytest -q",),
        risks=("smoke risk",),
    )
    fake_runtime = _FakeAdkRuntime([_make_invocation_result(plan.model_dump_json())])
    prompt_loader = _make_loader()
    catalog = _pricing_catalog(model="gemini-2.5-flash")

    gateway, request = assemble_planner_smoke(
        runtime=fake_runtime,
        prompt_loader=prompt_loader,
        pricing_catalog=catalog,
        model="gemini-2.5-flash",
    )

    assert request.role is AgentRole.PLANNER
    assert request.allowed_tools == ()
    assert request.model == "gemini-2.5-flash"
    assert len(request.system_instruction) > 0

    result = await gateway.execute(request)

    assert result.finish_status is AgentFinishStatus.SUCCEEDED
    assert isinstance(result.output, PlanOutput)
    assert result.output.summary == "live smoke structured plan"
    assert result.output.steps == ("step 1", "step 2")


@pytest.mark.asyncio
async def test_real_prompt_loader_trailing_newline_instruction_is_accepted() -> None:
    """The gateway accepts the real prompt while preserving its exact bytes."""
    plan = build_plan_output(
        summary="live smoke structured plan",
        steps=("step 1", "step 2"),
        required_checks=("pytest -q",),
        risks=("smoke risk",),
    )
    fake_runtime = _FakeAdkRuntime([_make_invocation_result(plan.model_dump_json())])
    repo_root = Path(__file__).resolve().parents[4]
    prompt_loader = PromptLoader(repo_root / "agents")
    catalog = _pricing_catalog(model="gemini-2.5-flash")

    gateway, request = assemble_planner_smoke(
        runtime=fake_runtime,
        prompt_loader=prompt_loader,
        pricing_catalog=catalog,
        model="gemini-2.5-flash",
    )

    result = await gateway.execute(request)
    assert result.finish_status is AgentFinishStatus.SUCCEEDED


@pytest.mark.parametrize(
    ("env_override", "skip_fragment"),
    [
        ({"FORGE_LIVE_ADK_ENABLED": "0"}, "Live ADK smoke is disabled"),
        ({"FORGE_LIVE_ADK_ENABLED": None}, "Live ADK smoke is disabled"),
        (
            {"FORGE_LIVE_ADK_ENABLED": "1", "FORGE_LIVE_ADK_SECRET_REF": ""},
            "FORGE_LIVE_ADK_SECRET_REF",
        ),
        (
            {
                "FORGE_LIVE_ADK_ENABLED": "1",
                "FORGE_LIVE_ADK_SECRET_REF": "secret://live",
                "FORGE_LIVE_ADK_API_KEY": "",
            },
            "FORGE_LIVE_ADK_API_KEY",
        ),
        (
            {
                "FORGE_LIVE_ADK_ENABLED": "1",
                "FORGE_LIVE_ADK_SECRET_REF": "secret://live",
                "FORGE_LIVE_ADK_API_KEY": "live-key",
                "FORGE_LIVE_ADK_MODEL": "",
            },
            "FORGE_LIVE_ADK_MODEL",
        ),
        (
            {
                "FORGE_LIVE_ADK_ENABLED": "1",
                "FORGE_LIVE_ADK_SECRET_REF": "secret://live",
                "FORGE_LIVE_ADK_API_KEY": "live-key",
                "FORGE_LIVE_ADK_MODEL": "gemini-2.5-flash",
                "FORGE_LIVE_ADK_INPUT_PER_MILLION": "",
            },
            "FORGE_LIVE_ADK_INPUT_PER_MILLION",
        ),
        (
            {
                "FORGE_LIVE_ADK_ENABLED": "1",
                "FORGE_LIVE_ADK_SECRET_REF": "secret://live",
                "FORGE_LIVE_ADK_API_KEY": "live-key",
                "FORGE_LIVE_ADK_MODEL": "gemini-2.5-flash",
                "FORGE_LIVE_ADK_INPUT_PER_MILLION": "0.15",
                "FORGE_LIVE_ADK_OUTPUT_PER_MILLION": "",
            },
            "FORGE_LIVE_ADK_OUTPUT_PER_MILLION",
        ),
        (
            {
                "FORGE_LIVE_ADK_ENABLED": "1",
                "FORGE_LIVE_ADK_SECRET_REF": "secret://live",
                "FORGE_LIVE_ADK_API_KEY": "live-key",
                "FORGE_LIVE_ADK_MODEL": "gemini-2.5-flash",
                "FORGE_LIVE_ADK_INPUT_PER_MILLION": "0.15",
                "FORGE_LIVE_ADK_OUTPUT_PER_MILLION": "0.60",
            },
            "FORGE_LIVE_ADK_CACHED_INPUT_PER_MILLION",
        ),
    ],
)
def test_live_smoke_preconditions_disabled_or_missing_skips(
    monkeypatch: pytest.MonkeyPatch,
    env_override: dict[str, str | None],
    skip_fragment: str,
) -> None:
    """check_opt_in_preconditions cleanly skips when any required env var is missing."""
    for key in (
        "FORGE_LIVE_ADK_ENABLED",
        "FORGE_LIVE_ADK_SECRET_REF",
        "FORGE_LIVE_ADK_API_KEY",
        "FORGE_LIVE_ADK_MODEL",
        "FORGE_LIVE_ADK_INPUT_PER_MILLION",
        "FORGE_LIVE_ADK_OUTPUT_PER_MILLION",
        "FORGE_LIVE_ADK_CACHED_INPUT_PER_MILLION",
    ):
        monkeypatch.delenv(key, raising=False)

    for key, val in env_override.items():
        if val is not None:
            monkeypatch.setenv(key, val)

    with pytest.raises(pytest.skip.Exception, match=skip_fragment):
        check_opt_in_preconditions()


@pytest.mark.parametrize("invalid_rate", ["not-a-number", "-1.00", "abc"])
def test_live_smoke_preconditions_invalid_pricing_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    invalid_rate: str,
) -> None:
    """Invalid pricing rate values raise ValueError before provider execution."""
    monkeypatch.setenv("FORGE_LIVE_ADK_ENABLED", "1")
    monkeypatch.setenv("FORGE_LIVE_ADK_SECRET_REF", "secret://forge/live_ref")
    monkeypatch.setenv("FORGE_LIVE_ADK_API_KEY", "live-key")
    monkeypatch.setenv("FORGE_LIVE_ADK_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("FORGE_LIVE_ADK_INPUT_PER_MILLION", invalid_rate)
    monkeypatch.setenv("FORGE_LIVE_ADK_OUTPUT_PER_MILLION", "0.60")
    monkeypatch.setenv("FORGE_LIVE_ADK_CACHED_INPUT_PER_MILLION", "0.0375")

    with pytest.raises(ValueError, match="Invalid pricing rate"):
        check_opt_in_preconditions()


def test_live_smoke_preconditions_valid_opt_in_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When all opt-in variables are valid, check_opt_in_preconditions returns parsed components."""
    monkeypatch.setenv("FORGE_LIVE_ADK_ENABLED", "1")
    monkeypatch.setenv("FORGE_LIVE_ADK_SECRET_REF", "secret://forge/live_ref")
    monkeypatch.setenv("FORGE_LIVE_ADK_API_KEY", "live-key-123")
    monkeypatch.setenv("FORGE_LIVE_ADK_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("FORGE_LIVE_ADK_INPUT_PER_MILLION", "0.15")
    monkeypatch.setenv("FORGE_LIVE_ADK_OUTPUT_PER_MILLION", "0.60")
    monkeypatch.setenv("FORGE_LIVE_ADK_CACHED_INPUT_PER_MILLION", "0.0375")

    secret_ref, api_key, model, catalog = check_opt_in_preconditions()

    assert secret_ref == "secret://forge/live_ref"
    assert api_key == "live-key-123"
    assert model == "gemini-2.5-flash"
    assert catalog.version == "live-smoke-pricing"
