"""Tests for EvaluationService running deterministic suites into PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from forge.agents.adk_runtime import (
    AdkFinishReason,
    AdkInvocation,
    AdkInvocationResult,
    AdkUsageSummary,
)
from forge.agents.fake_gateway import FakeAgentGateway, FakeAgentStep
from forge.application.services.evaluations import EvaluationService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentFinishStatus,
    AgentRequest,
    AgentResult,
    DeveloperOutput,
    ReviewOutput,
)
from forge.domain.plan import PlanOutput
from forge.evaluations.contracts import EvaluationCaseContract
from forge.evaluations.loader import load_evaluation_cases, load_expected_output
from forge.observability.usage import PricingCatalog
from forge.persistence.models import (
    AgentExecution,
    ModelUsage,
    Project,
    Run,
    RunCommand,
    Step,
    ToolCall,
)
from forge.persistence.models.evaluation import EvaluationCase, EvaluationSuite
from forge.persistence.models.execution import RunEvent
from forge.persistence.models.project import ProjectPolicyVersion
from forge.persistence.repositories.evaluations import EvaluationConflict
from sqlalchemy import select

pytestmark = pytest.mark.asyncio

FIXTURES_ROOT = Path(__file__).resolve().parents[4] / "tests" / "evaluations" / "fixtures"
EXPECTED_ROOT = Path(__file__).resolve().parents[4] / "tests" / "evaluations" / "expected"


def _build_passing_fake_gateway() -> FakeAgentGateway:
    plan_data = load_expected_output(EXPECTED_ROOT / "planner-basic-change.json")
    review_data = load_expected_output(EXPECTED_ROOT / "reviewer-missing-authorization.json")
    dev_data = load_expected_output(EXPECTED_ROOT / "developer-basic-change.json")
    plan = PlanOutput.model_validate(plan_data)
    review = ReviewOutput.model_validate(review_data)
    dev = DeveloperOutput.model_validate(dev_data)
    return FakeAgentGateway(
        {
            AgentRole.PLANNER: [FakeAgentStep.success(plan, duration_ms=500, cost_minor=10)],
            AgentRole.REVIEWER: [FakeAgentStep.success(review, duration_ms=500, cost_minor=10)],
            AgentRole.DEVELOPER: [FakeAgentStep.success(dev, duration_ms=500, cost_minor=10)],
        }
    )


async def test_evaluation_service_runs_deterministic_suite_and_persists_lineage(
    tmp_path: Path, session_factory: Any
) -> None:
    gateway = _build_passing_fake_gateway()
    store = FilesystemArtifactStore(tmp_path / "artifacts")

    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=f"det-suite-{uuid4()}",
    )

    assert result.status == "passed"
    assert result.passed is True
    assert len(result.cases) == 3
    for case in result.cases:
        assert case.passed is True
        assert case.status == "passed"
        assert case.input_digest is not None
        assert case.output_digest is not None
        assert case.usage_id is not None

    # Verify PostgreSQL records
    async with session_factory() as session:
        suite_row = await session.scalar(
            select(EvaluationSuite).where(EvaluationSuite.id == result.suite_id)
        )
        assert suite_row is not None
        assert suite_row.status == "passed"
        policies = (await session.scalars(select(ProjectPolicyVersion))).all()
        assert policies and all(policy.document["runner_mode"] == "docker" for policy in policies)

        case_rows = (
            await session.scalars(
                select(EvaluationCase).where(EvaluationCase.suite_id == result.suite_id)
            )
        ).all()
        assert len(case_rows) == 3
        for cr in case_rows:
            assert cr.status == "passed"
            assert cr.input_artifact_digest is not None
            assert cr.output_artifact_digest is not None
            assert cr.model_usage_id is not None

            # Verify ModelUsage and AgentExecution are bound to run
            usage = await session.get(ModelUsage, cr.model_usage_id)
            assert usage is not None
            execution = await session.get(AgentExecution, usage.agent_execution_id)
            assert execution is not None
            assert execution.status == "SUCCEEDED"
            assert execution.run_id == usage.run_id

            # Evaluation fixture admission is an explicit operator-only lifecycle:
            # it may complete after its evaluation step settles, without creating
            # ordinary delivery approval, merge, or remote-command authority.
            run = await session.get(Run, execution.run_id)
            assert run is not None
            step = await session.get(Step, execution.step_id)
            assert step is not None
            assert run.state == "COMPLETED"
            assert step.kind == "evaluation"
            assert step.status == "SUCCEEDED"
            assert run.pending_gate is None
            assert run.branch_name is None or run.branch_name.startswith("forge/evaluation/")
            commands = (
                await session.scalars(select(RunCommand).where(RunCommand.run_id == run.id))
            ).all()
            assert commands == []
            events = (
                await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id))
            ).all()
            assert all(not event.event_type.startswith("approval.") for event in events)
            assert all("merge" not in event.event_type for event in events)


async def test_evaluation_service_exact_replay_preserves_results_without_repeating_work(
    tmp_path: Path, session_factory: Any
) -> None:
    gateway = _build_passing_fake_gateway()
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    idempotency_key = f"replay-key-{uuid4()}"

    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    first_result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=idempotency_key,
    )
    assert first_result.status == "passed"
    first_invocation_count_planner = gateway.invocation_count(AgentRole.PLANNER)

    # Replay with same idempotency key
    replay_result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=idempotency_key,
    )
    assert replay_result.suite_id == first_result.suite_id
    assert replay_result.status == "passed"
    assert len(replay_result.cases) == len(first_result.cases)
    # Gateway invocation count must not increase on replay
    assert gateway.invocation_count(AgentRole.PLANNER) == first_invocation_count_planner


async def test_evaluation_service_gateway_failure_records_failed_case(
    tmp_path: Path, session_factory: Any
) -> None:
    class FailingGateway:
        async def execute(self, request: AgentRequest) -> AgentResult:
            raise RuntimeError("provider API failure")

    store = FilesystemArtifactStore(tmp_path / "artifacts")
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=FailingGateway(),
    )

    result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=f"failing-suite-{uuid4()}",
    )

    assert result.status == "failed"
    assert result.passed is False
    for case in result.cases:
        assert case.passed is False
        assert case.status == "failed"
        assert case.error is not None

    async with session_factory() as session:
        suite_row = await session.scalar(
            select(EvaluationSuite).where(EvaluationSuite.id == result.suite_id)
        )
        assert suite_row is not None
        assert suite_row.status == "failed"


async def test_evaluation_service_invalid_output_fails_scoring(
    tmp_path: Path, session_factory: Any
) -> None:
    # Plan output missing required checks and risks
    bad_plan = PlanOutput.model_validate(
        {
            "summary": "Incomplete plan",
            "assumptions": [],
            "affected_components": ["wrong/component"],
            "steps": ["Step 1"],
            "required_checks": ["wrong/check"],
            "risks": ["wrong/risk"],
            "security_considerations": [],
            "dependency_changes": [],
        }
    )
    review_data = load_expected_output(EXPECTED_ROOT / "reviewer-missing-authorization.json")
    review = ReviewOutput.model_validate(review_data)
    dev_data = load_expected_output(EXPECTED_ROOT / "developer-basic-change.json")
    dev = DeveloperOutput.model_validate(dev_data)

    gateway = FakeAgentGateway(
        {
            AgentRole.PLANNER: [FakeAgentStep.success(bad_plan)],
            AgentRole.REVIEWER: [FakeAgentStep.success(review)],
            AgentRole.DEVELOPER: [FakeAgentStep.success(dev)],
        }
    )
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=f"invalid-output-suite-{uuid4()}",
    )

    assert result.status == "failed"
    planner_case = next(c for c in result.cases if c.role == AgentRole.PLANNER)
    assert planner_case.passed is False
    assert planner_case.metrics["component_recall"] == 0.0


async def test_evaluation_service_budget_ceiling_regression_fails(
    tmp_path: Path, session_factory: Any
) -> None:
    gateway = _build_passing_fake_gateway()
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    # Impose a ceiling lower than the measured cost (cost_minor is 10 in fake gateway)
    result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=f"ceiling-regression-{uuid4()}",
        ceilings={"estimated_cost_minor": 5.0},
    )

    assert result.status == "failed"
    assert "estimated_cost_minor" in result.regressions


async def test_evaluation_service_version_mismatch_fails_regression(
    tmp_path: Path, session_factory: Any
) -> None:
    gateway = _build_passing_fake_gateway()
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=f"ver-mismatch-{uuid4()}",
        baseline_fixture_version="old-fixture-v0",
    )

    assert result.status == "failed"
    assert any("versions differ" in r for r in result.regressions)


async def test_evaluation_service_live_mode_requires_provider_reference(
    tmp_path: Path, session_factory: Any
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
    )

    with pytest.raises(ValueError, match="explicit --provider-reference"):
        await service.run_suite(suite_name="live", provider_reference=None)


async def test_live_service_resolves_adk_and_persists_a_bound_planner_tool_receipt(
    tmp_path: Path, session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-default-gateway live path binds registry tools before ADK invokes them."""

    planner_case = next(
        case
        for case in load_evaluation_cases(FIXTURES_ROOT).values()
        if case.role is AgentRole.PLANNER
    )
    plan_payload = load_expected_output(EXPECTED_ROOT / "planner-basic-change.json")

    class _MockAdkRuntime:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def invoke(self, invocation: AdkInvocation) -> AdkInvocationResult:
            tools = {tool.name: tool for tool in invocation.tools}
            receipt = await tools["repository.list_files"].run_async(
                args={"path": "."},
                tool_context=SimpleNamespace(
                    invocation_id="evaluation-live", function_call_id="planner-read"
                ),
            )
            assert receipt["status"] == "succeeded"
            return AdkInvocationResult(
                finish_reason=AdkFinishReason.COMPLETED,
                output_text=json.dumps(plan_payload, separators=(",", ":")),
                usage=AdkUsageSummary(
                    input_tokens=5,
                    output_tokens=5,
                    cached_input_tokens=0,
                    tool_call_count=1,
                    cost_minor=1,
                ),
                duration_ms=10,
            )

    class _MockCredentialResolver:
        async def resolve(self, _reference: str) -> str:
            return "test-key"

    monkeypatch.setattr("forge.evaluations.runtime.AdkRuntime", _MockAdkRuntime)
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        credential_resolver=_MockCredentialResolver(),  # type: ignore[arg-type]
        pricing_catalog=PricingCatalog.from_mapping(
            version="test-pricing-v1",
            entries={
                "google:mock-model": {
                    "input_per_million": "1",
                    "output_per_million": "1",
                    "cached_input_per_million": "1",
                }
            },
        ),
    )

    result = await service.run_suite(
        suite_name="live",
        provider_reference="secret://forge/mock-key",
        live_model="mock-model",
        idempotency_key=f"live-bound-planner-{uuid4()}",
        cases=[planner_case],
    )

    assert result.status == "passed", result
    async with session_factory() as session:
        receipts = (await session.scalars(select(ToolCall))).all()
    assert len(receipts) == 1
    assert receipts[0].tool_name == "repository.list_files"
    assert receipts[0].status == "SUCCEEDED"


