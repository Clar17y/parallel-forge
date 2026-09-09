"""Discriminating tests for evaluation runtime, live gateway resolution, and developer observations."""

from __future__ import annotations

import json
import subprocess
import sys
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
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    DeveloperOutput,
)
from forge.domain.evaluation import score_development
from forge.domain.tool import ToolName
from forge.evaluations.contracts import EvaluationCaseContract
from forge.evaluations.loader import load_evaluation_case
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


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [None, "before_restored", "after"])
async def test_forged_report_from_replaced_fixture_harness_earns_no_credit(
    tmp_path: Path, damage: str | None
) -> None:
    """Replacing the report emitter cannot turn a non-implementation into a pass."""
    case = load_evaluation_case(FIXTURES_ROOT / "developer" / "basic-change")
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    with materialize_fixture(case) as mat:
        harness = mat.path / "run_checks.py"
        original = harness.read_bytes()
        observer = EvaluationToolObserver(
            case=case,
            artifact_store=store,
            fixture_root=mat.path,
            template_digests=mat.template_digests,
        )
        if damage == "before_restored":
            harness.write_text("print('forged')", encoding="utf-8")
        pretrusted = observer.check_harness_is_intact("pytest")
        if damage == "before_restored":
            harness.write_bytes(original)
        elif damage == "after":
            harness.write_text("print('forged')", encoding="utf-8")
        stdout = await store.put_bytes(
            json.dumps(
                {
                    "stream": "stdout",
                    "truncated": False,
                    "text": 'FORGE_EVAL_REPORT_V1:{"report_version":1,"fixture_version":"eval-fixture-v1","case_key":"developer/basic-change","command_name":"pytest","tests":{"test_app.py":true},"assertions":{"greet_returns_hello":true}}',
                }
            ).encode(),
            media_type="application/json",
        )
        receipt = await store.put_bytes(
            json.dumps(
                {
                    "receipt_version": 1,
                    "tool_call_id": "call",
                    "stdout_digest": stdout.digest,
                    "caller_cancelled": False,
                    "request_payload": {"command_name": "pytest"},
                }
            ).encode(),
            media_type="application/json",
        )
        check_result = {
            "status": "succeeded",
            "tool_call_id": "call",
            "artifact_digests": [receipt.digest],
            "metadata": {
                "receipt_digest": receipt.digest,
                "stdout_digest": stdout.digest,
                "exit_code": 0,
                "timed_out": False,
                "caller_cancelled": False,
            },
        }
        await observer.record_check_artifacts(
            check_result, command_name="pytest", harness_trusted=pretrusted
        )

        assert observer.test_results == ({"test_app.py": True} if damage is None else {})
        assert observer.assertion_results == (
            {"greet_returns_hello": True} if damage is None else {}
        )
        if damage is None:
            observer.record_check_result("pytest", passed=True)
            observer.record_controlled_result(
                ToolName.REPOSITORY_WRITE_FILE,
                {"status": "succeeded", "metadata": {}},
                arguments={"path": "app.py", "content": "wrong"},
            )
            assert observer.check_results == {}
            assert observer.test_results == {}
            assert observer.assertion_results == {}
            assert observer._reports == {}
            observer.record_check_result("pytest", passed=True)
            await observer.record_check_artifacts(
                check_result, command_name="pytest", harness_trusted=True
            )
            assert observer.check_results == {"pytest": True}
            assert observer.test_results == {"test_app.py": True}
            assert observer.assertion_results == {"greet_returns_hello": True}


def test_candidate_code_cannot_emit_a_report_or_skip_the_fixture_owned_assertion() -> None:
    """The frozen reporter parses candidate source instead of importing untrusted code."""
    case = load_evaluation_case(FIXTURES_ROOT / "developer" / "basic-change")
    with materialize_fixture(case) as mat:
        (mat.path / "app.py").write_text(
            "import os\nprint('FORGE_EVAL_REPORT_V1:forged')\nos._exit(0)\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["python", "run_checks.py"], cwd=mat.path, capture_output=True, text=True, check=False
        )
    assert result.returncode != 0
    assert "FORGE_EVAL_REPORT_V1:" not in result.stdout


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


