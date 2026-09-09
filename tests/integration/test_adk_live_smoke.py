"""Opt-in live provider smoke test for Google ADK gateway and runtime.

This test assembles the real GoogleAdkGateway, AdkRuntime, PromptLoader, and
a narrow empty-tool provider to verify that a structured Planner result can be
obtained from a live Google ADK model.

Per repository policy and Task 16 contract, this test is strictly opt-in:
it requires an explicit enable flag (FORGE_LIVE_ADK_ENABLED=1), an explicit
disposable credential reference (FORGE_LIVE_ADK_SECRET_REF), a model configuration
(FORGE_LIVE_ADK_MODEL), a live credential value (FORGE_LIVE_ADK_API_KEY),
and explicit pricing rates (FORGE_LIVE_ADK_INPUT_PER_MILLION and
FORGE_LIVE_ADK_OUTPUT_PER_MILLION and FORGE_LIVE_ADK_CACHED_INPUT_PER_MILLION).
If any of these are missing, the test is cleanly skipped.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from forge.agents.adk_gateway import BoundAdkTools, GoogleAdkGateway
from forge.agents.adk_runtime import AdkRuntime, AdkRuntimeProtocol
from forge.agents.prompt_loader import PromptLoader
from forge.application.ports.provider_credentials import (
    ProviderCredentialError,
    ProviderCredentialResolverPort,
)
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
from forge.domain.policy import RunnerMode
from forge.domain.review import FindingSeverity
from forge.observability.usage import PricingCatalog

pytestmark = [pytest.mark.live_provider, pytest.mark.asyncio]


class _DisposableEnvCredentialResolver(ProviderCredentialResolverPort):
    """Resolve live credentials from environment for opt-in smoke execution."""

    def __init__(self, key: str, expected_ref: str) -> None:
        self._key = key
        self._expected_ref = expected_ref

    async def resolve(self, reference: str) -> str:
        if reference != self._expected_ref:
            raise ProviderCredentialError("unknown credential reference")
        return self._key


class _EmptyAdkToolProvider:
    """Narrow empty tool provider for no-tools structured Planner smoke."""

    def tools_for(self, request: AgentRequest) -> BoundAdkTools:
        del request
        return BoundAdkTools(names=(), tools=())


def check_opt_in_preconditions() -> tuple[str, str, str, PricingCatalog]:
    """Verify explicit opt-in environment configuration or skip cleanly."""
    if os.environ.get("FORGE_LIVE_ADK_ENABLED") not in ("1", "true", "True"):
        pytest.skip("Live ADK smoke is disabled. Opt in with FORGE_LIVE_ADK_ENABLED=1")

    secret_ref = os.environ.get("FORGE_LIVE_ADK_SECRET_REF")
    if not secret_ref or not secret_ref.strip():
        pytest.skip("Live ADK smoke requires explicit FORGE_LIVE_ADK_SECRET_REF secret reference")

    api_key = os.environ.get("FORGE_LIVE_ADK_API_KEY")
    if not api_key or not api_key.strip():
        pytest.skip("Live ADK smoke requires explicit disposable FORGE_LIVE_ADK_API_KEY")

    model = os.environ.get("FORGE_LIVE_ADK_MODEL")
    if not model or not model.strip():
        pytest.skip("Live ADK smoke requires explicit FORGE_LIVE_ADK_MODEL configuration")

    input_rate = os.environ.get("FORGE_LIVE_ADK_INPUT_PER_MILLION")
    if not input_rate or not input_rate.strip():
        pytest.skip("Live ADK smoke requires explicit FORGE_LIVE_ADK_INPUT_PER_MILLION pricing")

    output_rate = os.environ.get("FORGE_LIVE_ADK_OUTPUT_PER_MILLION")
    if not output_rate or not output_rate.strip():
        pytest.skip("Live ADK smoke requires explicit FORGE_LIVE_ADK_OUTPUT_PER_MILLION pricing")

    cached_rate = os.environ.get("FORGE_LIVE_ADK_CACHED_INPUT_PER_MILLION")
    if not cached_rate or not cached_rate.strip():
        pytest.skip(
            "Live ADK smoke requires explicit FORGE_LIVE_ADK_CACHED_INPUT_PER_MILLION pricing"
        )

    rates: dict[str, str] = {
        "input_per_million": input_rate.strip(),
        "output_per_million": output_rate.strip(),
        "cached_input_per_million": cached_rate.strip(),
    }
    for rate_name, rate_str in rates.items():
        try:
            val = Decimal(rate_str)
            if val < 0:
                raise ValueError(f"{rate_name} must be non-negative: {rate_str!r}")
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Invalid pricing rate for {rate_name}: {rate_str!r}") from exc

    catalog = PricingCatalog.from_mapping(
        version="live-smoke-pricing",
        entries={f"google:{model.strip()}": rates},
        currency_minor_exponents={"USD": 2},
    )
    return secret_ref.strip(), api_key.strip(), model.strip(), catalog


def assemble_planner_smoke(
    *,
    runtime: AdkRuntimeProtocol,
    prompt_loader: PromptLoader,
    pricing_catalog: PricingCatalog,
    model: str,
    run_id: UUID | None = None,
    execution_id: UUID | None = None,
    task_id: UUID | None = None,
) -> tuple[GoogleAdkGateway, AgentRequest]:
    """Assemble a narrow, tool-free GoogleAdkGateway and Planner AgentRequest."""
    run_id = run_id or uuid4()
    execution_id = execution_id or uuid4()
    task_id = task_id or uuid4()
    loaded_prompt = prompt_loader.load(AgentRole.PLANNER)

    tool_provider = _EmptyAdkToolProvider()
    gateway = GoogleAdkGateway(
        runtime=runtime,
        prompt_loader=prompt_loader,
        tool_provider=tool_provider,
        pricing_catalog=pricing_catalog,
        supported_provider="google",
        currency="USD",
    )

    context = PlannerInput(
        original_task=UntrustedContent.from_text(
            "Implement a health check ping endpoint returning 200 OK.",
            source_kind=UntrustedSourceKind.TASK,
            source_reference="smoke-test-task",
        ),
        base_commit="0" * 40,
        repository_tree=UntrustedContent.from_text(
            "apps/\nsrc/\npyproject.toml",
            source_kind=UntrustedSourceKind.REPOSITORY_TREE,
            source_reference="smoke-repo-tree",
        ),
        relevant_instructions=(),
        policy_summary=PolicySummary(
            policy_id=uuid4(),
            policy_version=1,
            runner_mode=RunnerMode.DOCKER,
            trusted_project=False,
            required_checks=("pytest -q",),
            allowed_merge_methods=("squash",),
            publication_blocking_severities=(FindingSeverity.BLOCKER, FindingSeverity.MAJOR),
            merge_blocking_severities=(FindingSeverity.BLOCKER, FindingSeverity.MAJOR),
        ),
    )

    request = AgentRequest(
        execution_id=execution_id,
        run_id=run_id,
        task_id=task_id,
        role=AgentRole.PLANNER,
        context=context,
        parent_execution_id=None,
        provider="google",
        model=model,
        instruction_version=loaded_prompt.version,
        system_instruction=loaded_prompt.instruction,
        instruction_digest=loaded_prompt.digest,
        allowed_tools=(),
        budget=AgentBudget(
            max_input_tokens=100_000,
            max_output_tokens=8_000,
            max_tool_calls=20,
            max_duration_seconds=120,
            max_cost_minor=500,
        ),
    )

    return gateway, request


async def test_live_adk_planner_structured_output_smoke() -> None:
    """Opt-in live smoke: assemble real ADK boundary for one structured Planner result."""
    secret_ref, api_key, model, catalog = check_opt_in_preconditions()

    resolver = _DisposableEnvCredentialResolver(key=api_key, expected_ref=secret_ref)
    runtime = AdkRuntime(resolver, secret_ref)

    repo_root = Path(__file__).resolve().parents[2]
    prompt_loader = PromptLoader(repo_root / "agents")

    gateway, request = assemble_planner_smoke(
        runtime=runtime,
        prompt_loader=prompt_loader,
        pricing_catalog=catalog,
        model=model,
    )

    result = await gateway.execute(request)

    assert result.finish_status is AgentFinishStatus.SUCCEEDED
    assert isinstance(result.output, PlanOutput)
    assert bool(result.output.summary.strip())
    assert len(result.output.steps) > 0
    assert len(result.output.required_checks) > 0
    assert len(result.output.risks) > 0
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0