@pytest.mark.parametrize(
    ("working", "outside_scope", "teardown_outcome"),
    [
        (True, False, None),
        (False, False, None),
        (True, True, None),
        (True, False, "failed"),
        (True, False, "cancelled"),
        (True, False, "caller"),
        (True, False, "caller-failed"),
    ],
)
async def test_live_service_binds_developer_write_check_and_reviewer_diff(
    tmp_path: Path,
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    working: bool,
    outside_scope: bool,
    teardown_outcome: str | None,
) -> None:
    """No-default live roles receive durable managed paths before SDK tool invocation."""

    cases = [
        case
        for case in load_evaluation_cases(FIXTURES_ROOT).values()
        if case.role in {AgentRole.DEVELOPER, AgentRole.REVIEWER}
    ]
    outputs = {
        "developer": load_expected_output(EXPECTED_ROOT / "developer-basic-change.json"),
        "reviewer": load_expected_output(EXPECTED_ROOT / "reviewer-missing-authorization.json"),
    }
    invocation_ready = asyncio.Event()
    caller_cancel = teardown_outcome in {"caller", "caller-failed"}

    class _MockAdkRuntime:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def invoke(self, invocation: AdkInvocation) -> AdkInvocationResult:
            tools = {tool.name: tool for tool in invocation.tools}
            tool_name = (
                "repository.write_file" if invocation.agent_name == "developer" else "git.diff"
            )
            arguments = (
                {
                    "path": "app.py",
                    "content": "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
                }
                if invocation.agent_name == "developer"
                else {}
            )
            if invocation.agent_name == "developer" and not working:
                arguments["content"] = "def greet(name):\n    return 'wrong'\n"
            receipt = await tools[tool_name].run_async(
                args=arguments,
                tool_context=SimpleNamespace(
                    invocation_id=f"evaluation-{invocation.agent_name}",
                    function_call_id="managed-read",
                ),
            )
            if invocation.agent_name == "developer":
                observers = service._evaluation_tool_registry._observers.values()
                observer = next(
                    item for item in observers if item._case.role is AgentRole.DEVELOPER
                )
                assert (observer._fixture_root / "app.py").read_text(encoding="utf-8") == arguments[
                    "content"
                ]
            if invocation.agent_name == "developer" and outside_scope:
                extra = await tools["repository.write_file"].run_async(
                    args={"path": "unexpected.py", "content": "outside = True\n"},
                    tool_context=SimpleNamespace(
                        invocation_id="evaluation-developer", function_call_id="outside-write"
                    ),
                )
                assert extra["status"] == "succeeded"
            if invocation.agent_name == "developer":
                check = await tools["build.run_named_check"].run_async(
                    args={"command_name": "pytest"},
                    tool_context=SimpleNamespace(
                        invocation_id="evaluation-developer", function_call_id="named-check"
                    ),
                )
                assert check["status"] == ("succeeded" if working else "failed")
            assert receipt["status"] == "succeeded"
            if caller_cancel:
                invocation_ready.set()
                await asyncio.Event().wait()
            return AdkInvocationResult(
                finish_reason=AdkFinishReason.COMPLETED,
                output_text=json.dumps(outputs[invocation.agent_name], separators=(",", ":")),
                usage=AdkUsageSummary(
                    input_tokens=5, output_tokens=5, tool_call_count=1, cost_minor=1
                ),
                duration_ms=10,
            )

    class _Resolver:
        async def resolve(self, _reference: str) -> str:
            return "test-key"

    monkeypatch.setattr("forge.evaluations.runtime.AdkRuntime", _MockAdkRuntime)
    pricing = PricingCatalog.from_mapping(
        version="test-pricing-v1",
        entries={
            "google:mock-model": {
                "input_per_million": "1",
                "output_per_million": "1",
                "cached_input_per_million": "1",
            }
        },
    )
    from forge.settings import Settings

    service = EvaluationService(
        session_factory,
        FilesystemArtifactStore(tmp_path / "artifacts"),
        trusted_fixture_execution=True,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        credential_resolver=_Resolver(),
        pricing_catalog=pricing,
        settings=Settings(
            process_role="cli",
            data_root=tmp_path / "data",
            runner_image="registry.test/runner@sha256:" + "a" * 64,
        ),
    )  # type: ignore[arg-type]
    if teardown_outcome not in {None, "caller"}:

        async def fail_teardown(self: object, run_id: object, policy: object) -> None:
            del self, run_id, policy
            if teardown_outcome == "cancelled":
                invocation_ready.set()
                await asyncio.Event().wait()
            raise RuntimeError("injected teardown failure")

        monkeypatch.setattr(
            "forge.application.services.evaluations.DeliveryRuntime.teardown", fail_teardown
        )
    execution_task = asyncio.create_task(
        service.run_suite(
            suite_name="live",
            provider_reference="secret://forge/mock-key",
            live_model="mock-model",
            idempotency_key=f"live-bound-worktree-{uuid4()}",
            cases=cases,
        )
    )
    if caller_cancel or teardown_outcome == "cancelled":
        try:
            await asyncio.wait_for(invocation_ready.wait(), timeout=45)
            execution_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(execution_task, timeout=15)
        finally:
            if not execution_task.done():
                execution_task.cancel()
                await asyncio.gather(execution_task, return_exceptions=True)
        async with session_factory() as session:
            runs = (await session.scalars(select(Run))).all()
            assert len(runs) == 1
            assert runs[0].state == (
                "CANCELLED" if teardown_outcome == "caller" else "AWAITING_HUMAN_INTERVENTION"
            )
            suite_row = (await session.scalars(select(EvaluationSuite))).one()
            assert suite_row.status == "cancelled"
            case_rows = (await session.scalars(select(EvaluationCase))).all()
            assert sorted(case.status for case in case_rows) == ["failed", "skipped"]
            assert all(
                item.status != "RUNNING"
                for item in (await session.scalars(select(AgentExecution))).all()
            )
            assert all(
                item.status != "RUNNING" for item in (await session.scalars(select(Step))).all()
            )
            assert not (await session.scalars(select(RunCommand))).all()
            projects = (await session.scalars(select(Project))).all()
            assert len(projects) == 1
            assert Path(projects[0].canonical_path).exists() is (teardown_outcome != "caller")
        return
    result = await execution_task
    assert len(result.cases) == 2
    developer = next(case for case in result.cases if case.role is AgentRole.DEVELOPER)
    assert developer.metrics["named_check_success"] == float(working)
    assert developer.metrics["required_test_pass"] == float(working)
    assert developer.metrics["task_assertion_pass"] == float(working)
    observed = service._evaluation_tool_registry.observer_for(developer.execution_id)
    assert observed is not None
    assert observed.changed_paths == ({"app.py", "unexpected.py"} if outside_scope else {"app.py"})
    assert developer.passed is (working and not outside_scope and teardown_outcome is None)
    assert developer.metrics["diff_scope_precision"] == (0.5 if outside_scope else 1.0)
    async with session_factory() as session:
        receipts = (await session.scalars(select(ToolCall))).all()
        if teardown_outcome is not None:
            runs = (await session.scalars(select(Run))).all()
            case_rows = (await session.scalars(select(EvaluationCase))).all()
            assert len(runs) == 2
            assert all(run.state == "AWAITING_HUMAN_INTERVENTION" for run in runs)
            assert all(run.suspension_kind == "INTERVENTION" for run in runs)
            assert all(case.status == "failed" for case in case_rows)
            projects = (await session.scalars(select(Project))).all()
            assert all(Path(project.canonical_path).is_dir() for project in projects)
    assert {receipt.tool_name for receipt in receipts} == {
        "repository.write_file",
        "build.run_named_check",
        "git.diff",
    }
    assert len(receipts) == (4 if outside_scope else 3)


