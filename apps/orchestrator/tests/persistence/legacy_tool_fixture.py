"""Shared legacy controlled-tool fixture, compatible with the pinned v0.1 package."""

from uuid import uuid4

from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.tools import ControlledToolService
from forge.domain.resource import WorktreeIdentity
from forge.domain.tool import ToolAuthorizationContext
from forge.worker.delivery_runtime import DeliveryRuntime


class SimulatedWorkerExit(BaseException):
    """Test process-loss boundary, deliberately outside normal provider errors."""


class ObservedWriter:
    def __init__(self, writer):
        self.writer = writer
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self.writer, name)

    def write_file(self, path, content):
        self.calls += 1
        return self.writer.write_file(path, content)


async def legacy_tools(case, session_factory, request):
    async with case.factory() as work:
        approved = await ApprovedPlanLoader(case.store).load(work, request.run_id)
        execution = await work.executions.get_admission(request.run_id, request.execution_id)
    runtime = DeliveryRuntime(case.settings, session_factory, case.store)
    git = runtime.git(approved.policy)
    identity = WorktreeIdentity.for_run(
        approved.run.project_id,
        approved.run.id,
        approved.run.branch_name,
        approved.policy.database.enabled,
    )
    tree = git.inspect_worktree(identity, approved.run.base_sha)
    assert tree is not None and git.is_ancestor(tree)
    writer = ObservedWriter(runtime.writer(approved.policy, tree, controlled_git=git))
    service = ControlledToolService(
        unit_of_work_factory=case.factory,
        artifact_store=case.store,
        repository_reader=runtime.reader(approved.policy, tree),
        repository_writer=writer,
        controlled_git=git,
        operation_executor=runtime.operation_executor,
        worktree=tree,
        runner_factory=runtime,
        command_environment=await runtime.environment(approved.run, approved.policy, tree),
    )
    context = ToolAuthorizationContext(
        role=request.role,
        run_id=request.run_id,
        worktree_id=tree.identity.worktree_name,
        policy_version=approved.policy.version,
        agent_execution_id=request.execution_id,
        step_id=execution.step_id,
        invocation_id=uuid4(),
    )
    return service, context, tree, writer
