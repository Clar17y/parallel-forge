"""Resolve controlled adapters from durable run policy, never provider context."""

from collections.abc import Callable, Mapping
from pathlib import Path

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.subscription_execution import SubscriptionAdmission
from forge.application.ports.subscription_gateway import SubscriptionInvocationRequest
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.tools import ControlledToolService
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunState
from forge.domain.subscription import SpecialistPurpose
from forge.domain.subscription_execution import SUBSCRIPTION_WORK_STATES
from forge.domain.tool import ToolName, repository_resource_identity
from forge.observability.redaction import Redactor
from forge.ranking.configuration import SearchRankingConfiguration
from forge.tools.repository import RepositoryReader
from forge.worker.delivery_runtime import DeliveryRuntime


def _subscription_search_objective(request: SubscriptionInvocationRequest) -> str | None:
    """Return the durable human task text, used only to rank search results."""

    context = request.untrusted_context
    if isinstance(context, Mapping):
        task_info = context.get("task")
        if isinstance(task_info, Mapping):
            text = task_info.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


_READS = frozenset(
    {
        ToolName.REPOSITORY_LIST_FILES,
        ToolName.REPOSITORY_READ_FILE,
        ToolName.REPOSITORY_SEARCH,
        ToolName.REPOSITORY_READ_INSTRUCTIONS,
    }
)
_WRITES = frozenset(
    {
        ToolName.REPOSITORY_WRITE_FILE,
        ToolName.REPOSITORY_DELETE_FILE,
        ToolName.REPOSITORY_RENAME_FILE,
    }
)


class SubscriptionToolServiceFactory:
    """Bind adapters lazily; broker and controlled service authorize each effect."""

    def __init__(
        self,
        work_factory: Callable[[], UnitOfWork],
        *,
        artifacts: ArtifactStore,
        delivery: DeliveryRuntime,
        redactor: Redactor | None = None,
        search_ranking: SearchRankingConfiguration | None = None,
    ) -> None:
        self._factory, self._artifacts = work_factory, artifacts
        self._delivery, self._redactor = delivery, redactor
        self._search_ranking = (
            search_ranking if search_ranking is not None else SearchRankingConfiguration()
        )

    async def __call__(
        self,
        admission: SubscriptionAdmission,
        request: SubscriptionInvocationRequest,
    ) -> ControlledToolService:
        if (
            request.task != admission.task
            or request.attempt != admission.attempt
            or request.envelope != admission.envelope
            or request.run_state is None
            or request.attempt_budget is None
        ):
            raise ValueError("subscription tools request differs from admission")
        authority = request.authorization
        # This runs after broker effect admission. Do not repeat invocation_context:
        # that check correctly rejects outstanding effects before provider launch.
        async with self._factory() as work:
            run = await work.runs.get_for_update(admission.attempt.run_id)
            project = await work.projects.get(run.project_id)
            if (
                (run.state is not RunState.PLANNING and run.state not in SUBSCRIPTION_WORK_STATES)
                or run.state is not request.run_state
                or run.pending_gate is not None
                or run.policy_version != authority.policy_version
                or project.current_policy_version != run.policy_version
            ):
                raise ValueError("subscription tools require current run policy and phase")
            record = await work.projects.get_policy(run.project_id, authority.policy_version)
            if record.document_schema_version != 1:
                raise ValueError("unsupported subscription tool policy schema")
            policy = ProjectPolicy.model_validate(record.document)
            if (
                policy.id != run.project_id
                or record.project_id != run.project_id
                or policy.version != run.policy_version
                or record.version != run.policy_version
            ):
                raise ValueError("subscription tool policy identity differs")
            await work.rollback()
        tools = authority.permitted_tools
        ranking = self._search_ranking
        objective = _subscription_search_objective(request)
        if run.state is RunState.PLANNING and (
            request.task.purpose not in (SpecialistPurpose.PRIMARY, SpecialistPurpose.PLANNING)
            or not tools <= _READS
        ):
            raise ValueError("subscription planning tools must be repository reads")
        if run.state is RunState.PLANNING and run.worktree_path is None:
            if authority.worktree_id != repository_resource_identity(run.project_id):
                raise ValueError("subscription planning resource differs")
            return ControlledToolService(
                self._factory,
                artifact_store=self._artifacts,
                repository_reader=RepositoryReader(
                    policy.repository_path,
                    secret_paths=policy.effective_secret_paths,
                ),
                redactor=self._redactor,
                search_ranker=ranking.ranker,
                search_ranking_mode=ranking.mode,
                search_ranking_top_k=ranking.top_k,
                search_objective=objective,
            )
        if not run.worktree_path or not run.branch_name or not run.base_sha:
            raise ValueError("subscription managed worktree is absent")
        identity = WorktreeIdentity.for_run(
            run.project_id,
            run.id,
            run.branch_name,
            policy.database.enabled,
        )
        if authority.worktree_id != identity.worktree_name:
            raise ValueError("subscription managed resource differs")
        tree = ManagedWorktree(
            identity=identity, path=Path(run.worktree_path), base_sha=run.base_sha
        )
        git = self._delivery.git(policy)
        reader = self._delivery.reader(policy, tree)
        checks = ToolName.BUILD_RUN_NAMED_CHECK in tools
        environment = await self._delivery.environment(run, policy, tree) if checks else {}
        return ControlledToolService(
            self._factory,
            artifact_store=self._artifacts,
            repository_reader=reader,
            repository_writer=(
                self._delivery.writer(policy, tree, controlled_git=git) if tools & _WRITES else None
            ),
            controlled_git=git,
            operation_executor=self._delivery.operation_executor,
            redactor=self._redactor,
            worktree=tree,
            runner_factory=self._delivery if checks else None,
            command_environment=environment,
            search_ranker=ranking.ranker,
            search_ranking_mode=ranking.mode,
            search_ranking_top_k=ranking.top_k,
            search_objective=objective,
        )