async def test_evaluation_service_uncertain_prior_execution_raises_conflict(
    tmp_path: Path, session_factory: Any
) -> None:
    gateway = _build_passing_fake_gateway()
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    idempotency_key = f"uncertain-{uuid4()}"

    # First initialize suite and manually set case status to 'running'
    from forge.persistence.repositories.evaluations import EvaluationRepository

    async with session_factory() as session, session.begin():
        repo = EvaluationRepository(session)
        suite = await repo.begin_suite(
            name="deterministic",
            fixture_version="eval-fixture-v1",
            metric_version="eval-metrics-v1",
            idempotency_key=idempotency_key,
            cases={"planner/basic-change": "planner"},
        )
        case_row = await repo.get_case(suite.id, "planner/basic-change")
        assert case_row is not None
        case_row.status = "running"

    case = EvaluationCaseContract(
        fixture_version="eval-fixture-v1",
        case_key="planner/basic-change",
        task="Task",
        role=AgentRole.PLANNER,
        base_directory=FIXTURES_ROOT / "planner" / "basic-change",
    )

    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    with pytest.raises(
        EvaluationConflict, match="uncertain prior execution|uncertain running execution"
    ):
        await service.run_suite(
            suite_name="deterministic",
            idempotency_key=idempotency_key,
            cases=[case],
        )


