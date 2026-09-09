"""End-to-end execution tests for the evaluation harness and service."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from forge.agents.fake_gateway import FakeAgentGateway, FakeAgentStep
from forge.application.services.evaluations import EvaluationService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.actor import AgentRole
from forge.domain.agent import DeveloperOutput, ReviewOutput
from forge.domain.plan import PlanOutput
from forge.evaluations.loader import load_expected_output
from forge.persistence.models import AgentExecution, Artifact, ModelUsage, Project, Run, Step
from forge.persistence.models.evaluation import EvaluationCase, EvaluationSuite
from forge.persistence.repositories.evaluations import EvaluationRepository
from sqlalchemy import select

pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)
pytestmark = pytest.mark.asyncio

FIXTURES_ROOT = Path(__file__).parent / "fixtures"
EXPECTED_ROOT = Path(__file__).parent / "expected"


def _build_passing_fake_gateway() -> FakeAgentGateway:
    plan_data = load_expected_output(EXPECTED_ROOT / "planner-basic-change.json")
    review_data = load_expected_output(EXPECTED_ROOT / "reviewer-missing-authorization.json")
    dev_data = load_expected_output(EXPECTED_ROOT / "developer-basic-change.json")
    plan = PlanOutput.model_validate(plan_data)
    review = ReviewOutput.model_validate(review_data)
    dev = DeveloperOutput.model_validate(dev_data)
    return FakeAgentGateway(
        {
            AgentRole.PLANNER: [FakeAgentStep.success(plan, duration_ms=250, cost_minor=5)],
            AgentRole.REVIEWER: [FakeAgentStep.success(review, duration_ms=300, cost_minor=5)],
            AgentRole.DEVELOPER: [FakeAgentStep.success(dev, duration_ms=400, cost_minor=5)],
        }
    )


async def test_end_to_end_deterministic_evaluation_runs_and_records_lineage(
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

    suite_key = f"e2e-eval-{uuid4()}"
    result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=suite_key,
    )

    assert result.status == "passed"
    assert len(result.cases) == 3
    assert all(c.passed for c in result.cases)

    planner_res = next(c for c in result.cases if c.role == AgentRole.PLANNER)
    reviewer_res = next(c for c in result.cases if c.role == AgentRole.REVIEWER)
    dev_res = next(c for c in result.cases if c.role == AgentRole.DEVELOPER)

    # Verify scores
    assert planner_res.metrics["component_recall"] == 1.0
    assert planner_res.metrics["check_recall"] == 1.0
    assert planner_res.metrics["risk_recall"] == 1.0
    assert planner_res.metrics["policy_compliance"] == 1.0
    assert planner_res.metrics["schema_validity"] == 1.0

    assert reviewer_res.metrics["defect_recall"] == 1.0
    assert reviewer_res.metrics["blocker_recall"] == 1.0
    assert reviewer_res.metrics["evidence_quality"] == 1.0
    assert reviewer_res.metrics["policy_compliance"] == 1.0

    assert dev_res.metrics["required_test_pass"] == 1.0
    assert dev_res.metrics["diff_scope_precision"] == 1.0
    assert dev_res.metrics["named_check_success"] == 1.0
    assert dev_res.metrics["task_assertion_pass"] == 1.0
    assert dev_res.metrics["policy_compliance"] == 1.0
    assert dev_res.metrics["schema_validity"] == 1.0

    # Verify durable persistence in PostgreSQL
    async with session_factory() as session:
        suite_row = await session.scalar(
            select(EvaluationSuite).where(EvaluationSuite.id == result.suite_id)
        )
        assert suite_row is not None
        assert suite_row.status == "passed"
        assert suite_row.idempotency_key == suite_key

        case_rows = list(
            (
                await session.scalars(
                    select(EvaluationCase).where(EvaluationCase.suite_id == result.suite_id)
                )
            ).all()
        )
        assert len(case_rows) == 3
        for crow in case_rows:
            assert crow.status == "passed"
            assert crow.model_usage_id is not None
            assert crow.input_artifact_digest is not None
            assert crow.output_artifact_digest is not None

            # Verify ModelUsage binding
            usage_row = await session.scalar(
                select(ModelUsage).where(ModelUsage.id == crow.model_usage_id)
            )
            assert usage_row is not None

            # Verify AgentExecution binding
            exec_row = await session.scalar(
                select(AgentExecution).where(AgentExecution.id == usage_row.agent_execution_id)
            )
            assert exec_row is not None
            assert exec_row.status == "SUCCEEDED"
            assert exec_row.output_artifact_id is not None

            run_row = await session.get(Run, usage_row.run_id)
            step_row = await session.get(Step, exec_row.step_id)
            assert run_row is not None
            assert step_row is not None
            assert run_row.state == "COMPLETED"
            assert step_row.status == "SUCCEEDED"
            assert step_row.completed_at is not None

            # Verify artifact store has persisted the content-addressed artifacts
            in_art = await session.scalar(
                select(Artifact).where(Artifact.digest == crow.input_artifact_digest)
            )
            out_art = await session.scalar(
                select(Artifact).where(Artifact.digest == crow.output_artifact_digest)
            )
            assert in_art is not None
            assert out_art is not None
            assert await store.verify(crow.input_artifact_digest)
            assert await store.verify(crow.output_artifact_digest)


async def test_changed_expected_metric_fails_evaluation(
    tmp_path: Path, session_factory: Any
) -> None:
    """Prove that changing an expected metric causes the test to fail."""
    # Build a gateway where planner produces an output with wrong component
    plan_data = dict(load_expected_output(EXPECTED_ROOT / "planner-basic-change.json"))
    plan_data["affected_components"] = ["wrong/component"]
    bad_plan = PlanOutput.model_validate(plan_data)

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
        idempotency_key=f"e2e-metric-fail-{uuid4()}",
    )

    # Suite and case must fail
    assert result.status == "failed"
    planner_case = next(c for c in result.cases if c.role == AgentRole.PLANNER)
    assert planner_case.passed is False
    assert planner_case.status == "failed"
    # Expected apps/web was not in bad_plan's affected_components
    assert planner_case.metrics["component_recall"] == 0.0

    async with session_factory() as session:
        repo = EvaluationRepository(session)
        suite = await repo.get_suite(result.suite_id)
        assert suite is not None
        assert suite.status == "failed"


async def test_new_suite_uses_fresh_project_after_prior_fixture_cleanup(
    tmp_path: Path, session_factory: Any
) -> None:
    """A stable case key must not reuse a Project pointing at a deleted fixture."""
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    first = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=_build_passing_fake_gateway(),
    )
    second = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=_build_passing_fake_gateway(),
    )

    first_result = await first.run_suite(
        suite_name="deterministic", idempotency_key=f"fresh-project-a-{uuid4()}"
    )
    second_result = await second.run_suite(
        suite_name="deterministic", idempotency_key=f"fresh-project-b-{uuid4()}"
    )
    assert first_result.status == second_result.status == "passed"

    async with session_factory() as session:
        first_run = await session.get(Run, first_result.cases[0].run_id)
        second_run = await session.get(Run, second_result.cases[0].run_id)
        assert first_run is not None and second_run is not None
        assert first_run.project_id != second_run.project_id
        first_project = await session.get(Project, first_run.project_id)
        second_project = await session.get(Project, second_run.project_id)
        assert first_project is not None and second_project is not None
        assert first_project.github_repository != second_project.github_repository
        assert not Path(first_project.canonical_path).exists()
        assert not Path(second_project.canonical_path).exists()


async def test_failed_and_cancelled_gateway_cases_settle_durable_lifecycle(
    tmp_path: Path, session_factory: Any
) -> None:
    """Terminal gateway outcomes cannot leave fixture runs or steps running."""
    gateway = FakeAgentGateway(
        {
            AgentRole.PLANNER: [FakeAgentStep.failed()],
            AgentRole.REVIEWER: [FakeAgentStep.cancelled()],
            AgentRole.DEVELOPER: [FakeAgentStep.failed()],
        }
    )
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )
    result = await service.run_suite(
        suite_name="deterministic", idempotency_key=f"terminal-fixture-{uuid4()}"
    )
    assert result.status == "failed"

    async with session_factory() as session:
        for case in result.cases:
            run = await session.get(Run, case.run_id)
            execution = await session.get(AgentExecution, case.execution_id)
            assert run is not None and execution is not None
            step = await session.get(Step, execution.step_id)
            assert step is not None
            assert run.state in {"FAILED", "CANCELLED"}
            assert step.status in {"FAILED", "CANCELLED"}
            assert step.completed_at is not None
            assert execution.status in {"FAILED", "CANCELLED"}


async def test_regression_floor_failure_marks_suite_failed(
    tmp_path: Path, session_factory: Any
) -> None:
    """Prove that an unmet metric floor fails the evaluation suite."""
    gateway = _build_passing_fake_gateway()
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    service = EvaluationService(
        session_factory=session_factory,
        artifact_store=store,
        fixtures_dir=FIXTURES_ROOT,
        expected_dir=EXPECTED_ROOT,
        default_gateway=gateway,
    )

    # Require impossible 100% token cache hit floor
    result = await service.run_suite(
        suite_name="deterministic",
        idempotency_key=f"e2e-floor-fail-{uuid4()}",
        floors={"cached_input_tokens": 9999.0},
    )

    assert result.status == "failed"
    assert len(result.regressions) > 0
    assert "cached_input_tokens" in result.regressions