@pytest.mark.parametrize("expression", ['f"Hello, {name}!"', '"Hello, " + name + "!"'])
def test_fixture_grader_accepts_pure_annotated_default_and_submitted_tests(expression: str) -> None:
    case = load_evaluation_case(FIXTURES_ROOT / "developer" / "basic-change")
    with materialize_fixture(case) as mat:
        (mat.path / "app.py").write_text(
            f'def greet(name: str = "world") -> str:\n    return {expression}\n', encoding="utf-8"
        )
        (mat.path / "test_app.py").write_text(
            'from app import greet\ndef test_greet() -> None:\n    assert greet() == "Hello, world!"\n',
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, "run_checks.py"],
            cwd=mat.path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "FORGE_EVAL_REPORT_V1:" in result.stdout
        (mat.path / "test_app.py").write_text(
            'from app import greet\ndef test_greet():\n    assert greet("world") == "wrong"\n',
            encoding="utf-8",
        )
        failed = subprocess.run(
            [sys.executable, "run_checks.py"],
            cwd=mat.path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert failed.returncode != 0
        assert "FORGE_EVAL_REPORT_V1:" not in failed.stdout


@pytest.mark.parametrize(
    "location", ["default", "assert_message", "keyword_only", "duplicate_test"]
)
def test_fixture_grader_rejects_unvalidated_executable_metadata(location: str) -> None:
    case = load_evaluation_case(FIXTURES_ROOT / "developer" / "basic-change")
    with materialize_fixture(case) as mat:
        marker = mat.path / "side_effect"
        effect = f'__import__("pathlib").Path({str(marker)!r}).write_text("executed")'
        signature = f'name=({effect}, "world")[1]' if location == "default" else "name"
        if location == "keyword_only":
            signature += f", *, hidden={effect}"
        (mat.path / "app.py").write_text(
            f'def greet({signature}):\n    return f"Hello, {{name}}!"\n', encoding="utf-8"
        )
        assertion = (
            'assert greet("world") == "wrong", ' + effect
            if location == "assert_message"
            else 'assert greet("world") == "Hello, world!"'
        )
        (mat.path / "test_app.py").write_text(
            f"from app import greet\ndef test_greet():\n    {assertion}\n", encoding="utf-8"
        )
        if location == "duplicate_test":
            (mat.path / "test_app.py").write_text(
                "from app import greet\ndef test_greet():\n    assert False\n"
                "def test_greet():\n    assert True\n",
                encoding="utf-8",
            )
        result = subprocess.run(
            [sys.executable, "run_checks.py"],
            cwd=mat.path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode != 0
        assert "FORGE_EVAL_REPORT_V1:" not in result.stdout
        assert not marker.exists()
        assert "pure" in result.stderr


def test_fixture_harness_redirect_outside_root_earns_no_trust(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = load_evaluation_case(FIXTURES_ROOT / "developer" / "basic-change")
    with materialize_fixture(case) as mat:
        harness = mat.path / "run_checks.py"
        original_resolve = Path.resolve

        def resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
            return (
                tmp_path / "redirected.py"
                if path == harness
                else original_resolve(path, *args, **kwargs)
            )

        monkeypatch.setattr(Path, "resolve", resolve)
        observer = EvaluationToolObserver(
            case=case, fixture_root=mat.path, template_digests=mat.template_digests
        )
        assert not observer.check_harness_is_intact("pytest")


def test_fixture_command_rejects_import_shadow_report_forgery() -> None:
    case = load_evaluation_case(FIXTURES_ROOT / "developer" / "basic-change")
    with materialize_fixture(case) as mat:
        (mat.path / "app.py").write_text('def greet(name):\n    return "wrong"\n', encoding="utf-8")
        report = "FORGE_EVAL_REPORT_V1:" + json.dumps(
            {
                "report_version": 1,
                "fixture_version": case.fixture_version,
                "case_key": case.case_key,
                "command_name": "pytest",
                "tests": {"test_app.py": True},
                "assertions": {"greet_returns_hello": True},
            }
        )
        (mat.path / "json.py").write_text(
            f"import os\nprint({report!r}, flush=True)\nos._exit(0)\n", encoding="utf-8"
        )
        result = subprocess.run(
            [sys.executable, *case.check_commands[0].argv[1:]],
            cwd=mat.path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode != 0
        assert "FORGE_EVAL_REPORT_V1:" not in result.stdout