async def test_adversarial_p01_settled_replay_preserves_identities_and_detects_changed_floors(
    tmp_path: Path, session_factory: Any
) -> None:
    gateway = _build_passing_fake_gateway()
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    idempotency_key = f"adv-p01-{uuid4()}"

    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    first_result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=idempotency_key,
    )
    assert first_result.status == "passed"
    planner_1 = next(c for c in first_result.cases if c.role == AgentRole.PLANNER)

    # Replay with same key
    replay_result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=idempotency_key,
    )
    planner_replay = next(c for c in replay_result.cases if c.role == AgentRole.PLANNER)

    # P01: Settled replay must preserve persisted run_id and execution_id, NOT invent uuid4()
    assert planner_replay.run_id == planner_1.run_id
    assert planner_replay.execution_id == planner_1.execution_id
    assert planner_replay.usage_id == planner_1.usage_id

    # P01: Changed floors must fail, not return prior pass
    replay_with_impossible_floor = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=idempotency_key,
        floors={"cached_input_tokens": 99999.0},
    )
    assert replay_with_impossible_floor.passed is False
    assert replay_with_impossible_floor.status == "failed"
    assert "cached_input_tokens" in replay_with_impossible_floor.regressions


async def test_adversarial_p01_settled_replay_rejects_changed_fixture_content(
    tmp_path: Path, session_factory: Any
) -> None:
    gateway = _build_passing_fake_gateway()
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    idempotency_key = f"adv-p01-content-{uuid4()}"

    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    await service.run_suite(
        suite_name="deterministic",
        idempotency_key=idempotency_key,
    )

    # Attempt replay with modified case contract (same case_key and fixture_version, but changed task)
    modified_case = EvaluationCaseContract(
        fixture_version="eval-fixture-v1",
        case_key="planner/basic-change",
        task="TAMPERED: Different task specification entirely",
        role=AgentRole.PLANNER,
        base_directory=FIXTURES_ROOT / "planner" / "basic-change",
        expected_components=("apps/web",),
        expected_checks=("pytest",),
        expected_risks=("regression",),
    )

    rev_case = next(
        c for c in load_evaluation_cases(FIXTURES_ROOT).values() if c.role == AgentRole.REVIEWER
    )
    dev_case = next(
        c for c in load_evaluation_cases(FIXTURES_ROOT).values() if c.role == AgentRole.DEVELOPER
    )
    with pytest.raises(EvaluationConflict, match="fixture content identity mismatch"):
        await service.run_suite(
            suite_name="deterministic",
            idempotency_key=idempotency_key,
            cases=[modified_case, rev_case, dev_case],
        )


