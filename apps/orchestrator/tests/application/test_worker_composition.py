"""Unit and contract tests for worker dependency composition and gateway binding."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

import pytest
from forge.agents.adk_gateway import BoundAdkTools
from forge.agents.errors import AgentGatewayError
from forge.agents.prompt_loader import PromptLoader
from forge.application.adapters.git import LocalGitRepositoryInspector
from forge.application.handlers.planning import PlanningHandler
from forge.application.ports.executions import ExecutionAdmission
from forge.application.ports.projects import ProjectPolicyRecord
from forge.application.ports.provider_credentials import ProviderCredentialError
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentBudget,
    AgentFinishStatus,
    AgentRequest,
    AgentResult,
    PlannerInput,
    PolicySummary,
    UntrustedContent,
    UntrustedSourceKind,
)
from forge.domain.plan import PlanOutput
from forge.domain.policy import AgentModelPolicy, ProjectPolicy
from forge.domain.run import RunSnapshot, RunState
from forge.domain.tool import ToolName
from forge.observability.redaction import Redactor
from forge.observability.usage import UsageRecord
from forge.persistence.models.execution import AgentExecution
from forge.settings import Settings
from forge.worker.composition import (
    BoundPlanningGateway,
    WorkerCompositionError,
    compose_worker_handlers,
    load_pricing_catalog,
)

_SAMPLE_INSTRUCTION = "<!-- forge-instruction-version: v1 -->\nInstructions for planner\n"
_SAMPLE_DIGEST = hashlib.sha256(_SAMPLE_INSTRUCTION.encode("utf-8")).hexdigest()


def _write_catalog(path: Path, entries: dict[str, dict[str, str]] | None = None) -> Path:
    if entries is None:
        entries = {
            "google:gemini-2.5-pro": {
                "input_per_million": "1.25",
                "output_per_million": "5.00",
                "cached_input_per_million": "0.3125",
            }
        }
    data = {
        "version": "2026-09-01",
        "entries": entries,
        "currency_minor_exponents": {"USD": 2},
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _make_prompt_root(tmp_path: Path) -> Path:
    prompt_dir = tmp_path / "agents"
    for role in ("planner", "developer", "reviewer"):
        d = prompt_dir / role
        d.mkdir(parents=True, exist_ok=True)
        (d / "instructions.md").write_text(
            f"<!-- forge-instruction-version: v1 -->\nInstructions for {role}\n",
            encoding="utf-8",
        )
    return prompt_dir


from forge.agents.adk_runtime import AdkRuntime
from forge.application.handlers.approvals import ApprovePlanHandler, RequestPlanRevisionHandler
from forge.application.services.plan_evidence import PlanEvidenceValidator
from forge.tools.provider_credentials import LocalProviderCredentialResolver


def test_load_pricing_catalog_success(tmp_path: Path) -> None:
    catalog_path = _write_catalog(tmp_path / "pricing.json")
    catalog = load_pricing_catalog(catalog_path)
    assert catalog.version == "2026-09-01"


def test_load_pricing_catalog_requires_cached_input_price(tmp_path: Path) -> None:
    catalog_path = _write_catalog(
        tmp_path / "pricing.json",
        {
            "google:gemini-2.5-pro": {
                "input_per_million": "1.25",
                "output_per_million": "5.00",
            }
        },
    )
    with pytest.raises(WorkerCompositionError, match="pricing catalog is invalid"):
        load_pricing_catalog(catalog_path)


def test_load_pricing_catalog_missing_or_malformed_fails_context_free(tmp_path: Path) -> None:
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("not json", encoding="utf-8")
    with pytest.raises(WorkerCompositionError, match="pricing catalog is invalid") as exc:
        load_pricing_catalog(bad_json)
    assert str(bad_json) not in str(exc.value)

    nonexistent = tmp_path / "nonexistent.json"
    with pytest.raises(WorkerCompositionError, match="pricing catalog is invalid") as exc:
        load_pricing_catalog(nonexistent)
    assert str(nonexistent) not in str(exc.value)


def test_compose_worker_handlers_missing_config_fails_closed(tmp_path: Path) -> None:
    prompt_root = _make_prompt_root(tmp_path)

    # Missing secret reference
    settings_no_secret = Settings(
        provider_secret_reference="",
        pricing_catalog_path=tmp_path / "pricing.json",
        prompt_root=prompt_root,
    )
    with pytest.raises(WorkerCompositionError, match="provider secret reference is not configured"):
        compose_worker_handlers(settings_no_secret, session_factory=object())  # type: ignore[arg-type]

    # Invalid secret reference rejected by Settings
    with pytest.raises(ProviderCredentialError):
        Settings(
            provider_secret_reference="not-a-valid-secret-uri",
            pricing_catalog_path=tmp_path / "pricing.json",
            prompt_root=prompt_root,
        )

    # Missing pricing catalog path
    settings_no_catalog = Settings(
        provider_secret_reference="secret://forge/gemini-api-key",
        pricing_catalog_path=None,
        prompt_root=prompt_root,
    )
    with pytest.raises(WorkerCompositionError, match="pricing catalog path is not configured"):
        compose_worker_handlers(settings_no_catalog, session_factory=object())  # type: ignore[arg-type]


def test_compose_worker_handlers_missing_prompt_root_fails_closed(tmp_path: Path) -> None:
    settings = Settings(
        provider_secret_reference="secret://forge/gemini-api-key",
        pricing_catalog_path=tmp_path / "pricing.json",
        prompt_root=tmp_path / "nonexistent_prompts",
    )
    with pytest.raises(WorkerCompositionError, match="prompt loader root is unavailable"):
        compose_worker_handlers(settings, session_factory=object())  # type: ignore[arg-type]


def test_compose_worker_handlers_invalid_pricing_catalog_fails_closed(tmp_path: Path) -> None:
    prompt_root = _make_prompt_root(tmp_path)
    bad_catalog = tmp_path / "bad_pricing.json"
    bad_catalog.write_text("invalid json", encoding="utf-8")
    data_root = tmp_path / "data"
    data_root.mkdir()
    settings = Settings(
        data_root=data_root,
        provider_secret_reference="secret://forge/gemini-api-key",
        pricing_catalog_path=bad_catalog,
        prompt_root=prompt_root,
    )
    with pytest.raises(WorkerCompositionError, match="pricing catalog is invalid"):
        compose_worker_handlers(settings, session_factory=object())  # type: ignore[arg-type]


def test_compose_worker_handlers_unavailable_data_root_fails_closed(tmp_path: Path) -> None:
    prompt_root = _make_prompt_root(tmp_path)
    catalog_path = _write_catalog(tmp_path / "pricing.json")
    settings = Settings(
        data_root=tmp_path / "nonexistent_data_root",
        provider_secret_reference="secret://forge/gemini-api-key",
        pricing_catalog_path=catalog_path,
        prompt_root=prompt_root,
    )
    with pytest.raises(WorkerCompositionError, match="secret store initialization failed"):
        compose_worker_handlers(settings, session_factory=object())  # type: ignore[arg-type]


def test_load_pricing_catalog_rejects_numeric_non_string_rates(tmp_path: Path) -> None:
    for bad_rate in (1.25, 1):
        catalog_path = _write_catalog(
            tmp_path / f"pricing_{type(bad_rate).__name__}.json",
            {
                "google:gemini-2.5-pro": {
                    "input_per_million": bad_rate,  # type: ignore[dict-item]
                    "output_per_million": "5.00",
                    "cached_input_per_million": "0.3125",
                }
            },
        )
        with pytest.raises(WorkerCompositionError, match="pricing catalog is invalid"):
            load_pricing_catalog(catalog_path)


def test_load_pricing_catalog_rejects_nonfinite_rates(tmp_path: Path) -> None:
    for nonfinite in ("NaN", "Infinity", "-Infinity"):
        catalog_path = _write_catalog(
            tmp_path / f"pricing_{nonfinite}.json",
            {
                "google:gemini-2.5-pro": {
                    "input_per_million": nonfinite,
                    "output_per_million": "5.00",
                    "cached_input_per_million": "0.3125",
                }
            },
        )
        with pytest.raises(WorkerCompositionError, match="pricing catalog is invalid"):
            load_pricing_catalog(catalog_path)


def test_load_pricing_catalog_rejects_negative_rates(tmp_path: Path) -> None:
    catalog_path = _write_catalog(
        tmp_path / "pricing_negative.json",
        {
            "google:gemini-2.5-pro": {
                "input_per_million": "-1.25",
                "output_per_million": "5.00",
                "cached_input_per_million": "0.3125",
            }
        },
    )
    with pytest.raises(WorkerCompositionError, match="pricing catalog is invalid"):
        load_pricing_catalog(catalog_path)


@pytest.mark.asyncio
async def test_compose_worker_handlers_production_construction(tmp_path: Path) -> None:
    prompt_root = _make_prompt_root(tmp_path)
    catalog_path = _write_catalog(
        tmp_path / "pricing.json",
        {
            "google:gemini-2.5-pro": {
                "input_per_million": "1.25",
                "output_per_million": "5.00",
                "cached_input_per_million": "0.3125",
            }
        },
    )
    data_root = tmp_path / "data"
    data_root.mkdir()
    settings = Settings(
        data_root=data_root,
        github_token_reference="env://FORGE_TEST_GITHUB_TOKEN",
        provider_secret_reference="secret://forge/gemini-api-key",
        pricing_catalog_path=catalog_path,
        prompt_root=prompt_root,
    )

    handlers = compose_worker_handlers(
        settings,
        session_factory=object(),  # type: ignore[arg-type]
    )

    assert set(handlers) == {
        "start_planning",
        "approve_plan",
        "request_plan_revision",
        "prepare_worktree",
        "implement",
        "remediate",
        "validate",
        "review",
        "pause",
        "cancel",
        "resume",
        "request_candidate_changes",
        "teardown_run_resources",
        "approve_pr",
        "publish_pr",
        "monitor_pr",
        "remediate_remote",
        "push_reviewed_pr",
        "approve_merge",
        "merge_pr",
        "update_base",
        "observe_merge_queue",
    }
    assert isinstance(handlers["start_planning"], PlanningHandler)
    assert "git.branch_delete" in handlers.recovery_adapters
    assert handlers["teardown_run_resources"]._branches is not None
    assert isinstance(handlers["approve_plan"], ApprovePlanHandler)
    assert isinstance(handlers["request_plan_revision"], RequestPlanRevisionHandler)

    # Real BoundPlanningGateway wrapping real AdkRuntime with local credential resolver
    gateway = handlers["start_planning"]._service._gateway
    assert isinstance(gateway, BoundPlanningGateway)
    assert isinstance(gateway._runtime, AdkRuntime)
    assert isinstance(gateway._runtime._credential_resolver, LocalProviderCredentialResolver)
    assert gateway._runtime._credential_resolver._secret_store._root == data_root.resolve()
    assert gateway._runtime._credential_reference == "secret://forge/gemini-api-key"
    assert gateway._pricing_catalog.version == "2026-09-01"

    # Real PlanEvidenceValidator assembled with FilesystemArtifactStore and LocalGitRepositoryInspector
    approve_handler = handlers["approve_plan"]
    validator = approve_handler._evidence_validator
    assert isinstance(validator, PlanEvidenceValidator)
    assert isinstance(validator._repository_inspector, LocalGitRepositoryInspector)
    assert validator._data_root == str(data_root)

    revision_handler = handlers["request_plan_revision"]
    assert isinstance(revision_handler._service._evidence_validator, PlanEvidenceValidator)
    assert revision_handler._service._evidence_validator is validator
    assert revision_handler._service._artifact_store._root == (data_root / "artifacts").resolve()

    from forge.application.handlers.merge import ApproveMergeHandler
    from forge.application.handlers.release import ApprovePrHandler
    from forge.release.github_client import GitHubClient
    from forge.release.github_write import GitHubWrite

    assert isinstance(handlers["approve_pr"], ApprovePrHandler)
    assert isinstance(handlers["approve_merge"], ApproveMergeHandler)
    release = handlers["publish_pr"].__self__
    read = release._evidence._github
    writes = release._github
    assert isinstance(read, GitHubClient)
    assert isinstance(writes, GitHubWrite)
    from forge.release.github_queue import GitHubMergeQueue

    queue = handlers["merge_pr"].__self__._queue
    assert isinstance(queue, GitHubMergeQueue)
    assert queue._write is writes
    assert handlers["observe_merge_queue"].__self__._queue is queue
    assert "enqueue_pr" in handlers.recovery_adapters
    assert handlers["remediate_remote"].__self__ is handlers["implement"].__self__
    assert not read._client.is_closed and not writes._client.is_closed
    await handlers.aclose()
    assert read._client.is_closed and writes._client.is_closed


def test_compose_worker_handlers_narrow_seams(tmp_path: Path) -> None:
    prompt_root = _make_prompt_root(tmp_path)
    catalog_path = _write_catalog(tmp_path / "pricing.json")
    data_root = tmp_path / "data"
    data_root.mkdir()
    settings = Settings(
        data_root=data_root,
        provider_secret_reference="secret://forge/gemini-api-key",
        pricing_catalog_path=catalog_path,
        prompt_root=prompt_root,
    )

    class FakeClock:
        def now(self) -> datetime:
            return datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

    class FakeInspector:
        def inspect(self, *args: object, **kwargs: object) -> object:
            raise NotImplementedError

    fake_clock = FakeClock()
    fake_inspector = FakeInspector()

    handlers = compose_worker_handlers(
        settings,
        session_factory=object(),  # type: ignore[arg-type]
        clock=fake_clock,  # type: ignore[arg-type]
        repository_inspector=fake_inspector,  # type: ignore[arg-type]
    )

    assert handlers["approve_plan"]._clock is fake_clock
    assert handlers["request_plan_revision"]._service._clock is fake_clock
    assert handlers["approve_plan"]._evidence_validator._repository_inspector is fake_inspector


@pytest.mark.asyncio
async def test_release_without_configuration_fails_before_using_command_or_database(tmp_path):
    settings = Settings(
        data_root=tmp_path, prompt_root=_make_prompt_root(tmp_path), github_token_reference=""
    )
    handlers = compose_worker_handlers(settings, object(), agent_gateway=object())
    for command_type in (
        "update_base",
        "approve_pr",
        "publish_pr",
        "monitor_pr",
        "push_reviewed_pr",
        "approve_merge",
        "merge_pr",
        "observe_merge_queue",
    ):
        with pytest.raises(WorkerCompositionError, match="GitHub credential reference"):
            await handlers[command_type](None, None)
    await handlers.aclose()


# =========================================================================
# BoundPlanningGateway tests
# =========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_injected_queue_controls_observation_and_recovery(tmp_path, configured):
    from forge.worker.composition import ReleaseDependencies

    queue = object() if configured else None
    dependencies = ReleaseDependencies(object(), object(), lambda _: object(), queue=queue)
    handlers = compose_worker_handlers(
        Settings(data_root=tmp_path, prompt_root=_make_prompt_root(tmp_path)),
        object(), agent_gateway=object(), release_dependencies=dependencies,
    )
    assert handlers["merge_pr"].__self__._queue is queue
    assert ("enqueue_pr" in handlers.recovery_adapters) is configured
    if configured:
        assert handlers["observe_merge_queue"].__self__._queue is queue
        assert handlers.recovery_adapters["enqueue_pr"]._queue is queue
    else:
        with pytest.raises(WorkerCompositionError, match="merge queue runtime"):
            await handlers["observe_merge_queue"](None, None)
    await handlers.aclose()


class _FakeExecutionsRepo:
    def __init__(self, execution: AgentExecution | None) -> None:
        self.execution = execution

    async def get_admission(self, run_id: UUID, execution_id: UUID) -> ExecutionAdmission | None:
        row = self.execution
        if row is None or row.run_id != run_id or row.id != execution_id or row.status != "RUNNING":
            return None
        return ExecutionAdmission(
            run_id=row.run_id,
            step_id=row.step_id,
            agent_execution_id=row.id,
            kind="plan",
            attempt=1,
            role=AgentRole(row.role),
            instruction_version=row.instruction_version,
            provider=row.provider,
            model=row.model,
            input_artifact_id=None,
            transition_from="CREATED",
            transition_to="PLANNING",
            admitted_at=datetime.now(UTC),
            is_new=False,
        )


class _FakeRunsRepo:
    def __init__(self, run: RunSnapshot | None) -> None:
        self._run = run

    async def get(self, run_id: UUID) -> RunSnapshot | None:
        return self._run if self._run and self._run.id == run_id else None


class _FakeProjectsRepo:
    def __init__(self, policy_record: ProjectPolicyRecord | None) -> None:
        self._policy_record = policy_record

    async def get_policy(self, project_id: UUID, version: int) -> ProjectPolicyRecord | None:
        if (
            self._policy_record
            and self._policy_record.project_id == project_id
            and self._policy_record.version == version
        ):
            return self._policy_record
        return None


class _FakeToolCallsRepo:
    async def validate_execution_context(self, *_: object) -> bool:
        return True

    async def count_for_execution(self, _: UUID) -> int:
        return 0

    async def record(self, record: object) -> object:
        return record


class _FakeEventsRepo:
    async def append(self, event: object) -> object:
        return event


class _FakeUoW:
    def __init__(
        self,
        run: RunSnapshot | None,
        execution: AgentExecution | None,
        policy_record: ProjectPolicyRecord | None,
    ) -> None:
        self.runs = _FakeRunsRepo(run)
        self.projects = _FakeProjectsRepo(policy_record)
        self.tool_calls = _FakeToolCallsRepo()
        self.events = _FakeEventsRepo()
        self.executions = _FakeExecutionsRepo(execution)
        self.active = False

    async def __aenter__(self) -> Self:
        self.active = True
        return self

    async def __aexit__(self, *_: object) -> None:
        self.active = False

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_change", ["none", "task", "state", "base", "policy", "budget"])
async def test_bound_planning_gateway_derives_and_validates_request_and_exact_tools(
    tmp_path: Path, binding_change: str
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "README.md").write_text("hello", encoding="utf-8")

    project_id = uuid4()
    run_id = uuid4()
    execution_id = uuid4()
    step_id = uuid4()
    task_id = uuid4()

    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path=str(repo_dir),
        github_repository="forge/test",
        default_branch="main",
        secret_paths=(".env",),
        planner_model=AgentModelPolicy(
            provider="google", model="gemini-2.5-pro", max_tool_calls=100
        ),
    )
    policy_record = ProjectPolicyRecord(
        project_id=project_id,
        version=1,
        policy_digest="a" * 64,
        document_schema_version=1,
        document=policy.model_dump(mode="json"),
    )
    run = RunSnapshot(
        id=run_id,
        project_id=project_id,
        task_id=task_id,
        state=RunState.PLANNING,
        policy_version=1,
        base_sha="a" * 40,
    )
    execution = AgentExecution(
        id=execution_id,
        run_id=run_id,
        step_id=step_id,
        role="planner",
        instruction_version="v1",
        provider="google",
        model="gemini-2.5-pro",
        status="RUNNING",
    )

    uow = _FakeUoW(run, execution, policy_record)
    uow_factory = lambda: uow

    prompt_root = _make_prompt_root(tmp_path)
    prompt_loader = PromptLoader(prompt_root)
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    redactor = Redactor()

    captured_bound_tools: list[BoundAdkTools] = []
    invoked_outside_tx = False

    class CapturedGateway:
        def __init__(self, tool_provider: object) -> None:
            self.tool_provider = tool_provider

        async def execute(self, req: AgentRequest) -> AgentResult:
            nonlocal invoked_outside_tx
            invoked_outside_tx = not uow.active
            assert not uow.active, "Provider invoked inside database transaction"
            tools = self.tool_provider.tools_for(req)
            captured_bound_tools.append(tools)
            return AgentResult(
                execution_id=req.execution_id,
                role=req.role,
                finish_status=AgentFinishStatus.SUCCEEDED,
                output=PlanOutput(
                    summary="Test plan",
                    assumptions=(),
                    affected_components=(),
                    steps=("Step 1",),
                    required_checks=("Check 1",),
                    risks=("Risk 1",),
                    security_considerations=(),
                    dependency_changes=(),
                ),
                parent_execution_id=None,
                provider=req.provider,
                model=req.model,
                instruction_digest=req.instruction_digest,
                usage=UsageRecord(
                    provider=req.provider,
                    model=req.model,
                    pricing_version="v1",
                    currency="USD",
                    estimated_cost_minor=10,
                    duration_ms=10,
                    tool_call_count=0,
                ),
                tool_call_count=0,
                duration_ms=10,
            )

    bound_gateway = BoundPlanningGateway(
        unit_of_work_factory=uow_factory,
        artifact_store=artifact_store,
        prompt_loader=prompt_loader,
        redactor=redactor,
        underlying_gateway_factory=CapturedGateway,
    )

    planner_input = PlannerInput(
        original_task=UntrustedContent.from_text(
            "Do task", source_kind=UntrustedSourceKind.TASK, source_reference="task-1"
        ),
        base_commit="a" * 40,
        repository_tree=UntrustedContent.from_text(
            ".", source_kind=UntrustedSourceKind.REPOSITORY_TREE, source_reference="."
        ),
        relevant_instructions=(),
        policy_summary=PolicySummary.from_policy(policy),
    )
    request = AgentRequest(
        execution_id=execution_id,
        run_id=run_id,
        task_id=task_id,
        role=AgentRole.PLANNER,
        context=planner_input,
        parent_execution_id=None,
        provider="google",
        model="gemini-2.5-pro",
        instruction_version="v1",
        system_instruction=_SAMPLE_INSTRUCTION,
        instruction_digest=_SAMPLE_DIGEST,
        allowed_tools=(
            ToolName.REPOSITORY_LIST_FILES,
            ToolName.REPOSITORY_READ_FILE,
            ToolName.REPOSITORY_SEARCH,
            ToolName.REPOSITORY_READ_INSTRUCTIONS,
        ),
        budget=AgentBudget.from_model_policy(policy.planner_model),
    )

    if binding_change == "task":
        request = request.model_copy(update={"task_id": uuid4()})
    elif binding_change == "state":
        uow.runs._run = replace(run, state=RunState.CANCELLED)
    elif binding_change == "base":
        request = request.model_copy(
            update={"context": planner_input.model_copy(update={"base_commit": "b" * 40})}
        )
    elif binding_change == "policy":
        changed_policy = planner_input.policy_summary.model_copy(update={"policy_version": 2})
        request = request.model_copy(
            update={"context": planner_input.model_copy(update={"policy_summary": changed_policy})}
        )
    elif binding_change == "budget":
        request = request.model_copy(
            update={"budget": request.budget.model_copy(update={"max_cost_minor": 999999})}
        )
    if binding_change != "none":
        with pytest.raises(AgentGatewayError):
            await bound_gateway.execute(request)
        assert not captured_bound_tools
        return
    result = await bound_gateway.execute(request)
    assert result.finish_status == AgentFinishStatus.SUCCEEDED
    assert invoked_outside_tx is True
    assert len(captured_bound_tools) == 1
    tools = captured_bound_tools[0]

    # Exact four repository read tools
    assert tools.names == (
        ToolName.REPOSITORY_LIST_FILES,
        ToolName.REPOSITORY_READ_FILE,
        ToolName.REPOSITORY_SEARCH,
        ToolName.REPOSITORY_READ_INSTRUCTIONS,
    )
    # NO developer capabilities
    for name in tools.names:
        assert name in {
            ToolName.REPOSITORY_LIST_FILES,
            ToolName.REPOSITORY_READ_FILE,
            ToolName.REPOSITORY_SEARCH,
            ToolName.REPOSITORY_READ_INSTRUCTIONS,
        }
        assert name != ToolName.REPOSITORY_WRITE_FILE
        assert name != ToolName.GIT_COMMIT
        assert name != ToolName.BUILD_RUN_NAMED_CHECK


@pytest.mark.asyncio
async def test_bound_planning_gateway_binding_mismatches_fail_closed(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    project_id = uuid4()
    run_id = uuid4()
    execution_id = uuid4()
    step_id = uuid4()
    task_id = uuid4()

    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path=str(repo_dir),
        github_repository="forge/test",
        default_branch="main",
        planner_model=AgentModelPolicy(provider="google", model="gemini-2.5-pro"),
    )
    policy_record = ProjectPolicyRecord(
        project_id=project_id,
        version=1,
        policy_digest="a" * 64,
        document_schema_version=1,
        document=policy.model_dump(mode="json"),
    )

    # 1. Run not found
    uow_no_run = _FakeUoW(None, None, None)
    gw = BoundPlanningGateway(
        unit_of_work_factory=lambda: uow_no_run,
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        prompt_loader=PromptLoader(_make_prompt_root(tmp_path)),
        redactor=Redactor(),
    )
    planner_input = PlannerInput(
        original_task=UntrustedContent.from_text(
            "Do task", source_kind=UntrustedSourceKind.TASK, source_reference="task-1"
        ),
        base_commit="a" * 40,
        repository_tree=UntrustedContent.from_text(
            ".", source_kind=UntrustedSourceKind.REPOSITORY_TREE, source_reference="."
        ),
        relevant_instructions=(),
        policy_summary=PolicySummary.from_policy(policy),
    )
    req = AgentRequest(
        execution_id=execution_id,
        run_id=run_id,
        task_id=task_id,
        role=AgentRole.PLANNER,
        context=planner_input,
        parent_execution_id=None,
        provider="google",
        model="gemini-2.5-pro",
        instruction_version="v1",
        system_instruction=_SAMPLE_INSTRUCTION,
        instruction_digest=_SAMPLE_DIGEST,
        allowed_tools=(
            ToolName.REPOSITORY_LIST_FILES,
            ToolName.REPOSITORY_READ_FILE,
            ToolName.REPOSITORY_SEARCH,
            ToolName.REPOSITORY_READ_INSTRUCTIONS,
        ),
        budget=AgentBudget.from_model_policy(policy.planner_model),
    )
    with pytest.raises(AgentGatewayError):
        await gw.execute(req)

    # 2. Execution role mismatch
    run = RunSnapshot(
        id=run_id,
        project_id=project_id,
        task_id=task_id,
        state=RunState.PLANNING,
        policy_version=1,
        base_sha="a" * 40,
    )
    execution_bad_role = AgentExecution(
        id=execution_id,
        run_id=run_id,
        step_id=step_id,
        role="developer",
        instruction_version="v1",
        provider="google",
        model="gemini-2.5-pro",
        status="RUNNING",
    )
    uow_bad_role = _FakeUoW(run, execution_bad_role, policy_record)
    gw = BoundPlanningGateway(
        unit_of_work_factory=lambda: uow_bad_role,
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        prompt_loader=PromptLoader(_make_prompt_root(tmp_path)),
        redactor=Redactor(),
    )
    with pytest.raises(AgentGatewayError):
        await gw.execute(req)

    # 3. Execution status not RUNNING
    execution_done = AgentExecution(
        id=execution_id,
        run_id=run_id,
        step_id=step_id,
        role="planner",
        instruction_version="v1",
        provider="google",
        model="gemini-2.5-pro",
        status="SUCCEEDED",
    )
    uow_done = _FakeUoW(run, execution_done, policy_record)
    gw = BoundPlanningGateway(
        unit_of_work_factory=lambda: uow_done,
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        prompt_loader=PromptLoader(_make_prompt_root(tmp_path)),
        redactor=Redactor(),
    )
    with pytest.raises(AgentGatewayError):
        await gw.execute(req)


@pytest.mark.asyncio
async def test_bound_planning_gateway_independent_request_binding(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    prompt_root = _make_prompt_root(tmp_path)
    prompt_loader = PromptLoader(prompt_root)
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    redactor = Redactor()

    project_id = uuid4()
    run_id_1 = uuid4()
    run_id_2 = uuid4()
    exec_id_1 = uuid4()
    exec_id_2 = uuid4()
    step_id_1 = uuid4()
    step_id_2 = uuid4()
    task_id = uuid4()

    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path=str(repo_dir),
        github_repository="forge/test",
        default_branch="main",
        planner_model=AgentModelPolicy(provider="google", model="gemini-2.5-pro"),
    )
    policy_record = ProjectPolicyRecord(
        project_id=project_id,
        version=1,
        policy_digest="a" * 64,
        document_schema_version=1,
        document=policy.model_dump(mode="json"),
    )

    run_1 = RunSnapshot(
        id=run_id_1,
        project_id=project_id,
        task_id=task_id,
        state=RunState.PLANNING,
        policy_version=1,
        base_sha="a" * 40,
    )
    run_2 = RunSnapshot(
        id=run_id_2,
        project_id=project_id,
        task_id=task_id,
        state=RunState.PLANNING,
        policy_version=1,
        base_sha="a" * 40,
    )
    exec_1 = AgentExecution(
        id=exec_id_1,
        run_id=run_id_1,
        step_id=step_id_1,
        role="planner",
        instruction_version="v1",
        provider="google",
        model="gemini-2.5-pro",
        status="RUNNING",
    )
    exec_2 = AgentExecution(
        id=exec_id_2,
        run_id=run_id_2,
        step_id=step_id_2,
        role="planner",
        instruction_version="v1",
        provider="google",
        model="gemini-2.5-pro",
        status="RUNNING",
    )

    current_uow: _FakeUoW | None = None

    class DynamicUoW:
        async def __aenter__(self) -> _FakeUoW:
            assert current_uow is not None
            return current_uow

        async def __aexit__(self, *_: object) -> None:
            pass

    observed_providers: list[object] = []

    class CapturingGateway:
        def __init__(self, tool_provider: object) -> None:
            self.tool_provider = tool_provider
            observed_providers.append(tool_provider)

        async def execute(self, req: AgentRequest) -> AgentResult:
            return AgentResult(
                execution_id=req.execution_id,
                role=req.role,
                finish_status=AgentFinishStatus.SUCCEEDED,
                output=PlanOutput(
                    summary="Test plan",
                    assumptions=(),
                    affected_components=(),
                    steps=("Step 1",),
                    required_checks=("Check 1",),
                    risks=("Risk 1",),
                    security_considerations=(),
                    dependency_changes=(),
                ),
                parent_execution_id=None,
                provider=req.provider,
                model=req.model,
                instruction_digest=req.instruction_digest,
                usage=UsageRecord(
                    provider=req.provider,
                    model=req.model,
                    pricing_version="v1",
                    currency="USD",
                    estimated_cost_minor=0,
                    duration_ms=10,
                    tool_call_count=0,
                ),
                tool_call_count=0,
                duration_ms=10,
            )

    bound_gateway = BoundPlanningGateway(
        unit_of_work_factory=DynamicUoW,
        artifact_store=artifact_store,
        prompt_loader=prompt_loader,
        redactor=redactor,
        underlying_gateway_factory=CapturingGateway,
    )

    planner_input = PlannerInput(
        original_task=UntrustedContent.from_text(
            "Task", source_kind=UntrustedSourceKind.TASK, source_reference="t"
        ),
        base_commit="a" * 40,
        repository_tree=UntrustedContent.from_text(
            ".", source_kind=UntrustedSourceKind.REPOSITORY_TREE, source_reference="."
        ),
        relevant_instructions=(),
        policy_summary=PolicySummary.from_policy(policy),
    )

    req_1 = AgentRequest(
        execution_id=exec_id_1,
        run_id=run_id_1,
        task_id=task_id,
        role=AgentRole.PLANNER,
        context=planner_input,
        parent_execution_id=None,
        provider="google",
        model="gemini-2.5-pro",
        instruction_version="v1",
        system_instruction=_SAMPLE_INSTRUCTION,
        instruction_digest=_SAMPLE_DIGEST,
        allowed_tools=(
            ToolName.REPOSITORY_LIST_FILES,
            ToolName.REPOSITORY_READ_FILE,
            ToolName.REPOSITORY_SEARCH,
            ToolName.REPOSITORY_READ_INSTRUCTIONS,
        ),
        budget=AgentBudget.from_model_policy(policy.planner_model),
    )
    req_2 = AgentRequest(
        execution_id=exec_id_2,
        run_id=run_id_2,
        task_id=task_id,
        role=AgentRole.PLANNER,
        context=planner_input,
        parent_execution_id=None,
        provider="google",
        model="gemini-2.5-pro",
        instruction_version="v1",
        system_instruction=_SAMPLE_INSTRUCTION,
        instruction_digest=_SAMPLE_DIGEST,
        allowed_tools=(
            ToolName.REPOSITORY_LIST_FILES,
            ToolName.REPOSITORY_READ_FILE,
            ToolName.REPOSITORY_SEARCH,
            ToolName.REPOSITORY_READ_INSTRUCTIONS,
        ),
        budget=AgentBudget.from_model_policy(policy.planner_model),
    )

    current_uow = _FakeUoW(run_1, exec_1, policy_record)
    await bound_gateway.execute(req_1)

    current_uow = _FakeUoW(run_2, exec_2, policy_record)
    await bound_gateway.execute(req_2)

    assert len(observed_providers) == 2
    # Ensure independent tool provider instances
    assert observed_providers[0] is not observed_providers[1]

    # Calling tool provider 1 with request 2 fails
    with pytest.raises(AgentGatewayError):
        observed_providers[0].tools_for(req_2)

    # A same-execution request cannot substitute another run into the closure.
    with pytest.raises(AgentGatewayError):
        observed_providers[0].tools_for(req_1.model_copy(update={"run_id": uuid4()}))
