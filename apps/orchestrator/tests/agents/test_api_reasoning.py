"""Focused test suite for reasoning strength in Forge API model policies."""

from __future__ import annotations

import json
from typing import Any, Literal, cast
from uuid import uuid4

import forge.agents.adk_runtime as adk_runtime_module
import pytest
from forge.agents.adk_gateway import BoundAdkTools, GoogleAdkGateway
from forge.agents.adk_runtime import (
    AdkFinishReason,
    AdkInvocation,
    AdkInvocationResult,
    AdkRuntime,
    AdkRuntimeError,
    AdkUsageSummary,
    build_adk_thinking_config,
)
from forge.agents.prompt_loader import LoadedPrompt, PromptLoader
from forge.application.ports.provider_credentials import ProviderCredentialResolverPort
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentBudget,
    AgentFinishStatus,
    AgentRequest,
    PlannerInput,
    PolicySummary,
    UntrustedContent,
    UntrustedSourceKind,
)
from forge.domain.plan import PlanOutput
from forge.domain.policy import AgentModelPolicy, ProjectPolicy, RunnerMode
from forge.observability.usage import PricingCatalog
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ScriptedLlm(BaseLlm):
    responses: list[LlmResponse]

    async def generate_content_async(
        self, _request: LlmRequest, stream: bool = False
    ) -> Any:
        assert stream is False
        while self.responses:
            response = self.responses.pop(0)
            yield response
            if response.partial is not True:
                return


class _FakeResolver(ProviderCredentialResolverPort):
    async def resolve(self, reference: str) -> str:
        assert reference == "secret://forge/offline_key"
        return "valid-api-key"


class _FakePromptLoader(PromptLoader):
    def __init__(self, prompt: LoadedPrompt) -> None:
        self._prompt = prompt

    def load(self, prompt_name: str) -> LoadedPrompt:
        return self._prompt

    def verify_unchanged(self, request: AgentRequest) -> LoadedPrompt:
        return self._prompt


class _FakeToolProvider:
    def tools_for(self, request: AgentRequest) -> BoundAdkTools:
        return BoundAdkTools(names=request.allowed_tools, tools=())


def _make_pricing_catalog(model: str = "gemini-3.8-flash") -> PricingCatalog:
    return PricingCatalog.from_mapping(
        version="fake-pricing-v1",
        entries={
            f"google:{model}": {
                "input_per_million": "1.00",
                "output_per_million": "2.00",
                "cached_input_per_million": "0.50",
            }
        },
        currency_minor_exponents={"USD": 2},
    )


def _make_planner_context() -> PlannerInput:
    task = UntrustedContent.from_text(
        "Design feature",
        source_kind=UntrustedSourceKind.TASK,
        source_reference="issue#1",
    )
    tree = UntrustedContent.from_text(
        "src/",
        source_kind=UntrustedSourceKind.REPOSITORY_TREE,
        source_reference="tree",
    )
    summary = PolicySummary(
        policy_id=uuid4(),
        policy_version=1,
        runner_mode=RunnerMode.DOCKER,
        trusted_project=False,
    )
    return PlannerInput(
        original_task=task,
        base_commit="a" * 40,
        repository_tree=tree,
        policy_summary=summary,
    )


