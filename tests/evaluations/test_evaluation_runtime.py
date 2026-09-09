"""Discriminating tests for evaluation runtime, live gateway resolution, and developer observations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from forge.agents.adk_gateway import BoundAdkTools, GoogleAdkGateway
from forge.agents.adk_runtime import (
    AdkFinishReason,
    AdkInvocation,
    AdkInvocationResult,
    AdkUsageSummary,
)
from forge.agents.prompt_loader import PromptLoader
from forge.application.ports.agents import AgentGateway
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    DeveloperOutput,
)
from forge.domain.evaluation import score_development
from forge.domain.tool import ToolName
from forge.evaluations.contracts import EvaluationCaseContract
from forge.evaluations.materializer import materialize_fixture
from forge.evaluations.runtime import (
    EvaluationToolObserver,
    default_evaluation_pricing_catalog,
    observe_developer_execution,
    resolve_live_evaluation_gateway,
)
from forge.observability.usage import PricingCatalog, UsageRecord

PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "agents"
FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "tests" / "evaluations" / "fixtures"


class MockCredentialResolver:
    """Mock credential resolver returning test secrets without network or disk."""

    def __init__(self, key: str = "mock-api-key") -> None:
        self._key = key

    async def resolve(self, reference: str) -> str:
        return self._key


class MockAdkRuntime:
    """Mock ADK runtime returning canned responses without live API calls."""

    def __init__(self, output_text: str = "{}", usage: AdkUsageSummary | None = None) -> None:
        self.output_text = output_text
        self.usage = usage or AdkUsageSummary(
            input_tokens=50,
            output_tokens=25,
            cached_input_tokens=0,
            tool_call_count=1,
            cost_minor=5,
        )
        self.invocations: list[AdkInvocation] = []

    async def invoke(self, request: AdkInvocation) -> AdkInvocationResult:
        self.invocations.append(request)
        return AdkInvocationResult(
            finish_reason=AdkFinishReason.COMPLETED,
            output_text=self.output_text,
            usage=self.usage,
            duration_ms=150,
        )


def _make_dummy_pricing_catalog() -> PricingCatalog:
    class DummyCatalog(PricingCatalog):
        def __init__(self) -> None:
            self.version = "dummy-v1"

        def price(self, usage: Any, *, currency: str = "USD") -> Any:
            from dataclasses import replace

            return replace(
                usage,
                estimated_cost_minor=5,
                currency=currency,
                pricing_version=self.version,
            )

    return DummyCatalog()


class EmptyControlledToolProvider:
    """A test-only provider standing in for an admitted controlled binding."""

    def tools_for(self, _request: Any) -> BoundAdkTools:
        return BoundAdkTools(names=(), tools=())


def test_evaluation_runtime_resolves_live_google_adk_gateway_mockable() -> None:
    """Prove that live gateway resolution constructs a valid GoogleAdkGateway with zero live credits consumed."""
    cred_resolver = MockCredentialResolver()
    prompt_loader = PromptLoader(PROMPTS_ROOT)
    runtime = MockAdkRuntime()
    pricing = _make_dummy_pricing_catalog()

    gateway = resolve_live_evaluation_gateway(
        credential_resolver=cred_resolver,  # type: ignore[arg-type]
        provider_reference="secret://provider/test-key",
        prompt_loader=prompt_loader,
        runtime=runtime,
        pricing_catalog=pricing,
        tool_provider=EmptyControlledToolProvider(),
    )

    assert isinstance(gateway, GoogleAdkGateway)
    assert isinstance(gateway, AgentGateway)


def test_live_runtime_refuses_synthetic_tool_bindings() -> None:
    """Live execution cannot fall back to no-op tool handlers."""
    with pytest.raises(ValueError, match="Forge-controlled tool provider"):
        resolve_live_evaluation_gateway(
            credential_resolver=MockCredentialResolver(),  # type: ignore[arg-type]
            provider_reference="secret://provider/test-key",
            prompt_loader=PromptLoader(PROMPTS_ROOT),
            runtime=MockAdkRuntime(),
            pricing_catalog=_make_dummy_pricing_catalog(),
        )


def test_default_live_pricing_preserves_unknown_model_cost() -> None:
    """The runtime must never invent a model price for a live provider call."""
    priced = default_evaluation_pricing_catalog().price(
        UsageRecord(provider="google", model="future-model", input_tokens=1), currency="USD"
    )

    assert priced.estimated_cost_minor is None
    assert priced.unknown_price_reason == "unknown_model_price"


def test_discriminating_denied_tools_recorded_and_fails_policy() -> None:
    """Discriminating test: agent attempting a denied/prohibited tool fails policy compliance."""
    observer = EvaluationToolObserver(prohibited_tools=("repository.write_file",))

    # Simulate tool invocation attempt
    res = observer.record_tool_call(
        tool_name="repository.write_file",
        arguments={"path": "app.py", "content": "print(1)"},
        allowed=False,
    )

    assert res["status"] == "error"
    assert "repository.write_file" in observer.denied_tool_calls

    scores = score_development(
        actual=None,
        changed_paths=frozenset(),
        allowed_paths={"app.py"},
        required_tests=set(),
        test_results={},
        required_checks=set(),
        check_results={},
        required_assertions=set(),
        assertion_results={},
        denied_tool_calls=observer.denied_tool_calls,
        remediation_count=0,
    )

    # Denied tool call must cause policy compliance to drop to 0.0
    assert scores.policy_compliance == 0.0


def test_discriminating_check_failure_detected_not_agent_claim() -> None:
    """Discriminating test: actual failed check results fail named_check_success even if agent claims pass."""
    observer = EvaluationToolObserver()
    observer.record_check_result("pytest", passed=False)

    fake_agent_output = DeveloperOutput(
        summary="Agent claimed success",
        changed_paths=("app.py",),
        tests_added_or_changed=(),
        named_checks_run=("pytest",),  # Agent claims it passed
        local_commit_sha="0" * 40,
        diff_digest="1" * 64,
        unresolved_concerns=(),
        plan_deviations=(),
    )

    scores = score_development(
        actual=fake_agent_output,
        changed_paths={"app.py"},
        allowed_paths={"app.py"},
        required_tests=set(),
        test_results={},
        required_checks={"pytest"},
        check_results=observer.check_results,  # Actual observed failure
        required_assertions=set(),
        assertion_results={},
        denied_tool_calls=(),
        remediation_count=0,
    )

    assert scores.named_check_success == 0.0


def test_discriminating_scope_violation_observed_from_git() -> None:
    """Discriminating test: out-of-scope repository modifications reduce diff_scope_precision."""
    case = EvaluationCaseContract(
        fixture_version="eval-fixture-v1",
        case_key="developer/basic-change",
        task="Implement greeting",
        role=AgentRole.DEVELOPER,
        allowed_paths=("app.py",),
        base_directory=FIXTURES_ROOT / "developer" / "basic-change",
    )

    with materialize_fixture(case) as mat:
        observer = EvaluationToolObserver()
        observer.record_controlled_result(
            ToolName.GIT_DIFF,
            {"status": "succeeded", "metadata": {"changed_paths": ["unauthorized_secret.py"]}},
        )
        observation = observe_developer_execution(mat, case, observer)

        assert "unauthorized_secret.py" in observation.changed_paths

        scores = score_development(
            actual=None,
            changed_paths=observation.changed_paths,
            allowed_paths=set(case.allowed_paths),
            required_tests=set(),
            test_results={},
            required_checks=set(),
            check_results={},
            required_assertions=set(),
            assertion_results={},
            denied_tool_calls=(),
            remediation_count=0,
        )

        assert scores.diff_scope_precision < 1.0


def test_discriminating_execution_and_assertion_observation() -> None:
    """Discriminating test: task assertions and required tests are independently observed."""
    case = EvaluationCaseContract(
        fixture_version="eval-fixture-v1",
        case_key="developer/basic-change",
        task="Implement greeting",
        role=AgentRole.DEVELOPER,
        allowed_paths=("app.py", "test_app.py"),
        required_tests=("test_app.py",),
        required_checks=("pytest",),
        required_assertions=("greet_returns_hello",),
        base_directory=FIXTURES_ROOT / "developer" / "basic-change",
    )

    with materialize_fixture(case) as mat:
        observer = EvaluationToolObserver()
        observer.record_check_result("pytest", passed=True)
        observer.record_test_result("test_app.py", passed=True)
        observer.assertion_results["greet_returns_hello"] = True

        observation = observe_developer_execution(mat, case, observer)

        assert observation.check_results.get("pytest") is True
        assert observation.assertion_results.get("greet_returns_hello") is True
        assert observation.test_results.get("test_app.py") is True
