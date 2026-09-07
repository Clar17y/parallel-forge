"""Real PostgreSQL coverage for measured planner-attempt usage persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from forge.agents.errors import AgentBudgetExceeded, AgentOutputInvalid, AgentPromptDrift
from forge.agents.prompt_loader import PromptLoader
from forge.application.adapters.git import canonical_path_key
from forge.application.services.planning import (
    PlanningError,
    PlanningRecoveryRequired,
    PlanningService,
    canonical_json_bytes,
)
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus, AgentRequest, AgentResult
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.plan import PlanOutput
from forge.domain.policy import AgentModelPolicy, CommandSpec, ProjectPolicy, StepKind
from forge.domain.run import RunSnapshot, RunState
from forge.observability.usage import UsageRecord
from forge.persistence.models import (
    AgentExecution,
    Artifact,
    ArtifactLineage,
    ModelUsage,
    Project,
    ProjectPolicyVersion,
    Run,
    Task,
)
from forge.persistence.repositories.tasks import compute_task_digest, derive_normalized_text
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.tools.repository import RepositoryReader
from sqlalchemy import select

pytestmark = pytest.mark.integration
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@dataclass(frozen=True)
class _Case:
    service: PlanningService
    command: CommandEnvelope
    gateway: _Gateway
    artifact_store: FilesystemArtifactStore
    run_id: UUID


class _Gateway:
    def __init__(
        self,
        *,
        fail_invalid: bool,
        fail_budget: bool = False,
        fail_prompt_drift: bool = False,
        fail_unsafe_usage: bool = False,
    ) -> None:
        self.fail_invalid = fail_invalid
        self.fail_budget = fail_budget
        self.fail_prompt_drift = fail_prompt_drift
        self.fail_unsafe_usage = fail_unsafe_usage
        self.requests: list[AgentRequest] = []

    async def execute(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        attempts = _attempts(request)
        aggregate = _aggregate(request, attempts)
        if self.fail_prompt_drift:
            raise AgentPromptDrift(usage=attempts[0], usage_attempts=(attempts[0],))
        if self.fail_unsafe_usage:
            error = AgentBudgetExceeded(usage=_aggregate(request, ()))
            error.usage = UsageRecord(
                provider=request.provider,
                model=request.model,
                prompt_version=request.instruction_version,
                input_tokens=24,
                provider_request_id="ghp_" + "A" * 36,
                pricing_version="test-v1",
                estimated_cost_minor=3,
                currency="USD",
            )
            raise error
        if self.fail_budget:
            unknown_attempts = tuple(
                UsageRecord(
                    provider=item.provider,
                    model=item.model,
                    prompt_version=item.prompt_version,
                    input_tokens=item.input_tokens,
                    output_tokens=item.output_tokens,
                    cached_input_tokens=item.cached_input_tokens,
                    duration_ms=item.duration_ms,
                    tool_call_count=item.tool_call_count,
                    provider_request_id=item.provider_request_id,
                    pricing_version="test-v1",
                    currency="USD",
                    unknown_price_reason="pricing_unavailable",
                )
                for item in attempts
            )
            raise AgentBudgetExceeded(
                usage=UsageRecord(
                    provider=aggregate.provider,
                    model=aggregate.model,
                    prompt_version=aggregate.prompt_version,
                    input_tokens=aggregate.input_tokens,
                    output_tokens=aggregate.output_tokens,
                    cached_input_tokens=aggregate.cached_input_tokens,
                    duration_ms=aggregate.duration_ms,
                    tool_call_count=aggregate.tool_call_count,
                    pricing_version="test-v1",
                    currency="USD",
                    unknown_price_reason="pricing_unavailable",
                ),
                usage_attempts=unknown_attempts,
            )
        if self.fail_invalid:
            raise AgentOutputInvalid(usage=aggregate, usage_attempts=attempts)
        return AgentResult(
            execution_id=request.execution_id,
            role=AgentRole.PLANNER,
            finish_status=AgentFinishStatus.SUCCEEDED,
            output=_plan(),
            parent_execution_id=None,
            provider=request.provider,
            model=request.model,
            instruction_digest=request.instruction_digest,
            usage=aggregate,
            usage_attempts=attempts,
            tool_call_count=aggregate.tool_call_count,
            duration_ms=aggregate.duration_ms,
        )


class _FailFinalCommitUnitOfWork(PostgresUnitOfWork):
    """Inject one database-boundary failure after durable provider admission."""

    def __init__(self, session_factory) -> None:
        super().__init__(session_factory)
        self._commit_count = 0

    async def commit(self) -> None:
        self._commit_count += 1
        if self._commit_count == 3:
            raise RuntimeError("injected final commit failure")
        await super().commit()


def _attempts(request: AgentRequest) -> tuple[UsageRecord, UsageRecord]:
    values = ((11, 7, 3, 101, 2, "attempt-one"), (13, 5, 2, 211, 4, "attempt-two"))
    return tuple(
        UsageRecord(
            provider=request.provider,
            model=request.model,
            prompt_version=request.instruction_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            duration_ms=duration_ms,
            tool_call_count=tool_calls,
            provider_request_id=request_id,
            pricing_version="test-v1",
            estimated_cost_minor=cost,
            currency="USD",
        )
        for cost, (
            input_tokens,
            output_tokens,
            cached_tokens,
            duration_ms,
            tool_calls,
            request_id,
        ) in enumerate(values, start=1)
    )  # type: ignore[return-value]


def _aggregate(request: AgentRequest, attempts: tuple[UsageRecord, UsageRecord]) -> UsageRecord:
    return UsageRecord(
        provider=request.provider,
        model=request.model,
        prompt_version=request.instruction_version,
        input_tokens=sum(item.input_tokens for item in attempts),
        output_tokens=sum(item.output_tokens for item in attempts),
        cached_input_tokens=sum(item.cached_input_tokens for item in attempts),
        duration_ms=sum(item.duration_ms for item in attempts),
        tool_call_count=sum(item.tool_call_count for item in attempts),
        provider_request_id="aggregate",
        pricing_version="test-v1",
        estimated_cost_minor=3,
        currency="USD",
    )


def _plan() -> PlanOutput:
    return PlanOutput(
        summary="Repair the invalid first response.",
        assumptions=(),
        affected_components=("planner",),
        steps=("Persist measured attempts.",),
        required_checks=("unit",),
        risks=("Transaction rollback",),
        security_considerations=(),
        dependency_changes=(),
    )


async def _build_case(
    tmp_path: Path,
    session_factory,
    *,
    fail_invalid: bool,
    fail_budget: bool = False,
    fail_prompt_drift: bool = False,
    fail_unsafe_usage: bool = False,
) -> _Case:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("fixture repository\n", encoding="utf-8")
    prompts = tmp_path / "prompts" / "planner"
    prompts.mkdir(parents=True)
    (prompts / "instructions.md").write_text(
        "<!-- forge-instruction-version: planning-test-v1 -->\nPlan carefully.\n",
        encoding="utf-8",
    )

    project_id, task_id, run_id = uuid4(), uuid4(), uuid4()
    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path=str(repository.resolve()),
        github_repository=f"example/project-{project_id}",
        default_branch="main",
        planner_model=AgentModelPolicy(provider="test", model="planner-test"),
        commands=(
            CommandSpec(
                kind=StepKind.TEST,
                name="unit",
                argv=("python", "-m", "pytest"),
                timeout_seconds=60,
            ),
        ),
    )
    document = policy.model_dump(mode="json")
    task_title, task_body = "Persist planner usage", "Keep both measured attempts."
    async with session_factory() as session, session.begin():
        project = Project(
            id=project_id,
            name="Planning usage fixture",
            canonical_path=policy.repository_path,
            canonical_path_key=canonical_path_key(policy.repository_path),
            github_repository=policy.github_repository,
            default_branch=policy.default_branch,
        )
        session.add(project)
        await session.flush()
        session.add(
            ProjectPolicyVersion(
                project_id=project_id,
                version=1,
                policy_digest=hashlib.sha256(canonical_json_bytes(document)).hexdigest(),
                document_schema_version=1,
                document=document,
            )
        )
        session.add(
            Task(
                id=task_id,
                project_id=project_id,
                title=task_title,
                body=task_body,
                normalized_text=derive_normalized_text(task_title, task_body),
                task_digest=compute_task_digest(
                    title=task_title,
                    body=task_body,
                    source_url=None,
                    source_updated_at=None,
                    external_source=None,
                    external_id=None,
                ),
            )
        )
        await session.flush()
        project.current_policy_version = 1

    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(
            RunSnapshot(
                id=run_id,
                project_id=project_id,
                task_id=task_id,
                policy_version=1,
                base_ref="refs/heads/main",
                base_sha="a" * 40,
            )
        )
        await work.commit()

    gateway = _Gateway(
        fail_invalid=fail_invalid,
        fail_budget=fail_budget,
        fail_prompt_drift=fail_prompt_drift,
        fail_unsafe_usage=fail_unsafe_usage,
    )
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    service = PlanningService(
        gateway,
        artifact_store,
        PromptLoader(tmp_path / "prompts"),
        lambda current_policy: RepositoryReader(
            current_policy.repository_path,
            secret_paths=current_policy.effective_secret_paths,
            force_python_search=True,
        ),
    )
    now = datetime.now(UTC)
    command = CommandEnvelope(
        id=uuid4(),
        run_id=run_id,
        command_type="start_planning",
        idempotency_key=f"planning:{run_id}",
        payload={},
        status=CommandStatus.LEASED,
        expected_run_version=0,
        actor_id=None,
        payload_schema_version=1,
        attempt=1,
        available_at=now,
        lease_owner="test-worker",
        lease_expires_at=now + timedelta(minutes=1),
    )
    return _Case(service, command, gateway, artifact_store, run_id)


async def _settled_rows(session_factory, case: _Case):
    async with session_factory() as session:
        run = await session.get(Run, case.run_id)
        execution = (
            await session.execute(
                select(AgentExecution).where(AgentExecution.run_id == case.run_id)
            )
        ).scalar_one()
        usage = (
            (await session.execute(select(ModelUsage).where(ModelUsage.run_id == case.run_id)))
            .scalars()
            .all()
        )
        attempt_row = (
            await session.execute(
                select(Artifact, ArtifactLineage)
                .join(ArtifactLineage, ArtifactLineage.artifact_id == Artifact.id)
                .where(
                    ArtifactLineage.run_id == case.run_id,
                    ArtifactLineage.producer_kind == "agent_usage_attempts",
                )
            )
        ).one_or_none()
        return run, execution, usage, attempt_row


async def _assert_attempt_evidence(case: _Case, attempt_row) -> None:
    artifact, lineage = attempt_row
    assert lineage.producer_id == case.gateway.requests[0].execution_id
    payload = json.loads(await case.artifact_store.open_bytes(artifact.digest))
    assert payload == {
        "attempts": [
            {
                "agent_execution_id": None,
                "cached_input_tokens": 3,
                "currency": "USD",
                "duration_ms": 101,
                "estimated_cost_minor": 1,
                "input_tokens": 11,
                "model": "planner-test",
                "output_tokens": 7,
                "pricing_version": "test-v1",
                "prompt_version": "planning-test-v1",
                "provider": "test",
                "provider_request_id": "attempt-one",
                "run_id": None,
                "tool_call_count": 2,
                "unknown_price_reason": None,
            },
            {
                "agent_execution_id": None,
                "cached_input_tokens": 2,
                "currency": "USD",
                "duration_ms": 211,
                "estimated_cost_minor": 2,
                "input_tokens": 13,
                "model": "planner-test",
                "output_tokens": 5,
                "pricing_version": "test-v1",
                "prompt_version": "planning-test-v1",
                "provider": "test",
                "provider_request_id": "attempt-two",
                "run_id": None,
                "tool_call_count": 4,
                "unknown_price_reason": None,
            },
        ],
        "schema_version": 1,
    }


@pytest.mark.asyncio
async def test_both_invalid_attempts_persist_aggregate_and_canonical_evidence(
    tmp_path: Path, session_factory
) -> None:
    case = await _build_case(tmp_path, session_factory, fail_invalid=True)

    async with PostgresUnitOfWork(session_factory) as work:
        outcome = await case.service.execute(case.command, work)

    run, execution, usages, attempt_row = await _settled_rows(session_factory, case)
    assert outcome.finish_status is AgentFinishStatus.INVALID_OUTPUT
    assert run.state == RunState.AWAITING_HUMAN_INTERVENTION.value
    assert execution.status == "FAILED"
    assert len(usages) == 1
    usage = usages[0]
    assert (
        usage.input_tokens,
        usage.output_tokens,
        usage.cached_input_tokens,
        usage.duration_ms,
        usage.tool_call_count,
    ) == (24, 12, 5, 312, 6)
    assert usage.provider_request_id == "aggregate"
    assert attempt_row is not None
    await _assert_attempt_evidence(case, attempt_row)


@pytest.mark.asyncio
async def test_budget_failure_persists_measured_unknown_price_attempts_once(
    tmp_path: Path, session_factory
) -> None:
    case = await _build_case(tmp_path, session_factory, fail_invalid=False, fail_budget=True)

    async with PostgresUnitOfWork(session_factory) as work:
        outcome = await case.service.execute(case.command, work)

    run, execution, usages, attempt_row = await _settled_rows(session_factory, case)
    assert outcome.finish_status is AgentFinishStatus.BUDGET_EXCEEDED
    assert run.state == RunState.AWAITING_HUMAN_INTERVENTION.value
    assert execution.status == "FAILED"
    assert len(usages) == 1
    usage = usages[0]
    assert (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens) == (24, 12, 5)
    assert usage.estimated_cost_minor is None
    assert usage.unknown_price_reason == "pricing_unavailable"
    assert attempt_row is not None
    payload = json.loads(await case.artifact_store.open_bytes(attempt_row[0].digest))
    assert [item["provider_request_id"] for item in payload["attempts"]] == [
        "attempt-one",
        "attempt-two",
    ]
    assert [item["unknown_price_reason"] for item in payload["attempts"]] == [
        "pricing_unavailable",
        "pricing_unavailable",
    ]


@pytest.mark.asyncio
async def test_prompt_drift_after_measured_attempt_persists_usage_and_attempts(
    tmp_path: Path, session_factory
) -> None:
    case = await _build_case(tmp_path, session_factory, fail_invalid=False, fail_prompt_drift=True)

    async with PostgresUnitOfWork(session_factory) as work:
        outcome = await case.service.execute(case.command, work)

    _, execution, usages, attempt_row = await _settled_rows(session_factory, case)
    assert outcome.finish_status is AgentFinishStatus.FAILED
    assert execution.status == "FAILED"
    assert len(case.gateway.requests) == 1
    assert len(usages) == 1
    assert usages[0].input_tokens == 11
    assert usages[0].provider_request_id == "attempt-one"
    assert outcome.failure_artifact is not None
    failure = json.loads(await case.artifact_store.open_bytes(outcome.failure_artifact.digest))
    assert failure["reason"] == "prompt_drift"
    assert attempt_row is not None
    payload = json.loads(await case.artifact_store.open_bytes(attempt_row[0].digest))
    assert [item["provider_request_id"] for item in payload["attempts"]] == ["attempt-one"]


@pytest.mark.asyncio
async def test_empty_attempt_credential_usage_is_replaced_before_pg_settlement(
    tmp_path: Path, session_factory
) -> None:
    case = await _build_case(tmp_path, session_factory, fail_invalid=False, fail_unsafe_usage=True)

    async with PostgresUnitOfWork(session_factory) as work:
        await case.service.execute(case.command, work)

    _, _, usages, attempt_row = await _settled_rows(session_factory, case)
    assert len(usages) == 1
    assert usages[0].provider_request_id is None
    assert usages[0].input_tokens == 0
    assert usages[0].estimated_cost_minor is None
    assert usages[0].unknown_price_reason == "gateway_usage_unavailable"
    assert attempt_row is None


@pytest.mark.asyncio
async def test_repaired_success_retains_both_attempts_with_plan_artifacts(
    tmp_path: Path, session_factory
) -> None:
    case = await _build_case(tmp_path, session_factory, fail_invalid=False)

    async with PostgresUnitOfWork(session_factory) as work:
        outcome = await case.service.execute(case.command, work)

    run, execution, usages, attempt_row = await _settled_rows(session_factory, case)
    assert outcome.finish_status is AgentFinishStatus.SUCCEEDED
    assert run.state == RunState.AWAITING_PLAN_APPROVAL.value
    assert execution.status == "SUCCEEDED"
    assert len(usages) == 1
    assert (
        usages[0].input_tokens,
        usages[0].output_tokens,
        usages[0].cached_input_tokens,
        usages[0].duration_ms,
        usages[0].tool_call_count,
    ) == (24, 12, 5, 312, 6)
    assert attempt_row is not None
    await _assert_attempt_evidence(case, attempt_row)
    async with session_factory() as session:
        kinds = set(
            (
                await session.execute(
                    select(ArtifactLineage.producer_kind).where(
                        ArtifactLineage.run_id == case.run_id
                    )
                )
            ).scalars()
        )
    assert {"agent_usage_attempts", "implementation_plan", "plan_approval_evidence"} <= kinds


@pytest.mark.asyncio
async def test_final_commit_failure_rolls_back_settlement_and_retry_does_not_replay_provider(
    tmp_path: Path, session_factory
) -> None:
    case = await _build_case(tmp_path, session_factory, fail_invalid=True)

    with pytest.raises(PlanningError, match="planning execution failed"):
        async with _FailFinalCommitUnitOfWork(session_factory) as work:
            await case.service.execute(case.command, work)

    run, execution, usages, attempt_row = await _settled_rows(session_factory, case)
    assert run.state == RunState.PLANNING.value
    assert execution.status == "RUNNING"
    assert execution.output_artifact_id is None
    assert usages == []
    assert attempt_row is None
    assert len(case.gateway.requests) == 1
    async with session_factory() as session:
        kinds = set(
            (
                await session.execute(
                    select(ArtifactLineage.producer_kind).where(
                        ArtifactLineage.run_id == case.run_id
                    )
                )
            ).scalars()
        )
    assert kinds == {"planning_context"}

    with pytest.raises(PlanningRecoveryRequired, match="planning recovery is required"):
        async with PostgresUnitOfWork(session_factory) as retry_work:
            await case.service.execute(case.command, retry_work)
    assert len(case.gateway.requests) == 1
