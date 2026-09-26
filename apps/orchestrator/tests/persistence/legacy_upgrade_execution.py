"""A real legacy admission and controlled write interrupted before model completion."""

from datetime import timedelta
from uuid import uuid4

import pytest
from forge.application.ports.executions import ExecutionStatus
from forge.application.services.auth import AuthService
from forge.domain.actor import AgentRole
from forge.domain.run import RunState
from forge.domain.tool import ToolCallStatus, ToolName, ToolRequest
from forge.persistence.models import RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.recovery import PostgresRecoveryBarrier
from forge.worker.startup_intervention import StartupInterventionRecovery
from legacy_tool_fixture import SimulatedWorkerExit, legacy_tools
from legacy_upgrade_case import LegacyPlanGateway, legacy_plan_case
from legacy_upgrade_controls import replay_legacy_plan_controls
from sqlalchemy import func, update


class InterruptedLegacyGateway(LegacyPlanGateway):
    async def execute(self, request):
        if request.role is AgentRole.PLANNER:
            return await super().execute(request)
        assert request.role is AgentRole.DEVELOPER
        self.requests.append(request)
        case = self.case
        service, context, tree, writer = await legacy_tools(case, self.session_factory, request)
        tool_request = ToolRequest(
            name=ToolName.REPOSITORY_WRITE_FILE,
            arguments={
                "path": "src/counter.py",
                "content": "def increment(value):\n    return value + 1\n",
            },
        )
        receipt = await service.invoke(context, tool_request)
        assert receipt.status is ToolCallStatus.SUCCEEDED
        assert writer.calls == 1
        case.tool_context, case.tool_request, case.tool_receipt = context, tool_request, receipt
        case.tree, case.original_writer = tree, writer
        case.partial = (tree.path / "src/counter.py").read_bytes()
        case.tool_artifacts = {
            digest: await case.store.open_bytes(digest) for digest in receipt.artifact_digests
        }
        raise SimulatedWorkerExit


async def unfinished_legacy_case(session_factory, tmp_path):
    gateway = InterruptedLegacyGateway()
    case = await legacy_plan_case(session_factory, tmp_path, gateway=gateway)
    try:
        gateway.case, gateway.session_factory = case, session_factory
        auth = AuthService(case.factory)
        case.operator_session = await auth.exchange_bootstrap(await auth.issue_bootstrap())
        await replay_legacy_plan_controls(case, session_factory, case.operator_session)
        commands = PostgresCommandRepository(session_factory)
        prepare = await commands.claim_next(worker_id="a9-execution", lease_seconds=60)
        assert prepare is not None and prepare.command_type == "prepare_worktree"
        async with case.factory() as work:
            await case.handlers[prepare.command_type](prepare, work)
        await commands.complete(prepare.id, worker_id="a9-execution")
        command = await commands.claim_next(worker_id="a9-execution", lease_seconds=60)
        assert command is not None and command.command_type == "implement"
        with pytest.raises(SimulatedWorkerExit):
            async with case.factory() as work:
                await case.handlers[command.command_type](command, work)
        case.command = command
        # Controlled expiry models a dead worker without wall-clock sleeping.
        async with session_factory() as session, session.begin():
            await session.execute(
                update(RunCommand)
                .where(RunCommand.id == command.id)
                .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        async with case.factory() as work:
            case.run = await work.runs.get(case.run.id)
            assert case.run.state is RunState.IMPLEMENTING
            case.admission = await work.executions.get_admission(
                case.run.id, case.tool_context.agent_execution_id
            )
            assert case.admission.status is ExecutionStatus.RUNNING
            case.call = await work.tool_calls.get(case.tool_context.invocation_id)
            case.operation = await work.operations.get(case.call.operation_intent_id)
        return case
    except BaseException:
        await case.handlers.aclose()
        raise


async def verify_unfinished_recovery(case, session_factory):
    async with case.factory() as work:
        assert await work.runs.get(case.run.id) == case.run
        assert (
            await work.executions.get_admission(case.run.id, case.tool_context.agent_execution_id)
            == case.admission
        )
        assert await work.tool_calls.get(case.call.id) == case.call
        assert await work.operations.get(case.operation.id) == case.operation
        assert await work.subscription.envelope_for_run(case.run.id) is None
    service, _, tree, writer = await legacy_tools(case, session_factory, case.gateway.requests[-1])
    replay = await service.invoke(case.tool_context, case.tool_request)
    assert replay == case.tool_receipt
    assert writer.calls == 0 and case.original_writer.calls == 1
    assert (tree.path / "src/counter.py").read_bytes() == case.partial
    assert {
        digest: await case.store.open_bytes(digest) for digest in case.tool_artifacts
    } == case.tool_artifacts

    # A terminal tool receipt proves the write, not an unknown model outcome.
    # Two fresh startup owners quarantine that attempt once instead of inventing
    # completion or admitting a replacement provider call.
    barrier = PostgresRecoveryBarrier(session_factory)
    for _ in range(2):
        lease = await barrier.acquire(owner_id=uuid4(), lease_seconds=30)
        assert lease is not None
        recovery = StartupInterventionRecovery(session_factory)
        try:
            await recovery.wait_for_owners()
            assert await recovery.quarantine() == (case.run.id,)
            await barrier.finish(lease)
        finally:
            await barrier.abandon(lease)
    async with case.factory() as work:
        run = await work.runs.get(case.run.id)
        assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
        assert run.version == case.run.version + 1
        events = await work.events.list_after(case.run.id, 0)
        assert sum(event.event_type == "run.recovery_intervention" for event in events) == 1
        assert await work.tool_calls.get(case.call.id) == case.call
        assert (
            await work.executions.get_admission(case.run.id, case.tool_context.agent_execution_id)
            == case.admission
        )
    assert (
        await PostgresCommandRepository(session_factory).claim_next(
            worker_id="a9-restart", lease_seconds=30
        )
        is None
    )
    assert len(case.gateway.requests) == 2