async def test_adversarial_p02_concurrent_admission_executes_gateway_once(
    tmp_path: Path, session_factory: Any
) -> None:
    import asyncio

    class DelayedGateway:
        def __init__(self, inner: FakeAgentGateway) -> None:
            self._inner = inner
            self.invocations: list[AgentRole] = []

        async def execute(self, request: AgentRequest) -> AgentResult:
            self.invocations.append(request.role)
            await asyncio.sleep(0.05)
            return await self._inner.execute(request)

    base_gw = _build_passing_fake_gateway()
    gateway = DelayedGateway(base_gw)
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    idempotency_key = f"adv-p02-concur-{uuid4()}"

    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,  # type: ignore[arg-type]
    )

    # Launch two concurrent runs for the same suite
    results = await asyncio.gather(
        service.run_suite(suite_name="deterministic", idempotency_key=idempotency_key),
        service.run_suite(suite_name="deterministic", idempotency_key=idempotency_key),
        return_exceptions=True,
    )

    # At least one caller must succeed
    successful_runs = [r for r in results if not isinstance(r, Exception)]
    assert len(successful_runs) >= 1

    # Gateway invocation count must be exactly 1 per role (never duplicate executions)
    planner_calls = sum(1 for r in gateway.invocations if r == AgentRole.PLANNER)
    reviewer_calls = sum(1 for r in gateway.invocations if r == AgentRole.REVIEWER)
    assert planner_calls == 1
    assert reviewer_calls == 1


