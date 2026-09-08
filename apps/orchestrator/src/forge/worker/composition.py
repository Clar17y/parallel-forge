"""Validated production dependency composition for the Forge worker."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.agents.adk_gateway import BoundAdkTools, GoogleAdkGateway
from forge.agents.adk_runtime import AdkRuntime, AdkRuntimeError, AdkRuntimeProtocol
from forge.agents.errors import AgentGatewayError
from forge.agents.prompt_loader import PromptLoader, PromptLoadError
from forge.agents.tool_bridge import build_adk_tools
from forge.application.adapters.git import LocalGitRepositoryInspector
from forge.application.handlers.approvals import (
    ApprovePlanHandler,
    RequestPlanRevisionHandler,
)
from forge.application.handlers.delivery import ReviewHandler
from forge.application.handlers.merge import ApproveMergeHandler
from forge.application.handlers.planning import PlanningHandler
from forge.application.handlers.release import ApprovePrHandler
from forge.application.handlers.run_controls import (
    CancelRunHandler,
    PauseRunHandler,
    ResumeRunHandler,
)
from forge.application.ports.agents import AgentGateway
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.base_adoption import BaseAdoptionPort
from forge.application.ports.clock import Clock
from forge.application.ports.executions import ExecutionAdmission, ExecutionStatus
from forge.application.ports.git_push import ManagedPushPort
from forge.application.ports.github import GitHubPort
from forge.application.ports.github_write import GitHubWritePort
from forge.application.ports.operations import OperationAdapter
from forge.application.ports.provider_credentials import (
    ProviderCredentialError,
    validate_provider_secret_reference,
)
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.approved_plan import ApprovedPlan, ApprovedPlanLoader
from forge.application.services.base_update import BaseUpdateService
from forge.application.services.candidate_revision import CandidateRevisionService
from forge.application.services.delivery import DeliveryService
from forge.application.services.delivery_preparation import DeliveryPreparationService
from forge.application.services.development import DevelopmentService
from forge.application.services.merge import MergeService
from forge.application.services.merge_evidence import MergeEvidenceValidator
from forge.application.services.plan_evidence import (
    PlanEvidenceValidator,
)
from forge.application.services.planning import PlanningService
from forge.application.services.pr_evidence import PrEvidenceValidator
from forge.application.services.release import ReleaseService
from forge.application.services.release_monitor import ReleaseMonitor
from forge.application.services.review import ReviewService
from forge.application.services.review_decision import ReviewDecisionService
from forge.application.services.tool_recovery import ToolRecoveryService
from forge.application.services.tools import ControlledToolService
from forge.application.services.validation import ValidationService
from forge.application.services.worker import CommandHandler
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentBudget, AgentRequest, AgentResult, PlannerInput, PolicySummary
from forge.domain.command import CommandEnvelope
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunState
from forge.domain.tool import (
    ToolAuthorizationContext,
    ToolName,
    repository_resource_identity,
)
from forge.observability.redaction import Redactor
from forge.observability.usage import PricingCatalog
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.release.credentials import LocalGitHubCredentialResolver
from forge.release.git_adoption import ManagedBaseAdoption
from forge.release.git_push import ManagedPush
from forge.release.github_client import GitHubClient
from forge.release.github_write import GitHubWrite
from forge.release.merge import MergeController
from forge.settings import Settings
from forge.tools.git import ControlledGit
from forge.tools.provider_credentials import LocalProviderCredentialResolver
from forge.tools.repository import RepositoryReader
from forge.tools.secrets import LocalSecretStore, SecretStoreError
from forge.worker.agent_tools import PerRequestToolProvider as _PerRequestToolProvider
from forge.worker.base_recovery import base_recovery_adapters
from forge.worker.bound_delivery import BoundDeliveryGateway
from forge.worker.delivery_runtime import DeliveryRuntime
from forge.worker.publication_recovery import publication_recovery_adapters
from forge.worker.recovery_adapters import local_recovery_adapters
from forge.worker.release_recovery import merge_recovery_adapters
from forge.worker.resource_recovery import resource_recovery_adapters


class WorkerCompositionError(RuntimeError):
    """Context-free failure when composing worker dependencies."""

    def __init__(self, message: str = "worker configuration is invalid") -> None:
        super().__init__(message)


class WorkerHandlers(dict[str, CommandHandler]):
    """Command dispatch plus resources owned by production composition."""

    def __init__(self, handlers: Mapping[str, CommandHandler]) -> None:
        super().__init__(handlers)
        self.resources = AsyncExitStack()
        self.recovery_adapters: dict[str, OperationAdapter] = {}
        self.tool_recovery: ToolRecoveryService | None = None

    async def aclose(self) -> None:
        await self.resources.aclose()


@dataclass(frozen=True)
class ReleaseDependencies:
    """Caller-owned release ports for deterministic worker execution."""

    read: GitHubPort
    writes: GitHubWritePort
    push: Callable[[ProjectPolicy], ManagedPushPort]
    adoption: Callable[[ProjectPolicy], BaseAdoptionPort] | None = None


async def _release_unconfigured(command: CommandEnvelope, work: UnitOfWork) -> None:
    raise WorkerCompositionError("GitHub credential reference is not configured")


async def _adoption_unconfigured(command: CommandEnvelope, work: UnitOfWork) -> None:
    raise WorkerCompositionError("base adoption runtime is not configured")


def load_pricing_catalog(path: Path | str) -> PricingCatalog:
    """Load and validate an operator-supplied versioned pricing catalog from JSON."""
    try:
        catalog_path = Path(path)
        content = catalog_path.read_text(encoding="utf-8")
        data = json.loads(content)
        if not isinstance(data, dict):
            raise TypeError
        version = data.get("version")
        entries = data.get("entries")
        if not isinstance(version, str) or not isinstance(entries, dict):
            raise TypeError
        if not entries:
            raise ValueError

        # Require cache/input/output prices for configured models before effect.
        for raw_entry in entries.values():
            if not isinstance(raw_entry, dict):
                raise TypeError
            if not {
                "input_per_million",
                "output_per_million",
                "cached_input_per_million",
            }.issubset(raw_entry):
                raise ValueError

        exponents = data.get("currency_minor_exponents")
        if exponents is not None and not isinstance(exponents, dict):
            raise TypeError

        return PricingCatalog.from_mapping(
            version=version,
            entries=entries,
            currency_minor_exponents=exponents,
        )
    except OSError, json.JSONDecodeError, ValueError, TypeError, KeyError:
        raise WorkerCompositionError("pricing catalog is invalid") from None


class BoundPlanningGateway:
    """Derive durable request bindings in a short UoW, then invoke gateway outside DB tx."""

    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], Any],
        artifact_store: ArtifactStore,
        prompt_loader: PromptLoader,
        redactor: Redactor,
        runtime: AdkRuntimeProtocol | None = None,
        pricing_catalog: PricingCatalog | None = None,
        supported_provider: str = "google",
        currency: str = "USD",
        underlying_gateway_factory: (
            Callable[[_PerRequestToolProvider], AgentGateway] | None
        ) = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._artifact_store = artifact_store
        self._prompt_loader = prompt_loader
        self._redactor = redactor
        self._runtime = runtime
        self._pricing_catalog = pricing_catalog
        self._supported_provider = supported_provider
        self._currency = currency
        self._underlying_gateway_factory = underlying_gateway_factory

    async def execute(self, request: AgentRequest) -> AgentResult:
        if type(request) is not AgentRequest:
            raise AgentGatewayError("invalid agent request")
        if request.role is not AgentRole.PLANNER:
            raise AgentGatewayError("bound planning gateway only supports planner role")

        # Derive durable rows using UoW in short transaction
        async with self._unit_of_work_factory() as uow:
            run = await uow.runs.get(request.run_id)
            if run is None or run.task_id != request.task_id or run.state is not RunState.PLANNING:
                raise AgentGatewayError()

            execution = await uow.executions.get_admission(request.run_id, request.execution_id)
            if (
                type(execution) is not ExecutionAdmission
                or execution.run_id != request.run_id
                or execution.agent_execution_id != request.execution_id
                or execution.kind != "plan"
                or execution.role is not request.role
                or execution.status is not ExecutionStatus.RUNNING
                or execution.instruction_version != request.instruction_version
                or execution.provider != request.provider
                or execution.model != request.model
            ):
                raise AgentGatewayError()

            if run.policy_version is None:
                raise AgentGatewayError("run policy version is missing")

            policy_record = await uow.projects.get_policy(run.project_id, run.policy_version)
            if policy_record is None:
                raise AgentGatewayError("policy record not found")
            policy = ProjectPolicy.model_validate(policy_record.document)
            if policy.id != run.project_id or policy.version != run.policy_version:
                raise AgentGatewayError("policy mismatch")

            if (
                policy.planner_model.provider != request.provider
                or policy.planner_model.model != request.model
                or request.budget != AgentBudget.from_model_policy(policy.planner_model)
                or type(request.context) is not PlannerInput
                or request.context.policy_summary != PolicySummary.from_policy(policy)
                or request.context.base_commit != run.base_sha
            ):
                raise AgentGatewayError("policy planner model mismatch")

            step_id = execution.step_id
            project_id = run.project_id
            policy_version = run.policy_version
            repo_path = policy.repository_path
            secret_paths = policy.effective_secret_paths

        # Outside DB transaction: construct per-request bound tools
        resource_id = repository_resource_identity(project_id)
        tool_context = ToolAuthorizationContext(
            role=request.role,
            run_id=request.run_id,
            worktree_id=resource_id,
            policy_version=policy_version,
            agent_execution_id=request.execution_id,
            step_id=step_id,
        )
        reader = RepositoryReader(
            root=repo_path,
            secret_paths=secret_paths,
        )
        tool_service = ControlledToolService(
            unit_of_work_factory=self._unit_of_work_factory,
            artifact_store=self._artifact_store,
            repository_reader=reader,
            redactor=self._redactor,
        )
        adk_tools = build_adk_tools(tool_service, tool_context)
        tool_names = tuple(ToolName(t.name) for t in adk_tools)
        if tool_names != request.allowed_tools:
            raise AgentGatewayError("bound tools mismatch role allowed tools")
        bound_tools = BoundAdkTools(names=tool_names, tools=adk_tools)

        tool_provider = _PerRequestToolProvider(bound_tools, request)

        if self._underlying_gateway_factory is not None:
            gateway = self._underlying_gateway_factory(tool_provider)
        else:
            if self._runtime is None or self._pricing_catalog is None:
                raise AgentGatewayError("gateway runtime or pricing catalog not configured")
            gateway = GoogleAdkGateway(
                runtime=self._runtime,
                prompt_loader=self._prompt_loader,
                tool_provider=tool_provider,
                pricing_catalog=self._pricing_catalog,
                supported_provider=self._supported_provider,
                currency=self._currency,
            )

        return await gateway.execute(request)


def compose_worker_handlers(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    agent_gateway: AgentGateway | None = None,
    redactor: Redactor | None = None,
    clock: Clock | None = None,
    repository_inspector: LocalGitRepositoryInspector | None = None,
    delivery_runtime: DeliveryRuntime | None = None,
    release_dependencies: ReleaseDependencies | None = None,
) -> WorkerHandlers:
    """Compose production worker command handlers with verified dependencies."""
    shared_redactor = redactor or Redactor()
    artifact_store = FilesystemArtifactStore(settings.artifact_root)

    prompt_root = settings.prompt_root or Path("agents")
    try:
        prompt_loader = PromptLoader(prompt_root)
    except PromptLoadError, TypeError, OSError:
        raise WorkerCompositionError("prompt loader root is unavailable") from None

    # The concrete repositories narrow writable protocol attributes; the UoW
    # still exposes the complete service port used by these production adapters.
    uow_factory = lambda: cast(
        UnitOfWork, PostgresUnitOfWork(session_factory, redactor=shared_redactor)
    )

    if agent_gateway is None:
        if not settings.effective_provider_secret_reference:
            raise WorkerCompositionError("provider secret reference is not configured")
        try:
            validate_provider_secret_reference(settings.effective_provider_secret_reference)
        except ProviderCredentialError, ValueError, TypeError:
            raise WorkerCompositionError("provider secret reference is invalid") from None

        if settings.pricing_catalog_path is None:
            raise WorkerCompositionError("pricing catalog path is not configured")
        pricing_catalog = load_pricing_catalog(settings.pricing_catalog_path)

        try:
            secret_store = LocalSecretStore(settings.data_root)
        except SecretStoreError, OSError, ValueError, TypeError:
            raise WorkerCompositionError("secret store initialization failed") from None
        credential_resolver = LocalProviderCredentialResolver(secret_store)
        try:
            runtime = AdkRuntime(
                credential_resolver=credential_resolver,
                credential_reference=settings.effective_provider_secret_reference,
            )
        except AdkRuntimeError, ProviderCredentialError, ValueError, TypeError:
            raise WorkerCompositionError("adk runtime initialization failed") from None

        resolved_gateway: AgentGateway = BoundPlanningGateway(
            unit_of_work_factory=uow_factory,
            artifact_store=artifact_store,
            prompt_loader=prompt_loader,
            redactor=shared_redactor,
            runtime=runtime,
            pricing_catalog=pricing_catalog,
        )
    else:
        resolved_gateway = agent_gateway

    delivery_dependencies = delivery_runtime or DeliveryRuntime(
        settings, session_factory, artifact_store, shared_redactor, clock
    )
    delivery_gateway = resolved_gateway
    if agent_gateway is None:

        async def delivery_tools(
            request: AgentRequest,
            approved: ApprovedPlan,
            tree: ManagedWorktree,
        ) -> ControlledToolService:
            policy = approved.policy
            git = delivery_dependencies.git(policy)
            developer = request.role is AgentRole.DEVELOPER
            environment = (
                await delivery_dependencies.environment(approved.run, policy, tree)
                if developer
                else {}
            )
            return ControlledToolService(
                unit_of_work_factory=uow_factory,
                artifact_store=artifact_store,
                repository_reader=delivery_dependencies.reader(policy, tree),
                repository_writer=(
                    delivery_dependencies.writer(policy, tree, controlled_git=git)
                    if developer
                    else None
                ),
                controlled_git=git,
                operation_executor=delivery_dependencies.operation_executor,
                redactor=shared_redactor,
                worktree=tree,
                runner_factory=delivery_dependencies if developer else None,
                command_environment=environment,
            )

        def delivery_provider(tools: _PerRequestToolProvider) -> AgentGateway:
            return GoogleAdkGateway(
                runtime=runtime,
                prompt_loader=prompt_loader,
                tool_provider=tools,
                pricing_catalog=pricing_catalog,
                supported_provider="google",
                currency="USD",
            )

        delivery_gateway = BoundDeliveryGateway(
            unit_of_work_factory=uow_factory,
            artifact_store=artifact_store,
            prompt_loader=prompt_loader,
            git_factory=delivery_dependencies.git,
            tool_service_factory=delivery_tools,
            gateway_factory=delivery_provider,
        )

    def make_repository_reader(policy: ProjectPolicy) -> RepositoryReader:
        return RepositoryReader(
            root=policy.repository_path,
            secret_paths=policy.effective_secret_paths,
        )

    planning_service = PlanningService(
        agent_gateway=resolved_gateway,
        artifact_store=artifact_store,
        prompt_loader=prompt_loader,
        repository_reader_factory=make_repository_reader,
        clock=clock,
    )
    start_planning_handler = PlanningHandler(planning_service)

    inspector = repository_inspector or LocalGitRepositoryInspector()
    validator = PlanEvidenceValidator(
        artifact_store,
        inspector,
        data_root=str(settings.data_root),
    )
    approve_plan_handler = ApprovePlanHandler(validator, clock=clock)
    request_plan_revision_handler = RequestPlanRevisionHandler(
        artifact_store, validator, clock=clock
    )

    approved_plans = ApprovedPlanLoader(artifact_store)
    preparation = DeliveryPreparationService(approved_plans, delivery_dependencies)
    development = DevelopmentService(
        delivery_gateway,
        artifact_store,
        prompt_loader,
        approved_plans,
        delivery_dependencies.git,
        delivery_dependencies.reader,
    )
    validation = ValidationService(
        artifact_store,
        uow_factory=uow_factory,
        operation_executor=delivery_dependencies.operation_executor,
        git_factory=delivery_dependencies.git,
        runner_factory=delivery_dependencies,
        environment_resolver=delivery_dependencies.environment,
        approved_plans=approved_plans,
    )
    delivery = DeliveryService(
        artifact_store, validation=validation, git_factory=delivery_dependencies.git
    )
    review = ReviewService(
        delivery_gateway,
        artifact_store,
        prompt_loader,
        approved_plans,
        delivery_dependencies.git,
        delivery_dependencies.reader,
    )
    review_decision = ReviewDecisionService(
        artifact_store, git_factory=delivery_dependencies.git, approved_plans=approved_plans
    )
    candidate_revision = CandidateRevisionService(
        artifact_store, approved_plans, delivery_dependencies.git, clock=clock
    )

    handlers = WorkerHandlers(
        {
            "start_planning": start_planning_handler,
            "approve_plan": approve_plan_handler,
            "request_plan_revision": request_plan_revision_handler,
            "prepare_worktree": preparation.execute,
            "implement": development.execute,
            "remediate": development.execute,
            "remediate_remote": development.execute,
            "validate": delivery.validate,
            "review": ReviewHandler(review, review_decision),
            "pause": PauseRunHandler(),
            "resume": ResumeRunHandler(
                artifact_store=artifact_store, preparation_inspector=delivery_dependencies
            ),
            "cancel": CancelRunHandler(),
            "request_candidate_changes": candidate_revision.execute,
        }
    )
    release_commands = (
        "update_base",
        "approve_pr",
        "publish_pr",
        "monitor_pr",
        "push_reviewed_pr",
        "approve_merge",
        "merge_pr",
    )
    handlers.tool_recovery = ToolRecoveryService(
        uow_factory, artifact_store, redactor=shared_redactor
    )
    handlers.recovery_adapters.update(resource_recovery_adapters(session_factory, delivery_dependencies))

    handlers.recovery_adapters.update(
        local_recovery_adapters(
            session_factory,
            artifact_store,
            delivery_dependencies.git,
            lambda policy, worktree, git: delivery_dependencies.writer(
                policy, worktree, controlled_git=cast(ControlledGit, git)
            ),
        )
    )
    if release_dependencies is None:
        if not settings.github_token_reference:
            handlers.update({name: _release_unconfigured for name in release_commands})
            return handlers
        credentials = LocalGitHubCredentialResolver(LocalSecretStore(settings.data_root))
        read = GitHubClient(credentials, settings.github_token_reference)
        writes = GitHubWrite(credentials, settings.github_token_reference)
        handlers.resources.push_async_callback(read.aclose)
        handlers.resources.push_async_callback(writes.aclose)
        release_dependencies = ReleaseDependencies(
            read,
            writes,
            lambda policy: ManagedPush(
                delivery_dependencies.git(policy), credentials, settings.github_token_reference
            ),
            lambda policy: ManagedBaseAdoption(
                delivery_dependencies.git(policy), credentials, settings.github_token_reference
            ),
        )
    pr_evidence = PrEvidenceValidator(
        artifact_store, approved_plans, delivery_dependencies.git, release_dependencies.read
    )
    handlers.recovery_adapters.update(
        publication_recovery_adapters(session_factory, pr_evidence, release_dependencies.writes)
    )
    release = ReleaseService(
        pr_evidence,
        release_dependencies.writes,
        release_dependencies.push,
        delivery_dependencies.operation_executor,
        clock=clock,
    )
    merge_controller = MergeController(release_dependencies.read, release_dependencies.writes)
    if release_dependencies.adoption is not None:
        handlers.recovery_adapters.update(base_recovery_adapters(
            session_factory, artifact_store, pr_evidence, release_dependencies.read,
            release_dependencies.writes, release_dependencies.adoption,
        ))
    merge_evidence = MergeEvidenceValidator(artifact_store, pr_evidence, merge_controller)
    handlers.recovery_adapters.update(
        merge_recovery_adapters(session_factory, merge_evidence, merge_controller)
    )
    merge = MergeService(
        merge_evidence, merge_controller, delivery_dependencies.operation_executor, clock=clock
    )
    handlers.update(
        {
            "update_base": BaseUpdateService(
                artifact_store,
                pr_evidence,
                release_dependencies.read,
                release_dependencies.writes,
                release_dependencies.adoption,
                delivery_dependencies.operation_executor,
                clock=clock,
            ).execute
            if release_dependencies.adoption is not None
            else _adoption_unconfigured,
            "approve_pr": ApprovePrHandler(pr_evidence, approved_plans, clock=clock),
            "publish_pr": release.publish,
            "monitor_pr": ReleaseMonitor(
                artifact_store,
                pr_evidence,
                release_dependencies.read,
                release_dependencies.writes,
                clock=clock,
            ),
            "push_reviewed_pr": release.push_reviewed,
            "approve_merge": ApproveMergeHandler(merge_evidence, clock=clock),
            "merge_pr": merge.execute,
        }
    )
    return handlers


__all__ = [
    "BoundPlanningGateway",
    "WorkerCompositionError",
    "compose_worker_handlers",
    "load_pricing_catalog",
]