def _make_agent_request(
    *,
    model: str = "gemini-3.5-flash",
    reasoning_effort: str | None = None,
) -> AgentRequest:
    context = _make_planner_context()
    instruction = "<!-- forge-instruction-version: prompt-v1 -->\nPerform planning"
    import hashlib

    digest = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    return AgentRequest(
        execution_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        role=AgentRole.PLANNER,
        context=context,
        provider="google",
        model=model,
        instruction_version="prompt-v1",
        system_instruction=instruction,
        instruction_digest=digest,
        allowed_tools=(),
        budget=AgentBudget(),
        reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Domain Policy Invariants
# ---------------------------------------------------------------------------


def test_agent_model_policy_default_reasoning_effort_is_none() -> None:
    policy = AgentModelPolicy()
    assert policy.reasoning_effort is None


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_agent_model_policy_accepts_valid_reasoning_efforts(effort: str) -> None:
    policy = AgentModelPolicy(reasoning_effort=cast(Any, effort))
    assert policy.reasoning_effort == effort


@pytest.mark.parametrize("invalid", ["none", "maximum", "minimal", "xhigh", "auto", ""])
def test_agent_model_policy_rejects_invalid_reasoning_effort(invalid: str) -> None:
    with pytest.raises(ValidationError):
        AgentModelPolicy(reasoning_effort=cast(Any, invalid))


def test_agent_model_policy_legacy_payload_roundtrip() -> None:
    legacy_dict = {
        "provider": "google",
        "model": "gemini-3.5-flash",
        "max_input_tokens": 100_000,
        "max_output_tokens": 16_000,
        "max_tool_calls": 100,
        "max_duration_seconds": 1800,
        "max_cost_minor": 1000,
    }
    policy = AgentModelPolicy.model_validate(legacy_dict)
    assert policy.reasoning_effort is None

    dumped = policy.model_dump(mode="json")
    assert dumped.get("reasoning_effort") is None


def test_agent_model_policy_changing_reasoning_preserves_other_fields() -> None:
    original = AgentModelPolicy(
        provider="google-custom",
        model="gemini-3.8-flash",
        max_input_tokens=200_000,
        max_output_tokens=32_000,
        max_tool_calls=50,
        max_duration_seconds=900,
        max_cost_minor=500,
        reasoning_effort=None,
    )
    updated = AgentModelPolicy(**{**original.model_dump(), "reasoning_effort": "high"})
    assert updated.reasoning_effort == "high"
    assert updated.provider == original.provider
    assert updated.model == original.model
    assert updated.max_input_tokens == original.max_input_tokens
    assert updated.max_output_tokens == original.max_output_tokens
    assert updated.max_tool_calls == original.max_tool_calls
    assert updated.max_duration_seconds == original.max_duration_seconds
    assert updated.max_cost_minor == original.max_cost_minor


# ---------------------------------------------------------------------------
# AgentRequest Invariants
# ---------------------------------------------------------------------------


def test_agent_request_default_reasoning_effort_is_none() -> None:
    request = _make_agent_request(reasoning_effort=None)
    assert request.reasoning_effort is None


def test_agent_request_preserves_explicit_reasoning_effort() -> None:
    request = _make_agent_request(reasoning_effort="medium")
    assert request.reasoning_effort == "medium"


def test_agent_request_rejects_invalid_reasoning_effort() -> None:
    with pytest.raises(ValidationError):
        _make_agent_request(reasoning_effort="unsupported_effort")


# ---------------------------------------------------------------------------
# Google ADK Thinking Config Resolution Invariants
# ---------------------------------------------------------------------------


def test_build_adk_thinking_config_omits_config_when_none() -> None:
    assert build_adk_thinking_config("gemini-3.5-flash", None) is None
    assert build_adk_thinking_config("gemini-2.5-flash", None) is None
    assert build_adk_thinking_config("gemini-3-pro", None) is None


@pytest.mark.parametrize(
    ("model", "effort", "expected_level"),
    [
        ("gemini-3.8-flash", "low", types.ThinkingLevel.LOW),
        ("gemini-3.8-flash", "medium", types.ThinkingLevel.MEDIUM),
        ("gemini-3.8-flash", "high", types.ThinkingLevel.HIGH),
        ("gemini-3.5-flash", "low", types.ThinkingLevel.LOW),
        ("gemini-3.5-flash", "medium", types.ThinkingLevel.MEDIUM),
        ("gemini-3.5-flash", "high", types.ThinkingLevel.HIGH),
        ("gemini-3.1-pro", "low", types.ThinkingLevel.LOW),
        ("gemini-3.1-pro", "medium", types.ThinkingLevel.MEDIUM),
        ("gemini-3.1-pro", "high", types.ThinkingLevel.HIGH),
    ],
)
def test_build_adk_thinking_config_gemini_3_models(
    model: str, effort: str, expected_level: types.ThinkingLevel
) -> None:
    config = build_adk_thinking_config(model, effort)
    assert config is not None
    assert config.thinking_level == expected_level
    assert config.thinking_budget is None


@pytest.mark.parametrize(
    ("model", "effort", "expected_level"),
    [
        ("gemini-3-pro", "low", types.ThinkingLevel.LOW),
        ("gemini-3-pro", "high", types.ThinkingLevel.HIGH),
        ("gemini-3.0-pro", "low", types.ThinkingLevel.LOW),
        ("gemini-3.0-pro", "high", types.ThinkingLevel.HIGH),
    ],
)
def test_build_adk_thinking_config_older_gemini_3_pro(
    model: str, effort: str, expected_level: types.ThinkingLevel
) -> None:
    config = build_adk_thinking_config(model, effort)
    assert config is not None
    assert config.thinking_level == expected_level
    assert config.thinking_budget is None


@pytest.mark.parametrize("model", ["gemini-3-pro", "gemini-3.0-pro"])
def test_build_adk_thinking_config_older_gemini_3_pro_rejects_medium(model: str) -> None:
    with pytest.raises(AdkRuntimeError):
        build_adk_thinking_config(model, "medium")


@pytest.mark.parametrize(
    ("model", "effort", "expected_budget"),
    [
        ("gemini-2.5-flash", "low", 1024),
        ("gemini-2.5-flash", "medium", 4096),
        ("gemini-2.5-flash", "high", 8192),
        ("gemini-2.5-pro", "low", 1024),
        ("gemini-2.5-pro", "medium", 4096),
        ("gemini-2.5-pro", "high", 8192),
    ],
)
def test_build_adk_thinking_config_gemini_2_5_models(
    model: str, effort: str, expected_budget: int
) -> None:
    config = build_adk_thinking_config(model, effort)
    assert config is not None
    assert config.thinking_budget == expected_budget
    assert config.thinking_level is None


@pytest.mark.parametrize("unsupported_model", ["gemini-1.5-pro", "gemini-1.5-flash", "custom-model", "gpt-4o"])
def test_build_adk_thinking_config_rejects_unsupported_model(unsupported_model: str) -> None:
    with pytest.raises(AdkRuntimeError):
        build_adk_thinking_config(unsupported_model, "low")


# ---------------------------------------------------------------------------
# GoogleAdkGateway Propagation and Retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_threads_reasoning_effort_to_invocation_and_repairs() -> None:
    invocations: list[AdkInvocation] = []

    class _CapturingRuntime:
        async def invoke(self, request: AdkInvocation) -> AdkInvocationResult:
            invocations.append(request)
            if len(invocations) == 1:
                # First call returns invalid schema JSON to trigger repair
                return AdkInvocationResult(
                    finish_reason=AdkFinishReason.COMPLETED,
                    output_text='{"invalid":"missing required fields"}',
                    usage=AdkUsageSummary(input_tokens=10, output_tokens=10),
                    duration_ms=100,
                )
            # Second call (repair) returns valid PlanOutput
            valid_plan = json.dumps({
                "summary": "repaired plan",
                "assumptions": ["assumption 1"],
                "affected_components": ["component 1"],
                "steps": ["step 1"],
                "required_checks": ["check 1"],
                "risks": ["risk 1"],
                "security_considerations": ["none"],
                "dependency_changes": ["none"],
            })
            return AdkInvocationResult(
                finish_reason=AdkFinishReason.COMPLETED,
                output_text=valid_plan,
                usage=AdkUsageSummary(input_tokens=10, output_tokens=10),
                duration_ms=100,
            )

    request = _make_agent_request(model="gemini-3.8-flash", reasoning_effort="high")
    loader = _FakePromptLoader(
        LoadedPrompt(
            role=AgentRole.PLANNER,
            version="prompt-v1",
            instruction=request.system_instruction,
            digest=request.instruction_digest,
        )
    )
    gateway = GoogleAdkGateway(
        runtime=cast(Any, _CapturingRuntime()),
        prompt_loader=loader,
        tool_provider=cast(Any, _FakeToolProvider()),
        pricing_catalog=_make_pricing_catalog(request.model),
    )

    result = await gateway.execute(request)
    assert result.finish_status == AgentFinishStatus.SUCCEEDED
    assert len(invocations) == 2
    # Both first call and repair retry retain the exact frozen reasoning_effort
    assert invocations[0].reasoning_effort == "high"
    assert invocations[1].reasoning_effort == "high"


# ---------------------------------------------------------------------------
# Scripted AdkRuntime Invariant Check (Actual LlmAgent ThinkingConfig)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "effort", "expected_level", "expected_budget"),
    [
        ("gemini-3.8-flash", "medium", types.ThinkingLevel.MEDIUM, None),
        ("gemini-2.5-flash", "low", None, 1024),
        ("gemini-3.5-flash", None, None, None),
    ],
)
async def test_adk_runtime_scripted_agent_config(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    effort: Literal["low", "medium", "high"] | None,
    expected_level: types.ThinkingLevel | None,
    expected_budget: int | None,
) -> None:
    captured_agents: list[LlmAgent] = []
    original_llm_agent_init = LlmAgent.__init__

    def _spy_init(self: LlmAgent, *args: Any, **kwargs: Any) -> None:
        original_llm_agent_init(self, *args, **kwargs)
        captured_agents.append(self)

    monkeypatch.setattr(LlmAgent, "__init__", _spy_init)

    valid_plan = json.dumps({
        "summary": "completed plan",
        "steps": ["step 1"],
        "required_checks": ["check 1"],
        "risks": ["risk 1"],
    })
    llm = _ScriptedLlm(
        model=model,
        responses=[LlmResponse(content=types.Content(role="model", parts=[types.Part(text=valid_plan)]))],
    )
    monkeypatch.setattr(adk_runtime_module, "Gemini", lambda **_kwargs: llm)

    runtime = AdkRuntime(_FakeResolver(), "secret://forge/offline_key")
    invocation = AdkInvocation(
        agent_name="planner",
        model=model,
        instruction="Plan",
        output_schema=PlanOutput,
        tools=(),
        user_id="user1",
        session_id="session1",
        user_payload_json='{"task":"test"}',
        max_input_tokens=1000,
        max_output_tokens=1000,
        max_tool_calls=10,
        max_duration_ms=5000,
        max_cost_minor=100,
        reasoning_effort=effort,
    )

    result = await runtime.invoke(invocation)
    assert result.finish_reason == AdkFinishReason.COMPLETED
    assert len(captured_agents) == 1
    agent = captured_agents[0]
    config = agent.generate_content_config
    if effort is None:
        assert config is None or config.thinking_config is None
    else:
        assert config is not None
        assert config.thinking_config is not None
        assert config.thinking_config.thinking_level == expected_level
        assert config.thinking_config.thinking_budget == expected_budget