async def test_adversarial_p03_valid_policy_run_snapshot_and_real_diff(
    tmp_path: Path, session_factory: Any
) -> None:
    import json

    from forge.domain.policy import ProjectPolicy
    from forge.persistence.models import Project, ProjectPolicyVersion, Run

    gateway = _build_passing_fake_gateway()
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    idempotency_key = f"adv-p03-{uuid4()}"

    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=idempotency_key,
    )
    assert result.status == "passed"

    async with session_factory() as session:
        # Check Project and ProjectPolicyVersion
        project_policy_row = await session.scalar(select(ProjectPolicyVersion))
        assert project_policy_row is not None
        # P03: Must NOT be placeholder a*64
        assert project_policy_row.policy_digest != "a" * 64
        assert len(project_policy_row.policy_digest) == 64

        # Validate that document parses as actual ProjectPolicy
        policy_model = ProjectPolicy.model_validate(project_policy_row.document)
        assert policy_model.repository_path is not None

        # Check Project github_repository format: must be valid owner/repo without extra slashes
        proj_row = await session.get(Project, project_policy_row.project_id)
        assert proj_row is not None
        assert proj_row.github_repository.count("/") == 1

        # The dedicated fixture admission may reach terminal completion without
        # ordinary delivery approval or merge authority.
        run_row = await session.scalar(select(Run))
        assert run_row is not None
        assert run_row.state == "COMPLETED"
        assert run_row.policy_version == 1
        step_row = await session.scalar(select(Step).where(Step.run_id == run_row.id))
        assert step_row is not None
        assert step_row.kind == "evaluation"
        assert step_row.status == "SUCCEEDED"
        assert (
            await session.scalars(select(RunCommand).where(RunCommand.run_id == run_row.id))
        ).all() == []

    # Check Reviewer input artifact: current_diff must NOT be "evaluation diff"
    reviewer_case = next(c for c in result.cases if c.role == AgentRole.REVIEWER)
    assert reviewer_case.input_digest is not None
    input_bytes = await store.open_bytes(reviewer_case.input_digest)
    input_json = json.loads(input_bytes.decode("utf-8"))
    context = input_json.get("context", input_json)
    diff_text = context["current_diff"].get("content") or context["current_diff"].get("text", "")
    # P03: Must contain actual git diff, NOT placeholder "evaluation diff"
    assert diff_text != "evaluation diff"
    assert "diff --git" in diff_text
    assert "api.py" in diff_text


async def test_adversarial_p04_gateway_error_and_identity_validation(
    tmp_path: Path, session_factory: Any
) -> None:
    import json

    from forge.persistence.models import ModelUsage

    class SecretLeakingFailingGateway:
        async def execute(self, request: AgentRequest) -> AgentResult:
            raise RuntimeError("secret_token_12345 failed to authenticate")

    store = FilesystemArtifactStore(tmp_path / "artifacts")
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=SecretLeakingFailingGateway(),
    )

    result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=f"adv-p04-error-{uuid4()}",
    )
    assert result.status == "failed"

    # P04: Output artifact must not leak raw exception str
    for case in result.cases:
        assert case.output_digest is not None
        out_bytes = await store.open_bytes(case.output_digest)
        out_json = json.loads(out_bytes.decode("utf-8"))
        # Must be redacted and stable
        assert "secret_token_12345" not in json.dumps(out_json)

        # P04: Usage on gateway failure must NOT be invented as 0 cost!
        assert case.usage_id is not None
        async with session_factory() as session:
            usage_row = await session.get(ModelUsage, case.usage_id)
            assert usage_row is not None
            assert usage_row.estimated_cost_minor is None
            assert usage_row.unknown_price_reason is not None


async def test_adversarial_p05_independent_execution_status_vs_evaluation_score(
    tmp_path: Path, session_factory: Any
) -> None:
    from forge.persistence.models import AgentExecution

    # Plan with wrong components: Agent succeeded in finishing, but evaluation score fails
    bad_plan = PlanOutput.model_validate(
        {
            "summary": "Completed plan with wrong component",
            "assumptions": [],
            "affected_components": ["wrong/component"],
            "steps": ["Step 1"],
            "required_checks": ["pytest"],
            "risks": ["regression"],
            "security_considerations": [],
            "dependency_changes": [],
        }
    )
    review_data = load_expected_output(EXPECTED_ROOT / "reviewer-missing-authorization.json")
    review = ReviewOutput.model_validate(review_data)
    dev_data = load_expected_output(EXPECTED_ROOT / "developer-basic-change.json")
    dev = DeveloperOutput.model_validate(dev_data)

    gateway = FakeAgentGateway(
        {
            AgentRole.PLANNER: [FakeAgentStep.success(bad_plan)],
            AgentRole.REVIEWER: [FakeAgentStep.success(review)],
            AgentRole.DEVELOPER: [FakeAgentStep.success(dev)],
        }
    )
    gateway.evaluation_provider = "test"  # type: ignore[attr-defined]
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=f"adv-p05-status-{uuid4()}",
    )

    planner_case = next(c for c in result.cases if c.role == AgentRole.PLANNER)
    # Evaluation score failed
    assert planner_case.passed is False
    assert planner_case.status == "failed"

    # P05: AgentExecution status must remain SUCCEEDED because the agent finished successfully!
    async with session_factory() as session:
        exec_row = await session.get(AgentExecution, planner_case.execution_id)
        assert exec_row is not None
        assert exec_row.status == "SUCCEEDED"


