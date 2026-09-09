"""Evaluation execution service running versioned suites against Forge gateways."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.agents.adk_gateway import AdkToolProvider
from forge.agents.fake_gateway import FakeAgentGateway, FakeAgentStep
from forge.agents.prompt_loader import PromptLoader
from forge.application.adapters.git import canonical_path_key
from forge.application.ports.agents import AgentGateway
from forge.application.ports.evaluations import (
    EvaluationCaseResult,
    EvaluationServicePort,
    EvaluationSuiteResult,
)
from forge.application.ports.executions import database_status_for_finish
from forge.application.ports.provider_credentials import (
    ProviderCredentialResolverPort,
    validate_provider_secret_reference,
)
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.agent_results import (
    safe_usage,
    validate_agent_result,
)
from forge.application.services.tools import ControlledToolService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    _ALLOWED_ROLE_TOOLS,
    AgentBudget,
    AgentFinishStatus,
    AgentRequest,
    AgentResult,
    DeveloperInput,
    DeveloperOutput,
    PlannerInput,
    PolicySummary,
    ReviewerInput,
    ReviewOutput,
    UntrustedContent,
    UntrustedSourceKind,
)
from forge.domain.evaluation import (
    regression_failures,
    score_development,
    score_plan,
    score_review,
    score_usage,
)
from forge.domain.event import RunEvent
from forge.domain.plan import PlanOutput
from forge.domain.policy import AgentModelPolicy, CommandSpec, ProjectPolicy, RunnerMode, StepKind
from forge.domain.run import RunState
from forge.domain.tool import ToolAuthorizationContext, ToolName, repository_resource_identity
from forge.evaluations.contracts import EvaluationCaseContract
from forge.evaluations.loader import load_evaluation_cases, load_expected_output
from forge.evaluations.materializer import (
    MaterializedFixture,
    materialize_fixture,
)
from forge.evaluations.runtime import (
    EvaluationToolObserver,
    observe_developer_execution,
    resolve_live_evaluation_gateway,
)
from forge.observability.usage import PricingCatalog
from forge.persistence.models import (
    AgentExecution,
    ModelUsage,
    Project,
    ProjectPolicyVersion,
    Run,
    Step,
    Task,
)
from forge.persistence.models.evaluation import EvaluationBaseline, EvaluationCase, EvaluationSuite
from forge.persistence.repositories.artifacts import ArtifactRepository
from forge.persistence.repositories.evaluations import (
    EvaluationConflict,
    EvaluationRepository,
)
from forge.persistence.repositories.events import PostgresEventRepository
from forge.persistence.repositories.projects import PostgresProjectRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from forge.tools.repository import RepositoryReader
from forge.worker.delivery_runtime import DeliveryRuntime
from forge.worker.evaluation_tools import EvaluationToolRegistry


def _canonical_json_bytes(data: object) -> bytes:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _read_repository_tree(root: Path) -> str:
    paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        rel_dir = Path(dirpath).relative_to(root)
        if any(part == ".git" for part in rel_dir.parts):
            continue
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for filename in filenames:
            rel_file = (rel_dir / filename).as_posix() if rel_dir != Path(".") else filename
            paths.append(rel_file)
    paths.sort()
    return "\n".join(paths)


def _extract_fixture_commit_diff(path: Path, commit: str) -> str:
    res = subprocess.run(
        ["git", "diff-tree", "-p", "--root", commit],
        cwd=str(path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    diff_text = res.stdout if res.returncode == 0 and res.stdout else ""
    if not diff_text:
        res2 = subprocess.run(
            ["git", "show", "-p", "--format=", commit],
            cwd=str(path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        diff_text = res2.stdout if res2.returncode == 0 else ""
    return diff_text


class EvaluationService(EvaluationServicePort):
    """Execution boundary orchestrating agent evaluation suites and durable storage."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_store: FilesystemArtifactStore | None = None,
        *,
        prompt_loader: PromptLoader | None = None,
        default_gateway: AgentGateway | None = None,
        credential_resolver: ProviderCredentialResolverPort | None = None,
        pricing_catalog: PricingCatalog | None = None,
        fixtures_dir: Path | None = None,
        expected_dir: Path | None = None,
        settings: Settings | None = None,
        live_tool_provider: AdkToolProvider | None = None,
        trusted_fixture_execution: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or Settings(process_role="cli")
        self._artifact_store = artifact_store or FilesystemArtifactStore(
            self._settings.artifact_root
        )
        self._prompt_loader = prompt_loader
        self._default_gateway = default_gateway
        self._credential_resolver = credential_resolver
        self._pricing_catalog = pricing_catalog
        self._fixtures_dir = fixtures_dir
        self._expected_dir = expected_dir
        self._live_tool_provider = live_tool_provider
        if type(trusted_fixture_execution) is not bool:
            raise ValueError("trusted fixture execution must be explicitly selected")
        self._trusted_fixture_execution = trusted_fixture_execution
        self._evaluation_tool_registry = EvaluationToolRegistry()

    def _resolve_paths(
        self, fixtures_dir: Path | None, expected_dir: Path | None
    ) -> tuple[Path, Path]:
        resolved_fixtures = fixtures_dir or self._fixtures_dir
        resolved_expected = expected_dir or self._expected_dir
        if resolved_fixtures is None or resolved_expected is None:
            # Search repo root upwards
            current = Path(__file__).resolve()
            for parent in [current, *list(current.parents)]:
                candidate_fix = parent / "tests" / "evaluations" / "fixtures"
                candidate_exp = parent / "tests" / "evaluations" / "expected"
                if candidate_fix.is_dir() and candidate_exp.is_dir():
                    resolved_fixtures = resolved_fixtures or candidate_fix
                    resolved_expected = resolved_expected or candidate_exp
                    break
        if resolved_fixtures is None or not resolved_fixtures.is_dir():
            raise ValueError(f"fixtures directory not found: {resolved_fixtures}")
        if resolved_expected is None or not resolved_expected.is_dir():
            raise ValueError(f"expected directory not found: {resolved_expected}")
        return resolved_fixtures, resolved_expected

    async def _mark_evaluation_fixture_ready_for_review(self, run_id: UUID) -> None:
        """Move an isolated, prepared reviewer fixture into its tool-active phase.

        This is deliberately local to evaluation fixture lifecycle creation.  It
        does not apply an ordinary delivery transition or manufacture a plan
        approval: the run was created only for the admitted fixture and already
        has its preparation admission event before the managed worktree exists.
        """

        async with self._session_factory() as session, session.begin():
            result = await session.execute(select(Run).where(Run.id == run_id).with_for_update())
            run = result.scalar_one_or_none()
            if run is None or run.state != RunState.PREPARING_WORKTREE.value:
                raise EvaluationConflict("evaluation reviewer fixture is not preparing")
            run.state = RunState.REVIEWING.value
            run.version += 1
            await session.flush()
            await PostgresEventRepository(session).append(
                RunEvent(
                    run_id=run_id,
                    run_version=run.version,
                    event_type="evaluation.fixture_prepared_for_review",
                    actor_class="operator",
                    actor_id=None,
                    payload={"evaluation_fixture": True},
                )
            )

    def _get_prompt_loader(self) -> PromptLoader:
        if self._prompt_loader is not None:
            return self._prompt_loader
        prompt_root = self._settings.prompt_root
        if prompt_root is None or not prompt_root.is_dir():
            current = Path(__file__).resolve()
            for parent in [current, *list(current.parents)]:
                candidate = parent / "agents"
                if candidate.is_dir() and (candidate / "planner" / "instructions.md").is_file():
                    prompt_root = candidate
                    break
        if prompt_root is None or not prompt_root.is_dir():
            raise ValueError("prompt instructions directory not found")
        return PromptLoader(prompt_root)

    def _build_fake_gateway(
        self, cases: Sequence[EvaluationCaseContract], expected_dir: Path
    ) -> FakeAgentGateway:
        scripts: dict[AgentRole, list[FakeAgentStep]] = {}
        for case in cases:
            expected_file_name = f"{case.case_key.replace('/', '-')}.json"
            expected_path = expected_dir / expected_file_name
            if expected_path.is_file():
                raw_data = load_expected_output(expected_path)
                output: PlanOutput | DeveloperOutput | ReviewOutput
                if case.role == AgentRole.PLANNER:
                    output = PlanOutput.model_validate(raw_data)
                elif case.role == AgentRole.DEVELOPER:
                    output = DeveloperOutput.model_validate(raw_data)
                elif case.role == AgentRole.REVIEWER:
                    output = ReviewOutput.model_validate(raw_data)
                else:
                    continue
                step = FakeAgentStep.success(output=output)
                scripts.setdefault(case.role, []).append(step)
        if not scripts:
            raise ValueError("no expected outputs found for deterministic cases")
        return FakeAgentGateway(scripts)

    @staticmethod
    def _deterministic_developer_observer(
        case: EvaluationCaseContract,
    ) -> EvaluationToolObserver:
        """Create synthetic metric observations for the explicit fake suite.

        This is limited to the deterministic gateway, whose expected fixture is
        the test oracle.  Live runs never manufacture observations: they must
        receive receipts from ``ControlledEvaluationToolProvider``.
        """
        observer = EvaluationToolObserver()
        observer.changed_paths.update(case.allowed_paths)
        for name in case.required_checks:
            observer.record_check_result(name, passed=True)
        for name in case.required_tests:
            observer.record_test_result(name, passed=True)
        for name in case.required_assertions:
            observer.assertion_results[name] = True
        return observer

    async def run_suite(
        self,
        suite_name: str = "deterministic",
        *,
        idempotency_key: str | None = None,
        fixture_version: str | None = None,
        metric_version: str | None = None,
        fixtures_dir: Path | None = None,
        expected_dir: Path | None = None,
        cases: Mapping[str, EvaluationCaseContract]
        | Sequence[EvaluationCaseContract]
        | None = None,
        gateway: AgentGateway | None = None,
        provider_reference: str | None = None,
        live_model: str | None = None,
        baseline_fixture_version: str | None = None,
        baseline_metric_version: str | None = None,
        floors: Mapping[str, float] | None = None,
        ceilings: Mapping[str, float] | None = None,
        is_live: bool = False,
        promoted_baseline: bool = False,
        baseline_name: str | None = None,
        baseline_id: UUID | None = None,
    ) -> EvaluationSuiteResult:
        is_live_mode = is_live or suite_name == "live"
        if is_live_mode:
            if not provider_reference:
                raise ValueError("live evaluation requires an explicit --provider-reference")
            validate_provider_secret_reference(provider_reference)
            if self._credential_resolver is None:
                raise ValueError("provider credential resolver is required for live evaluation")
            if not isinstance(live_model, str) or not live_model.strip():
                raise ValueError("live evaluation requires an explicit model")

        persisted_baseline: EvaluationBaseline | None = None
        has_baseline_selection = (
            promoted_baseline or baseline_name is not None or baseline_id is not None
        )
        if has_baseline_selection:
            async with self._session_factory() as session:
                repo = EvaluationRepository(session)
                lookup_name = baseline_name or ("live" if is_live_mode else suite_name)
                persisted_baseline = await repo.get_baseline(
                    name=lookup_name if baseline_id is None else None,
                    baseline_id=baseline_id,
                )
                if persisted_baseline is None:
                    raise EvaluationConflict(
                        f"no persisted baseline found for promotion comparison: "
                        f"name={lookup_name}, id={baseline_id}"
                    )

        # Discover or use supplied cases
        cases_map: dict[str, EvaluationCaseContract] = {}
        if cases is not None:
            if isinstance(cases, Mapping):
                cases_map = dict(cases)
            else:
                cases_map = {c.case_key: c for c in cases}
        else:
            resolved_fixtures, _ = self._resolve_paths(fixtures_dir, expected_dir)
            cases_map = load_evaluation_cases(resolved_fixtures)

        if not cases_map:
            raise ValueError("no evaluation cases found to run")

        sorted_cases = [cases_map[k] for k in sorted(cases_map.keys())]

        eff_fixture_version = (
            fixture_version or sorted_cases[0].fixture_version
            if sorted_cases
            else "eval-fixture-v1"
        )
        eff_metric_version = metric_version or "eval-metrics-v1"
        eff_idempotency_key = (
            idempotency_key or f"eval-{suite_name}-{eff_fixture_version}-{eff_metric_version}"
        )

        eff_baseline_fixture_version = (
            persisted_baseline.fixture_version
            if persisted_baseline is not None
            else baseline_fixture_version
        )
        eff_baseline_metric_version = (
            persisted_baseline.metric_version
            if persisted_baseline is not None
            else baseline_metric_version
        )
        merged_floors: dict[str, float] = {}
        merged_ceilings: dict[str, float] = {}
        if persisted_baseline is not None:
            merged_floors.update(persisted_baseline.floors)
            merged_ceilings.update(persisted_baseline.ceilings)
        if floors:
            merged_floors.update(floors)
        if ceilings:
            merged_ceilings.update(ceilings)

        if persisted_baseline is not None:
            if eff_fixture_version != persisted_baseline.fixture_version:
                raise EvaluationConflict(
                    f"suite fixture version {eff_fixture_version} does not match baseline fixture version {persisted_baseline.fixture_version}"
                )
            if eff_metric_version != persisted_baseline.metric_version:
                raise EvaluationConflict(
                    f"suite metric version {eff_metric_version} does not match baseline metric version {persisted_baseline.metric_version}"
                )

        manifest = {c.case_key: c.role.value for c in sorted_cases}

        # Begin suite in database
        async with self._session_factory() as session, session.begin():
            repo = EvaluationRepository(session)
            suite_summary = await repo.begin_suite(
                name=suite_name,
                fixture_version=eff_fixture_version,
                metric_version=eff_metric_version,
                idempotency_key=eff_idempotency_key,
                cases=manifest,
            )

        prompt_loader = self._get_prompt_loader()

        # Gateway resolution
        active_gateway = gateway or self._default_gateway
        # The resolved live gateway is always an AgentGateway.  Preserve its
        # provenance here; testing it for ``None`` after resolution would make
        # the production controlled-tool binding unreachable.
        bind_controlled_tools = is_live_mode and gateway is None and self._default_gateway is None
        if active_gateway is None:
            if is_live_mode:
                assert provider_reference is not None
                assert self._credential_resolver is not None
                active_gateway = resolve_live_evaluation_gateway(
                    credential_resolver=self._credential_resolver,
                    provider_reference=provider_reference,
                    prompt_loader=prompt_loader,
                    pricing_catalog=self._pricing_catalog,
                    tool_provider=self._live_tool_provider or self._evaluation_tool_registry,
                )
            else:
                _, res_expected = self._resolve_paths(fixtures_dir, expected_dir)
                active_gateway = self._build_fake_gateway(sorted_cases, res_expected)

        if is_live_mode:
            provider = getattr(active_gateway, "_supported_provider", "google")
            assert live_model is not None
            model = live_model
        else:
            provider = (
                "fake" if suite_name == "deterministic" else (provider_reference or "live-provider")
            )
            model = "deterministic" if suite_name == "deterministic" else "live-model"

        case_results: list[EvaluationCaseResult] = []

        # Execute each case
        for case in sorted_cases:
            if persisted_baseline is not None and case.case_key in persisted_baseline.cases:
                bcase = persisted_baseline.cases[case.case_key]
                if bcase.get("fixture_version") and bcase["fixture_version"] != eff_fixture_version:
                    raise EvaluationConflict(
                        f"case {case.case_key} fixture version {eff_fixture_version} does not match baseline {bcase['fixture_version']}"
                    )
                if bcase.get("metric_version") and bcase["metric_version"] != eff_metric_version:
                    raise EvaluationConflict(
                        f"case {case.case_key} metric version mismatch with baseline: {bcase['metric_version']}"
                    )

            loaded_prompt = prompt_loader.load(case.role)

            with materialize_fixture(case) as materialized:
                # Atomically lock suite and case; admit exactly one execution (E27-P02)
                settled_info: dict[str, Any] | None = None
                admitted_run_id: UUID | None = None
                admitted_execution_id: UUID | None = None
                admitted_task_id: UUID | None = None
                admitted_policy: ProjectPolicy | None = None

                async with self._session_factory() as session, session.begin():
                    repo = EvaluationRepository(session)
                    case_row = await repo.admit_case(
                        suite_id=suite_summary.id,
                        case_key=case.case_key,
                    )
                    if case_row.status in {"passed", "failed", "skipped"}:
                        # Settled replay: load persisted ModelUsage and AgentExecution (E27-P01)
                        bound = (
                            await session.execute(
                                select(ModelUsage, AgentExecution)
                                .join(
                                    AgentExecution,
                                    AgentExecution.id == ModelUsage.agent_execution_id,
                                )
                                .where(
                                    ModelUsage.id == case_row.model_usage_id,
                                    AgentExecution.run_id == ModelUsage.run_id,
                                )
                            )
                        ).one_or_none()
                        if bound is None:
                            raise EvaluationConflict(
                                f"persisted execution evidence is missing for settled case {case.case_key}"
                            )
                        settled_usage, settled_exec = bound
                        settled_info = {
                            "status": case_row.status,
                            "metrics": dict(case_row.metrics),
                            "run_id": settled_usage.run_id,
                            "execution_id": settled_exec.id,
                            "usage_id": settled_usage.id,
                            "input_digest": case_row.input_artifact_digest,
                            "output_digest": case_row.output_artifact_digest,
                            "fixture_version": case_row.fixture_version,
                            "metric_version": case_row.metric_version,
                            "provider": settled_exec.provider,
                            "model": settled_exec.model,
                            "instruction_version": settled_exec.instruction_version,
                            "instruction_digest": settled_exec.instruction_digest,
                        }
                    else:
                        # Admitted as running: record Project, Policy, Task, Run, and AgentExecution (E27-P03)
                        repo_slug = case.case_key.replace("/", "-")
                        # A materialized fixture is an isolated, disposable
                        # repository.  It must never inherit a Project whose
                        # path was removed by a previous suite execution.
                        project_id = uuid4()
                        task_id = uuid4()
                        run_id = uuid4()
                        execution_id = uuid4()
                        github_repo = f"evaluation/{repo_slug}-{run_id.hex}"
                        path_key = canonical_path_key(str(materialized.path))
                        project_repo = PostgresProjectRepository(session)
                        existing_project = await session.scalar(
                            select(Project).where(
                                Project.canonical_path_key == path_key
                            )
                        )
                        if existing_project is not None:
                            project_id = existing_project.id
                            policy_row = await session.scalar(
                                select(ProjectPolicyVersion).where(
                                    ProjectPolicyVersion.project_id == project_id,
                                    ProjectPolicyVersion.version
                                    == (existing_project.current_policy_version or 1),
                                )
                            )
                            assert policy_row is not None
                            policy = ProjectPolicy.model_validate(policy_row.document)
                        else:
                            policy = ProjectPolicy(
                                # ProjectPolicy identity is the durable project
                                # resource identity used by planner reads.
                                id=project_id,
                                version=1,
                                repository_path=str(materialized.path),
                                github_repository=github_repo,
                                default_branch="main",
                                runner_mode=RunnerMode.TRUSTED_HOST
                                if self._trusted_fixture_execution
                                else RunnerMode.DOCKER,
                                trusted_project=self._trusted_fixture_execution,
                                planner_model=AgentModelPolicy(provider=provider, model=model),
                                developer_model=AgentModelPolicy(provider=provider, model=model),
                                reviewer_model=AgentModelPolicy(provider=provider, model=model),
                                commands=case.check_commands
                                or tuple(
                                    CommandSpec(
                                        kind=StepKind.TEST,
                                        name=name,
                                        argv=("python", "-m", "pytest", "-q"),
                                        timeout_seconds=300,
                                    )
                                    for name in case.required_checks
                                ),
                            )
                            policy_document = policy.model_dump(mode="json")
                            policy_digest = hashlib.sha256(
                                _canonical_json_bytes(policy_document)
                            ).hexdigest()

                            await project_repo.create(
                                project_id=project_id,
                                name=f"eval-{repo_slug}",
                                canonical_path=str(materialized.path),
                                canonical_path_key=path_key,
                                github_repository=github_repo,
                                default_branch="main",
                                policy_digest=policy_digest,
                                policy_document=policy_document,
                            )

                        task = Task(
                            id=task_id,
                            project_id=project_id,
                            normalized_text=case.task,
                            task_digest=hashlib.sha256(case.task.encode("utf-8")).hexdigest(),
                        )
                        session.add(task)
                        await session.flush()

                        # Evaluation creation is a dedicated operator-authorized
                        # fixture lifecycle, not an ordinary product run or a
                        # forged plan approval.  Its durable creation event
                        # precedes DeliveryRuntime's first worktree effect.
                        needs_worktree = case.role is not AgentRole.PLANNER
                        branch_name = f"forge/evaluation/{run_id.hex}" if needs_worktree else None
                        session.add(
                            Run(
                                id=run_id,
                                project_id=project_id,
                                task_id=task_id,
                                policy_version=1,
                                state=(
                                    RunState.PREPARING_WORKTREE.value
                                    if needs_worktree
                                    else RunState.PLANNING.value
                                ),
                                version=0,
                                token_budget=0,
                                cost_budget_minor=0,
                                duration_budget_seconds=0,
                                base_ref="main",
                                base_sha=materialized.base_commit,
                                branch_name=branch_name,
                                database_state="DISABLED",
                            )
                        )
                        await session.flush()
                        await PostgresEventRepository(session).append(
                            RunEvent(
                                run_id=run_id,
                                run_version=0,
                                event_type=(
                                    "evaluation.fixture_preparation_admitted"
                                    if needs_worktree
                                    else "evaluation.fixture_planning_admitted"
                                ),
                                actor_class="operator",
                                actor_id=None,
                                payload={
                                    "suite_id": str(suite_summary.id),
                                    "case_key": case.case_key,
                                    "fixture_identity": materialized.fixture_identity,
                                    "policy_version": 1,
                                },
                            )
                        )

                        step_id = uuid4()
                        session.add(
                            Step(
                                id=step_id,
                                run_id=run_id,
                                kind="evaluation",
                                attempt=1,
                                status="RUNNING",
                                started_at=datetime.now(UTC),
                            )
                        )
                        await session.flush()
                        execution_row = AgentExecution(
                            id=execution_id,
                            run_id=run_id,
                            step_id=step_id,
                            role=case.role.value,
                            instruction_version=loaded_prompt.version,
                            instruction_digest=loaded_prompt.digest,
                            provider=provider,
                            model=model,
                            status="RUNNING",
                            started_at=datetime.now(UTC),
                        )
                        session.add(execution_row)
                        await session.flush()

                        admitted_run_id = run_id
                        admitted_execution_id = execution_id
                        admitted_task_id = task_id
                        admitted_policy = policy

                # Handle settled replay validation (E27-P01)
                if settled_info is not None:
                    # Gateway identity
                    if settled_info["provider"] != provider or settled_info["model"] != model:
                        raise EvaluationConflict(
                            f"gateway identity mismatch on replay for case {case.case_key}: "
                            f"expected ({provider}, {model}), got ({settled_info['provider']}, {settled_info['model']})"
                        )

                    # Prompt version and digest
                    if (
                        settled_info["instruction_version"] != loaded_prompt.version
                        or settled_info["instruction_digest"] != loaded_prompt.digest
                    ):
                        raise EvaluationConflict(
                            f"prompt identity mismatch on replay for case {case.case_key}: "
                            f"expected {loaded_prompt.version}:{loaded_prompt.digest}, "
                            f"got {settled_info['instruction_version']}:{settled_info['instruction_digest']}"
                        )

                    # Fixture content identity and manifest validation via input artifact
                    assert settled_info["input_digest"] is not None
                    input_bytes = await self._artifact_store.open_bytes(
                        settled_info["input_digest"]
                    )
                    stored_input_data = json.loads(input_bytes.decode("utf-8"))

                    # Replay gateway and prompt identity in frozen manifest
                    if (
                        stored_input_data.get("provider") != provider
                        or stored_input_data.get("model") != model
                    ):
                        raise EvaluationConflict(
                            f"gateway identity mismatch on replay for case {case.case_key}"
                        )
                    if (
                        stored_input_data.get("prompt_version") != loaded_prompt.version
                        or stored_input_data.get("prompt_digest") != loaded_prompt.digest
                    ):
                        raise EvaluationConflict(
                            f"prompt identity mismatch on replay for case {case.case_key}"
                        )

                    # Aggregate suite metric version check against database record and frozen manifest
                    if eff_metric_version != settled_info["metric_version"] or (
                        stored_input_data.get("suite_metric_version") is not None
                        and stored_input_data["suite_metric_version"] != eff_metric_version
                    ):
                        raise EvaluationConflict(
                            f"case version mismatch on replay for case {case.case_key}"
                        )

                    # Case fixture version check against database record and frozen manifest
                    if case.fixture_version != settled_info["fixture_version"] or (
                        stored_input_data.get("fixture_version") is not None
                        and stored_input_data["fixture_version"] != case.fixture_version
                    ):
                        raise EvaluationConflict(
                            f"case version mismatch on replay for case {case.case_key}"
                        )

                    # Role-specific case metric version check against frozen manifest
                    stored_case_metric_version = stored_input_data.get(
                        "role_metric_version"
                    ) or stored_input_data.get("metric_version")
                    if (
                        case.metric_version is not None
                        and stored_case_metric_version is not None
                        and case.metric_version != stored_case_metric_version
                    ):
                        raise EvaluationConflict(
                            f"case version mismatch on replay for case {case.case_key}"
                        )

                    # Exact frozen fixture identity check (no legacy compatibility fallback)
                    stored_fid = stored_input_data.get("fixture_identity")
                    if not stored_fid or stored_fid != materialized.fixture_identity:
                        raise EvaluationConflict(
                            f"fixture content identity mismatch on replay for case {case.case_key}"
                        )

                    # Thresholds & regression checking on replayed results
                    replayed_case_ceilings: dict[str, float] = {}
                    replayed_case_floors: dict[str, float] = dict(merged_floors)
                    if case.max_cost_minor is not None:
                        replayed_case_ceilings["estimated_cost_minor"] = float(case.max_cost_minor)
                    if case.max_duration_ms is not None:
                        replayed_case_ceilings["duration_ms"] = float(case.max_duration_ms)
                    for k, v in merged_ceilings.items():
                        if k in replayed_case_ceilings:
                            replayed_case_ceilings[k] = min(replayed_case_ceilings[k], float(v))
                        else:
                            replayed_case_ceilings[k] = float(v)

                    replayed_reg_failures: list[str] = []
                    replayed_passed = settled_info["status"] == "passed"
                    if (
                        eff_baseline_fixture_version is not None
                        or eff_baseline_metric_version is not None
                        or replayed_case_floors
                        or replayed_case_ceilings
                    ):
                        base_fix = eff_baseline_fixture_version or case.fixture_version
                        base_met = eff_baseline_metric_version or eff_metric_version
                        try:
                            reg_fails = regression_failures(
                                fixture_version=case.fixture_version,
                                metric_version=eff_metric_version,
                                baseline_fixture_version=base_fix,
                                baseline_metric_version=base_met,
                                metrics=settled_info["metrics"],
                                floors=replayed_case_floors,
                                ceilings=replayed_case_ceilings,
                            )
                            if reg_fails:
                                replayed_passed = False
                                replayed_reg_failures.extend(reg_fails)
                        except ValueError as reg_err:
                            replayed_passed = False
                            replayed_reg_failures.append(str(reg_err))

                    case_results.append(
                        EvaluationCaseResult(
                            case_key=case.case_key,
                            role=case.role,
                            passed=replayed_passed,
                            status="passed" if replayed_passed else "failed",
                            metrics=settled_info["metrics"],
                            run_id=settled_info["run_id"],
                            execution_id=settled_info["execution_id"],
                            usage_id=settled_info["usage_id"],
                            input_digest=settled_info["input_digest"],
                            output_digest=settled_info["output_digest"],
                            regression_failures=tuple(replayed_reg_failures),
                        )
                    )
                    continue

                # Execute newly admitted case
                assert admitted_run_id is not None
                assert admitted_execution_id is not None
                assert admitted_task_id is not None
                assert admitted_policy is not None
                # Once durable admission exists, preserve the temporary
                # repository unless every later cleanup and settlement succeeds.
                materialized.preserve()

                try:
                    case_result = await self._execute_admitted_case(
                        suite_id=suite_summary.id,
                        case=case,
                        materialized=materialized,
                        run_id=admitted_run_id,
                        execution_id=admitted_execution_id,
                        task_id=admitted_task_id,
                        policy=admitted_policy,
                        gateway=active_gateway,
                        loaded_prompt=loaded_prompt,
                        provider=provider,
                        model=model,
                        metric_version=eff_metric_version,
                        baseline_fixture_version=eff_baseline_fixture_version,
                        baseline_metric_version=eff_baseline_metric_version,
                        floors=merged_floors,
                        ceilings=merged_ceilings,
                        is_live=is_live_mode,
                        has_persisted_baseline=(persisted_baseline is not None),
                        bind_controlled_tools=bind_controlled_tools,
                    )
                except BaseException as exc:
                    settled = await self._settle_fixture_lifecycle(
                        run_id=admitted_run_id,
                        execution_id=admitted_execution_id,
                        policy=admitted_policy,
                        finish_status=(
                            AgentFinishStatus.CANCELLED
                            if isinstance(exc, asyncio.CancelledError)
                            else AgentFinishStatus.FAILED
                        ),
                        requires_teardown=bind_controlled_tools and case.role is not AgentRole.PLANNER,
                    )
                    await self._mark_fixture_case_failed(suite_summary.id, case.case_key)
                    await self._abort_fixture_suite(
                        suite_summary.id, cancelled=isinstance(exc, asyncio.CancelledError)
                    )
                    if settled:
                        materialized.release()
                    raise
                try:
                    settled = await self._settle_fixture_lifecycle(
                        run_id=admitted_run_id,
                        execution_id=admitted_execution_id,
                        policy=admitted_policy,
                        finish_status=(
                            AgentFinishStatus.SUCCEEDED
                            if case_result.passed
                            else AgentFinishStatus.FAILED
                        ),
                        requires_teardown=bind_controlled_tools and case.role is not AgentRole.PLANNER,
                    )
                except asyncio.CancelledError:
                    await self._mark_fixture_case_failed(suite_summary.id, case.case_key)
                    await self._abort_fixture_suite(suite_summary.id, cancelled=True)
                    raise
                if not settled:
                    await self._mark_fixture_case_failed(suite_summary.id, case.case_key)
                    case_result = replace(
                        case_result,
                        passed=False,
                        status="failed",
                        error="fixture_teardown_required",
                    )
                else:
                    materialized.release()
                case_results.append(case_result)

        # Settle the suite
        all_regressions: list[str] = []
        for cr in case_results:
            all_regressions.extend(cr.regression_failures)

        async with self._session_factory() as session, session.begin():
            repo = EvaluationRepository(session)
            final_summary = await repo.finish_suite(suite_summary.id)

        suite_status = final_summary.status
        if (all_regressions or any(not cr.passed for cr in case_results)) and (
            not is_live_mode or has_baseline_selection
        ):
            suite_status = "failed"

        return EvaluationSuiteResult(
            suite_id=final_summary.id,
            name=final_summary.name,
            fixture_version=final_summary.fixture_version,
            metric_version=final_summary.metric_version,
            status=suite_status,
            cases=tuple(case_results),
            regressions=tuple(all_regressions),
        )

    async def _settle_fixture_lifecycle(
        self,
        *,
        run_id: UUID,
        execution_id: UUID,
        policy: ProjectPolicy,
        finish_status: AgentFinishStatus,
        requires_teardown: bool,
    ) -> bool:
        """Close an admitted fixture run only after its managed resources are gone.

        Fixture preparation is admitted by a dedicated operator event.  This
        method deliberately does not use normal delivery approval transitions:
        it only records the terminal evaluation lifecycle or a recovery hold.
        """
        teardown_error: BaseException | None = None
        if requires_teardown:
            try:
                runtime = DeliveryRuntime(
                    self._settings, self._session_factory, self._artifact_store
                )
                await runtime.teardown(run_id, policy)
            except BaseException as exc:  # noqa: BLE001 - cancellation also requires recovery
                teardown_error = exc

        async with self._session_factory() as session, session.begin():
            run = await session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            execution = await session.get(AgentExecution, execution_id, with_for_update=True)
            if run is None or execution is None:
                raise EvaluationConflict("evaluation fixture lifecycle evidence is missing")
            step = await session.scalar(
                select(Step).where(Step.id == execution.step_id).with_for_update()
            )
            if step is None:
                raise EvaluationConflict("evaluation fixture step is missing")

            now = datetime.now(UTC)
            if teardown_error is not None:
                # Do not delete the temporary repository when delivery cannot
                # prove its worktree resources are removed.  The durable event
                # gives an operator the exact run to reconcile.
                suspended = RunState(run.state)
                run.state = RunState.AWAITING_HUMAN_INTERVENTION.value
                run.suspended_state = suspended.value
                run.suspension_kind = "INTERVENTION"
                run.suspension_context = None
                run.suspension_context_schema_version = None
                run.version += 1
                step.status = "FAILED"
                step.completed_at = now
                step.outcome = "fixture teardown requires intervention"
                if execution.status == "RUNNING":
                    execution.status = database_status_for_finish(AgentFinishStatus.FAILED).value
                    execution.completed_at = now
                await PostgresEventRepository(session).append(
                    RunEvent(
                        run_id=run_id,
                        run_version=run.version,
                        event_type="evaluation.fixture_teardown_intervention_required",
                        actor_class="operator",
                        actor_id=None,
                        payload={"reason": type(teardown_error).__name__},
                    )
                )
            else:
                terminal_state = (
                    RunState.COMPLETED
                    if finish_status is AgentFinishStatus.SUCCEEDED
                    else RunState.CANCELLED
                    if finish_status is AgentFinishStatus.CANCELLED
                    else RunState.FAILED
                )
                step.status = (
                    "SUCCEEDED"
                    if finish_status is AgentFinishStatus.SUCCEEDED
                    else "CANCELLED"
                    if finish_status is AgentFinishStatus.CANCELLED
                    else "FAILED"
                )
                step.completed_at = now
                step.outcome = f"evaluation fixture {terminal_state.value.lower()}"
                run.state = terminal_state.value
                run.version += 1
                if execution.status == "RUNNING":
                    execution.status = database_status_for_finish(finish_status).value
                    execution.completed_at = now
                await PostgresEventRepository(session).append(
                    RunEvent(
                        run_id=run_id,
                        run_version=run.version,
                        event_type="evaluation.fixture_lifecycle_settled",
                        actor_class="operator",
                        actor_id=None,
                        payload={"outcome": terminal_state.value.lower()},
                    )
                )
        # Commit recovery evidence before propagating cancellation; do not run
        # another fixture or attempt the same uncertain teardown again.
        if isinstance(teardown_error, asyncio.CancelledError):
            raise teardown_error
        return teardown_error is None

    async def _abort_fixture_suite(self, suite_id: UUID, *, cancelled: bool) -> None:
        """Close interrupted execution without admitting any remaining fixture."""
        async with self._session_factory() as session, session.begin():
            suite = await session.get(EvaluationSuite, suite_id, with_for_update=True)
            if suite is None or suite.status != "running":
                raise EvaluationConflict("evaluation suite cannot abort")
            cases = (await session.scalars(
                select(EvaluationCase).where(EvaluationCase.suite_id == suite_id).with_for_update()
            )).all()
            for case in cases:
                if case.status == "pending":
                    case.status = "skipped"
                    case.completed_at = datetime.now(UTC)
            suite.status = "cancelled" if cancelled else "failed"

    async def _mark_fixture_case_failed(self, suite_id: UUID, case_key: str) -> None:
        """Settle an admitted case when execution never produced reusable evidence."""
        async with self._session_factory() as session, session.begin():
            case = await session.scalar(
                select(EvaluationCase)
                .where(EvaluationCase.suite_id == suite_id, EvaluationCase.case_key == case_key)
                .with_for_update()
            )
            if case is None:
                raise EvaluationConflict("evaluation fixture case is missing")
            case.status = "failed"
            case.metrics = {"lifecycle_failed": 1}
            case.completed_at = datetime.now(UTC)

    async def _execute_admitted_case(
        self,
        *,
        suite_id: UUID,
        case: EvaluationCaseContract,
        materialized: MaterializedFixture,
        run_id: UUID,
        execution_id: UUID,
        task_id: UUID,
        policy: ProjectPolicy,
        gateway: AgentGateway,
        loaded_prompt: Any,
        provider: str,
        model: str,
        metric_version: str,
        baseline_fixture_version: str | None,
        baseline_metric_version: str | None,
        floors: Mapping[str, float] | None,
        ceilings: Mapping[str, float] | None,
        is_live: bool = False,
        has_persisted_baseline: bool = False,
        bind_controlled_tools: bool = False,
    ) -> EvaluationCaseResult:
        # Build typed context (E27-P03)
        context: PlannerInput | DeveloperInput | ReviewerInput
        if case.role == AgentRole.PLANNER:
            tree_text = _read_repository_tree(materialized.path)
            context = PlannerInput(
                original_task=UntrustedContent.from_text(
                    case.task,
                    source_kind=UntrustedSourceKind.TASK,
                    source_reference=str(task_id),
                ),
                base_commit=materialized.base_commit,
                repository_tree=UntrustedContent.from_text(
                    tree_text,
                    source_kind=UntrustedSourceKind.REPOSITORY_TREE,
                    source_reference=".",
                ),
                policy_summary=PolicySummary(
                    policy_id=policy.id,
                    policy_version=policy.version,
                    runner_mode=policy.runner_mode,
                    required_checks=tuple(c.name for c in policy.required_checks),
                ),
            )
        elif case.role == AgentRole.REVIEWER:
            commit_diff = _extract_fixture_commit_diff(materialized.path, materialized.base_commit)
            plan = PlanOutput(
                summary=f"Evaluation plan for {case.case_key}",
                assumptions=(),
                affected_components=tuple(case.expected_components)
                if case.expected_components
                else ("api",),
                steps=("Review code",),
                required_checks=("pytest",),
                risks=("regression",),
                security_considerations=(),
                dependency_changes=(),
            )
            context = ReviewerInput(
                original_task=UntrustedContent.from_text(
                    case.task,
                    source_kind=UntrustedSourceKind.TASK,
                    source_reference=str(task_id),
                ),
                plan=plan,
                current_diff=UntrustedContent.from_text(
                    commit_diff,
                    source_kind=UntrustedSourceKind.DIFF,
                    source_reference=materialized.base_commit,
                ),
            )
        elif case.role == AgentRole.DEVELOPER:
            plan = PlanOutput(
                summary=f"Evaluation plan for {case.case_key}",
                assumptions=(),
                affected_components=tuple(case.expected_components)
                if case.expected_components
                else ("app",),
                steps=("Implement code",),
                required_checks=("pytest",),
                risks=("regression",),
                security_considerations=(),
                dependency_changes=(),
            )
            context = DeveloperInput(
                original_task=UntrustedContent.from_text(
                    case.task,
                    source_kind=UntrustedSourceKind.TASK,
                    source_reference=str(task_id),
                ),
                plan=plan,
                worktree_id=f"eval-worktree-{run_id}",
                base_commit=materialized.base_commit,
            )
        else:
            raise ValueError(f"unsupported role: {case.role}")

        # Store input artifact with fixture_identity and canonical manifest
        input_data = {
            "schema_version": 1,
            "case_key": case.case_key,
            "fixture_identity": materialized.fixture_identity,
            "fixture_version": case.fixture_version,
            "role_metric_version": case.metric_version,
            "metric_version": case.metric_version or metric_version,
            "suite_metric_version": metric_version,
            "prompt_version": loaded_prompt.version,
            "prompt_digest": loaded_prompt.digest,
            "provider": provider,
            "model": model,
            "context": context.model_dump(mode="json"),
        }
        input_bytes = _canonical_json_bytes(input_data)
        input_desc = await self._artifact_store.put_bytes(
            input_bytes, media_type="application/json"
        )
        artifact_repo = ArtifactRepository(self._session_factory)
        input_record = await artifact_repo.record(
            input_desc,
            run_id=run_id,
            producer_type="evaluation_input",
            producer_id=execution_id,
        )

        async with self._session_factory() as session, session.begin():
            exec_to_update = await session.get(AgentExecution, execution_id)
            if exec_to_update is not None:
                exec_to_update.input_artifact_id = input_record.artifact_id

        # Prepare allowed tools
        permitted = _ALLOWED_ROLE_TOOLS.get(case.role, frozenset())
        # ``build_adk_tools`` emits the enum's canonical order.  Keep the
        # admitted request in that same order so the provider can prove it has
        # neither added nor removed a capability before ADK receives it.
        allowed_tools = tuple(
            tool
            for tool in ToolName
            if tool in permitted and tool.value not in case.prohibited_tools
        )

        max_cost = case.max_cost_minor if case.max_cost_minor is not None else 1000
        max_duration = (case.max_duration_ms // 1000) if case.max_duration_ms else 1800
        request = AgentRequest(
            execution_id=execution_id,
            run_id=run_id,
            task_id=task_id,
            role=case.role,
            context=context,
            parent_execution_id=None,
            provider=provider,
            model=model,
            instruction_version=loaded_prompt.version,
            system_instruction=loaded_prompt.instruction,
            instruction_digest=loaded_prompt.digest,
            allowed_tools=allowed_tools,
            budget=AgentBudget(
                max_cost_minor=max_cost,
                max_duration_seconds=max(1, max_duration),
            ),
        )

        # A provider-backed live request receives a real run-scoped managed
        # worktree and the same controlled adapters as delivery.  Test-injected
        # gateways deliberately bypass this path and never consume provider or
        # runner resources.
        if bind_controlled_tools:
            async with self._session_factory() as session:
                execution = await session.get(AgentExecution, execution_id)
                step_id = None if execution is None else execution.step_id
            if step_id is None:
                raise EvaluationConflict("live evaluation execution step is missing")
            uow_factory = cast(
                Callable[[], UnitOfWork],
                lambda: PostgresUnitOfWork(self._session_factory),
            )
            if case.role is AgentRole.PLANNER:
                tools = ControlledToolService(
                    unit_of_work_factory=uow_factory,
                    artifact_store=self._artifact_store,
                    repository_reader=RepositoryReader(
                        root=policy.repository_path, secret_paths=policy.effective_secret_paths
                    ),
                )
                tool_context = ToolAuthorizationContext(
                    role=case.role,
                    run_id=run_id,
                    worktree_id=repository_resource_identity(policy.id),
                    policy_version=policy.version,
                    agent_execution_id=execution_id,
                    step_id=step_id,
                )
            else:
                runtime = DeliveryRuntime(
                    self._settings, self._session_factory, self._artifact_store
                )
                worktree = await runtime.prepare(run_id, policy)
                if case.role is AgentRole.REVIEWER:
                    await self._mark_evaluation_fixture_ready_for_review(run_id)
                async with PostgresUnitOfWork(self._session_factory) as work:
                    prepared_run = await work.runs.get(run_id)
                controlled_git = runtime.git(policy)
                environment = await runtime.environment(prepared_run, policy, worktree)
                tools = ControlledToolService(
                    unit_of_work_factory=uow_factory,
                    artifact_store=self._artifact_store,
                    repository_reader=runtime.reader(policy, worktree),
                    repository_writer=runtime.writer(
                        policy, worktree, controlled_git=controlled_git
                    )
                    if case.role is AgentRole.DEVELOPER
                    else None,
                    controlled_git=controlled_git,
                    operation_executor=runtime.operation_executor,
                    worktree=worktree,
                    runner_factory=runtime if case.role is AgentRole.DEVELOPER else None,
                    command_environment=environment,
                )
                tool_context = ToolAuthorizationContext(
                    role=case.role,
                    run_id=run_id,
                    worktree_id=worktree.identity.worktree_name,
                    policy_version=policy.version,
                    agent_execution_id=execution_id,
                    step_id=step_id,
                )
            self._evaluation_tool_registry.register(
                request,
                tools,
                tool_context,
                EvaluationToolObserver(case=case, artifact_store=self._artifact_store),
            )

        # Execute Gateway and validate result (E27-P04)
        gateway_exception: Exception | None = None
        live_changed_paths: tuple[str, ...] | None = None
        raw_result: object = None
        try:
            raw_result = await gateway.execute(request)
            if bind_controlled_tools and case.role is AgentRole.DEVELOPER:
                live_changed_paths = controlled_git.changed_paths(worktree, policy)
        except Exception as exc:  # noqa: BLE001
            gateway_exception = exc

        extracted_output: PlanOutput | DeveloperOutput | ReviewOutput | None = None
        if gateway_exception is not None:
            finish_status = AgentFinishStatus.FAILED
            usage_to_save = safe_usage(None, request)
            output_payload: dict[str, Any] = {
                "error": "gateway_execution_failed",
                "error_type": type(gateway_exception).__name__,
            }
            error_reason: str | None = f"gateway_{type(gateway_exception).__name__.lower()}"
        else:
            finish_status, usage_to_save, _, error_reason = validate_agent_result(
                raw_result,
                request,
                expected_role=case.role,
            )
            if finish_status != AgentFinishStatus.SUCCEEDED:
                output_payload = {
                    "error": error_reason or finish_status.value,
                }
            else:
                assert isinstance(raw_result, AgentResult)
                assert raw_result.output is not None
                output_payload = raw_result.output.model_dump(mode="json")
                extracted_output = raw_result.output

        output_bytes = _canonical_json_bytes(output_payload)
        output_desc = await self._artifact_store.put_bytes(
            output_bytes, media_type="application/json"
        )
        output_record = await artifact_repo.record(
            output_desc,
            run_id=run_id,
            producer_type="agent_execution",
            producer_id=execution_id,
            parent_digests=(input_desc.digest,),
        )

        # Price usage if catalog is available (E27-P04)
        if self._pricing_catalog is not None and usage_to_save.pricing_version == "unavailable-v1":
            usage_to_save = self._pricing_catalog.price(usage_to_save, currency="USD")
        usage_id = usage_to_save.id or uuid4()

        # Score the output
        scores_dict: dict[str, int | float | bool | None] = {}
        passed = finish_status == AgentFinishStatus.SUCCEEDED
        denied_tool_calls_observed: Sequence[str] = ()

        if case.role == AgentRole.PLANNER:
            actual_plan = extracted_output if isinstance(extracted_output, PlanOutput) else None
            plan_scores = score_plan(
                expected_components=set(case.expected_components),
                expected_checks=set(case.expected_checks),
                expected_risks=set(case.expected_risks),
                expected_dependencies=set(case.expected_dependencies),
                actual=actual_plan,
                denied_tool_calls=(),
            )
            scores_dict.update(
                {
                    "component_recall": plan_scores.component_recall,
                    "check_recall": plan_scores.check_recall,
                    "risk_recall": plan_scores.risk_recall,
                    "dependency_disclosure": plan_scores.dependency_disclosure,
                    "policy_compliance": plan_scores.policy_compliance,
                    "schema_validity": plan_scores.schema_validity,
                }
            )
            if (
                plan_scores.schema_validity < 1.0
                or plan_scores.policy_compliance < 1.0
                or plan_scores.component_recall < 1.0
                or plan_scores.check_recall < 1.0
                or plan_scores.risk_recall < 1.0
                or (case.expected_dependencies and plan_scores.dependency_disclosure < 1.0)
            ):
                passed = False

        elif case.role == AgentRole.REVIEWER:
            findings = (
                extracted_output.findings if isinstance(extracted_output, ReviewOutput) else None
            )
            review_scores = score_review(
                seeded_defects=case.expected_defects,
                findings=findings,
                denied_tool_calls=(),
            )
            scores_dict.update(
                {
                    "defect_recall": review_scores.defect_recall,
                    "blocker_recall": review_scores.blocker_recall,
                    "major_recall": review_scores.major_recall,
                    "minor_recall": review_scores.minor_recall,
                    "suggestion_recall": review_scores.suggestion_recall,
                    "false_positive_count": review_scores.false_positive_count,
                    "blocker_false_positive_count": review_scores.blocker_false_positive_count,
                    "evidence_quality": review_scores.evidence_quality,
                    "missing_test_recall": review_scores.missing_test_recall,
                    "policy_compliance": review_scores.policy_compliance,
                    "schema_validity": review_scores.schema_validity,
                }
            )
            if (
                review_scores.schema_validity < 1.0
                or review_scores.policy_compliance < 1.0
                or review_scores.defect_recall < 1.0
                or review_scores.blocker_false_positive_count > 0
                or review_scores.evidence_quality < 1.0
            ):
                passed = False

        elif case.role == AgentRole.DEVELOPER:
            actual_dev = extracted_output if isinstance(extracted_output, DeveloperOutput) else None
            observer = getattr(getattr(gateway, "_tool_provider", None), "observer", None)
            if not isinstance(observer, EvaluationToolObserver):
                registry_observer = self._evaluation_tool_registry.observer_for(execution_id)
                observer = (
                    self._deterministic_developer_observer(case)
                    if isinstance(gateway, FakeAgentGateway)
                    else registry_observer
                    if isinstance(registry_observer, EvaluationToolObserver)
                    else None
                )
            if isinstance(observer, EvaluationToolObserver) and live_changed_paths is not None:
                observer.changed_paths = set(live_changed_paths)
            observation = observe_developer_execution(materialized, case, observer=observer)
            denied_tool_calls_observed = observation.denied_tool_calls
            dev_scores = score_development(
                actual=actual_dev,
                changed_paths=observation.changed_paths,
                allowed_paths=set(case.allowed_paths),
                required_tests=set(case.required_tests),
                test_results=dict(observation.test_results),
                required_checks=set(case.required_checks),
                check_results=dict(observation.check_results),
                required_assertions=set(case.required_assertions),
                assertion_results=dict(observation.assertion_results),
                denied_tool_calls=observation.denied_tool_calls,
                remediation_count=observation.remediation_count,
            )
            scores_dict.update(
                {
                    "required_test_pass": dev_scores.required_test_pass,
                    "diff_scope_precision": dev_scores.diff_scope_precision,
                    "named_check_success": dev_scores.named_check_success,
                    "policy_compliance": dev_scores.policy_compliance,
                    "remediation_count": dev_scores.remediation_count,
                    "task_assertion_pass": dev_scores.task_assertion_pass,
                    "schema_validity": dev_scores.schema_validity,
                }
            )
            if (
                dev_scores.schema_validity < 1.0
                or dev_scores.policy_compliance < 1.0
                or (case.required_tests and dev_scores.required_test_pass < 1.0)
                or (case.allowed_paths and dev_scores.diff_scope_precision < 1.0)
                or (case.required_checks and dev_scores.named_check_success < 1.0)
                or (case.required_assertions and dev_scores.task_assertion_pass < 1.0)
            ):
                passed = False

        # Usage scores
        common_scores = score_usage(usage_to_save, denied_tool_calls=denied_tool_calls_observed)
        scores_dict.update(
            {
                "input_tokens": common_scores.input_tokens,
                "output_tokens": common_scores.output_tokens,
                "cached_input_tokens": common_scores.cached_input_tokens,
                "total_tokens": common_scores.total_tokens,
                "duration_ms": common_scores.duration_ms,
                "tool_count": common_scores.tool_count,
                "denied_calls": common_scores.denied_calls,
            }
        )
        if common_scores.estimated_cost_minor is not None:
            scores_dict["estimated_cost_minor"] = common_scores.estimated_cost_minor

        # Budget ceilings & regression checking
        case_ceilings: dict[str, float] = {}
        case_floors: dict[str, float] = dict(floors or {})
        if case.max_cost_minor is not None:
            case_ceilings["estimated_cost_minor"] = float(case.max_cost_minor)
        if case.max_duration_ms is not None:
            case_ceilings["duration_ms"] = float(case.max_duration_ms)
        if ceilings:
            for k, v in ceilings.items():
                if k in case_ceilings:
                    case_ceilings[k] = min(case_ceilings[k], float(v))
                else:
                    case_ceilings[k] = float(v)

        regression_errors: list[str] = []
        if (
            baseline_fixture_version is not None
            or baseline_metric_version is not None
            or case_floors
            or case_ceilings
        ):
            base_fix = baseline_fixture_version or case.fixture_version
            base_met = baseline_metric_version or metric_version
            try:
                reg_fails = regression_failures(
                    fixture_version=case.fixture_version,
                    metric_version=metric_version,
                    baseline_fixture_version=base_fix,
                    baseline_metric_version=base_met,
                    metrics=scores_dict,
                    floors=case_floors,
                    ceilings=case_ceilings,
                )
                if reg_fails:
                    if not is_live or has_persisted_baseline:
                        passed = False
                    regression_errors.extend(reg_fails)
            except ValueError as reg_err:
                if not is_live or has_persisted_baseline:
                    passed = False
                regression_errors.append(str(reg_err))

        # Durably finalize together: execution status, priced usage, and case settlement (E27-P04, E27-P05)
        async with self._session_factory() as session, session.begin():
            exec_to_finalize = await session.get(AgentExecution, execution_id)
            if exec_to_finalize is not None:
                exec_to_finalize.status = database_status_for_finish(finish_status).value
                exec_to_finalize.output_artifact_id = output_record.artifact_id
                exec_to_finalize.completed_at = datetime.now(UTC)

            usage_record = ModelUsage(
                id=usage_id,
                run_id=run_id,
                agent_execution_id=execution_id,
                provider=usage_to_save.provider,
                model=usage_to_save.model,
                prompt_version=usage_to_save.prompt_version,
                input_tokens=usage_to_save.input_tokens,
                output_tokens=usage_to_save.output_tokens,
                cached_input_tokens=usage_to_save.cached_input_tokens,
                duration_ms=usage_to_save.duration_ms,
                tool_call_count=usage_to_save.tool_call_count,
                provider_request_id=usage_to_save.provider_request_id,
                pricing_version=usage_to_save.pricing_version,
                estimated_cost_minor=usage_to_save.estimated_cost_minor,
                currency=usage_to_save.currency,
                unknown_price_reason=usage_to_save.unknown_price_reason,
            )
            session.add(usage_record)
            await session.flush()

            repo = EvaluationRepository(session)
            await repo.record_case(
                suite_id=suite_id,
                case_key=case.case_key,
                run_id=run_id,
                usage_id=usage_id,
                input_digest=input_desc.digest,
                output_digest=output_desc.digest,
                passed=passed,
                metrics=scores_dict,
            )

        return EvaluationCaseResult(
            case_key=case.case_key,
            role=case.role,
            passed=passed,
            status="passed" if passed else "failed",
            metrics=scores_dict,
            run_id=run_id,
            execution_id=execution_id,
            usage_id=usage_id,
            input_digest=input_desc.digest,
            output_digest=output_desc.digest,
            regression_failures=tuple(regression_errors),
            error=error_reason,
        )

    async def promote_baseline(
        self,
        suite_id: UUID,
        name: str = "live",
        *,
        floors: Mapping[str, float] | None = None,
        ceilings: Mapping[str, float] | None = None,
        promoted_by: str = "operator",
    ) -> EvaluationBaseline:
        async with self._session_factory() as session, session.begin():
            repo = EvaluationRepository(session)
            return await repo.promote_baseline(
                suite_id=suite_id,
                name=name,
                floors=floors,
                ceilings=ceilings,
                promoted_by=promoted_by,
            )


__all__ = ["EvaluationService"]