def test_all_three_roles_policy_binding() -> None:
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path="C:/dev/repo",
        github_repository="test/repo",
        default_branch="main",
        planner_model=AgentModelPolicy(model="gemini-3.8-flash", reasoning_effort="low"),
        developer_model=AgentModelPolicy(model="gemini-3.5-flash", reasoning_effort="medium"),
        reviewer_model=AgentModelPolicy(model="gemini-3.1-pro", reasoning_effort="high"),
    )
    assert policy.planner_model.reasoning_effort == "low"
    assert policy.developer_model.reasoning_effort == "medium"
    assert policy.reviewer_model.reasoning_effort == "high"


def test_legacy_policy_digest_preservation() -> None:
    from forge.domain.operation import canonical_digest

    legacy_raw = {
        "id": str(uuid4()),
        "version": 1,
        "repository_path": "C:/dev/repo",
        "github_repository": "test/repo",
        "default_branch": "main",
        "planner_model": {
            "provider": "google",
            "model": "gemini-3.5-flash",
            "max_input_tokens": 100000,
            "max_output_tokens": 16000,
            "max_tool_calls": 100,
            "max_duration_seconds": 1800,
            "max_cost_minor": 1000,
        },
        "developer_model": {
            "provider": "google",
            "model": "gemini-3.5-flash",
            "max_input_tokens": 100000,
            "max_output_tokens": 16000,
            "max_tool_calls": 100,
            "max_duration_seconds": 1800,
            "max_cost_minor": 1000,
        },
        "reviewer_model": {
            "provider": "google",
            "model": "gemini-3.5-flash",
            "max_input_tokens": 100000,
            "max_output_tokens": 16000,
            "max_tool_calls": 100,
            "max_duration_seconds": 1800,
            "max_cost_minor": 1000,
        },
    }
    digest_before = canonical_digest(legacy_raw)
    policy = ProjectPolicy.model_validate(legacy_raw)
    assert policy.planner_model.reasoning_effort is None
    assert policy.developer_model.reasoning_effort is None
    assert policy.reviewer_model.reasoning_effort is None
    # Re-digesting the original document dict remains perfectly identical
    assert canonical_digest(legacy_raw) == digest_before