async def test_adversarial_p01_replay_rejects_changed_role_metric_version(
    tmp_path: Path, session_factory: Any
) -> None:
    gateway = _build_passing_fake_gateway()
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    idempotency_key = f"adv-p01-metric-{uuid4()}"

    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    await service.run_suite(
        suite_name="deterministic",
        idempotency_key=idempotency_key,
    )

    # Replay with tampered role-specific metric_version
    tampered_case = EvaluationCaseContract(
        fixture_version="eval-fixture-v1",
        metric_version="tampered-metric-v2",
        case_key="planner/basic-change",
        task="TAMPERED",
        role=AgentRole.PLANNER,
        base_directory=FIXTURES_ROOT / "planner" / "basic-change",
        expected_components=("apps/web",),
        expected_checks=("pytest",),
        expected_risks=("regression",),
    )
    rev_case = next(
        c for c in load_evaluation_cases(FIXTURES_ROOT).values() if c.role == AgentRole.REVIEWER
    )
    dev_case = next(
        c for c in load_evaluation_cases(FIXTURES_ROOT).values() if c.role == AgentRole.DEVELOPER
    )

    with pytest.raises(EvaluationConflict, match="case version mismatch"):
        await service.run_suite(
            suite_name="deterministic",
            idempotency_key=idempotency_key,
            cases=[tampered_case, rev_case, dev_case],
        )


async def test_adversarial_p01_replay_rejects_missing_stored_fixture_identity(
    tmp_path: Path, session_factory: Any
) -> None:
    import json

    gateway = _build_passing_fake_gateway()
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    idempotency_key = f"adv-p01-legacy-reject-{uuid4()}"

    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    res = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=idempotency_key,
    )
    assert res.status == "passed"

    planner_case = next(c for c in res.cases if c.role == AgentRole.PLANNER)
    assert planner_case.input_digest is not None

    # Tamper with stored input artifact by stripping fixture_identity (legacy shape)
    orig_open = store.open_bytes

    async def tampered_open_bytes(digest: str, *, max_bytes: int | None = None) -> bytes:
        content = await orig_open(digest, max_bytes=max_bytes)
        if digest == planner_case.input_digest:
            doc = json.loads(content.decode("utf-8"))
            doc.pop("fixture_identity", None)
            return json.dumps(doc, sort_keys=True).encode("utf-8")
        return content

    store.open_bytes = tampered_open_bytes  # type: ignore[method-assign]

    # Settled replay must reject the missing fixture identity without legacy compatibility
    with pytest.raises(EvaluationConflict, match="fixture content identity mismatch"):
        await service.run_suite(
            suite_name="deterministic",
            idempotency_key=idempotency_key,
        )


async def test_adversarial_p04_substituted_gateway_identity_rejected(
    tmp_path: Path, session_factory: Any
) -> None:
    class SubstitutedIdentityGateway:
        async def execute(self, request: AgentRequest) -> AgentResult:
            from forge.observability.usage import UsageRecord

            plan_data = load_expected_output(EXPECTED_ROOT / "planner-basic-change.json")
            plan = PlanOutput.model_validate(plan_data)
            usage = UsageRecord(
                provider="impostor-provider",
                model="impostor-model",
                prompt_version=request.instruction_version,
                input_tokens=1,
                output_tokens=1,
                duration_ms=100,
                tool_call_count=0,
                pricing_version="fake-v1",
                currency="USD",
                estimated_cost_minor=1,
            )
            return AgentResult(
                execution_id=uuid4(),  # Substituted execution ID
                role=AgentRole.PLANNER,
                finish_status=AgentFinishStatus.SUCCEEDED,
                output=plan,
                provider="impostor-provider",
                model="impostor-model",
                instruction_digest=request.instruction_digest,
                usage=usage,
                tool_call_count=0,
                duration_ms=100,
            )

    store = FilesystemArtifactStore(tmp_path / "artifacts")
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=SubstitutedIdentityGateway(),
    )

    result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=f"adv-p04-subst-{uuid4()}",
    )
    # Substituted identity must fail the suite and record the error reason
    assert result.status == "failed"
    for case in result.cases:
        assert case.passed is False
        assert case.error == "result_identity_mismatch"


async def test_evaluation_service_promoted_baseline_flag_requires_persisted_baseline(
    tmp_path: Path, session_factory: Any
) -> None:
    gateway = _build_passing_fake_gateway()
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    with pytest.raises(EvaluationConflict, match="no persisted baseline found"):
        await service.run_suite(
            suite_name="deterministic",
            idempotency_key=f"no-base-{uuid4()}",
            promoted_baseline=True,
        )


async def test_evaluation_service_live_suite_nonblocking_without_persisted_baseline(
    tmp_path: Path, session_factory: Any
) -> None:
    class MockCredResolver:
        async def resolve(self, ref: str) -> str:
            return "mock-key"

    plan_data = load_expected_output(EXPECTED_ROOT / "planner-basic-change.json")
    review_data = load_expected_output(EXPECTED_ROOT / "reviewer-missing-authorization.json")
    dev_data = load_expected_output(EXPECTED_ROOT / "developer-basic-change.json")
    gateway = FakeAgentGateway(
        {
            AgentRole.PLANNER: [
                FakeAgentStep.success(PlanOutput.model_validate(plan_data), cost_minor=100)
            ],
            AgentRole.REVIEWER: [
                FakeAgentStep.success(ReviewOutput.model_validate(review_data), cost_minor=100)
            ],
            AgentRole.DEVELOPER: [
                FakeAgentStep.success(DeveloperOutput.model_validate(dev_data), cost_minor=100)
            ],
        }
    )
    gateway.evaluation_provider = "test"  # type: ignore[attr-defined]

    store = FilesystemArtifactStore(tmp_path / "artifacts")
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
        credential_resolver=MockCredResolver(),  # type: ignore[arg-type]
    )

    # A fake live gateway cannot manufacture developer tool evidence. Regression
    # thresholds are nonblocking, but the missing controlled evidence fails the case.
    result = await service.run_suite(
        suite_name="live",
        provider_reference="secret://forge/google_ai_studio_api_key",
        live_model="mock-model",
        idempotency_key=f"live-nonblock-{uuid4()}",
        ceilings={"estimated_cost_minor": 10.0},
    )
    assert len(result.regressions) > 0
    assert result.status == "failed"


async def test_evaluation_service_live_suite_blocking_with_persisted_baseline(
    tmp_path: Path, session_factory: Any
) -> None:
    class MockCredResolver:
        async def resolve(self, ref: str) -> str:
            return "mock-key"

    passing_gw = _build_passing_fake_gateway()
    passing_gw.evaluation_provider = "test"  # type: ignore[attr-defined]
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=passing_gw,
        credential_resolver=MockCredResolver(),  # type: ignore[arg-type]
    )

    # 1. Run a passing deterministic suite
    initial_res = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=f"initial-for-promo-{uuid4()}",
    )
    assert initial_res.status == "passed"

    # 2. Promote it as baseline 'live' with ceiling on cost
    baseline = await service.promote_baseline(
        suite_id=initial_res.suite_id,
        name="live",
        ceilings={"estimated_cost_minor": 50.0},
    )
    assert baseline.name == "live"

    # 3. Run live suite with regressing cost
    plan_data = load_expected_output(EXPECTED_ROOT / "planner-basic-change.json")
    review_data = load_expected_output(EXPECTED_ROOT / "reviewer-missing-authorization.json")
    dev_data = load_expected_output(EXPECTED_ROOT / "developer-basic-change.json")
    regressing_gw = FakeAgentGateway(
        {
            AgentRole.PLANNER: [
                FakeAgentStep.success(PlanOutput.model_validate(plan_data), cost_minor=100)
            ],
            AgentRole.REVIEWER: [
                FakeAgentStep.success(ReviewOutput.model_validate(review_data), cost_minor=100)
            ],
            AgentRole.DEVELOPER: [
                FakeAgentStep.success(DeveloperOutput.model_validate(dev_data), cost_minor=100)
            ],
        }
    )
    regressing_gw.evaluation_provider = "test"  # type: ignore[attr-defined]
    service._default_gateway = regressing_gw

    # With promoted_baseline=True, live suite regression IS blocking -> status == 'failed'
    live_res = await service.run_suite(
        suite_name="live",
        provider_reference="secret://forge/google_ai_studio_api_key",
        live_model="mock-model",
        idempotency_key=f"live-block-{uuid4()}",
        promoted_baseline=True,
    )
    assert live_res.status == "failed"
    assert "estimated_cost_minor" in live_res.regressions


async def test_atomic_persistence_rolls_back_priced_usage_on_settlement_failure(
    tmp_path: Path, session_factory: Any
) -> None:
    from unittest.mock import patch

    from forge.persistence.repositories.evaluations import EvaluationRepository

    gateway = _build_passing_fake_gateway()
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    with (
        patch.object(
            EvaluationRepository,
            "record_case",
            side_effect=RuntimeError("simulated settlement failure"),
        ),
        pytest.raises(RuntimeError, match="simulated settlement failure"),
    ):
        await service.run_suite(
            suite_name="deterministic",
            idempotency_key=f"atomic-rollback-{uuid4()}",
        )

    # Verify no ModelUsage was orphaned in PostgreSQL
    async with session_factory() as session:
        usage_count = (await session.scalars(select(ModelUsage))).all()
        assert len(usage_count) == 0
