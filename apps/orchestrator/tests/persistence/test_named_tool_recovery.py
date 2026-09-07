"""Recovery from terminal named-check evidence without rerunning a command."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.adapters.named_check import NamedCheckOperationAdapter
from forge.application.services.recovery import RecoveryService
from forge.application.services.tool_recovery import ToolRecoveryDisposition, ToolRecoveryService
from forge.application.services.tools import ToolInvocationError
from forge.domain.operation import OperationStatus, canonical_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.tool import ToolCallStatus
from forge.persistence.models import OperationIntent as OperationRow
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork

from apps.orchestrator.tests.application.test_tool_named_check import _named_case


@pytest.mark.parametrize("corruption", [None, "receipt", "scope"])
@pytest.mark.parametrize(
    "exit_code,expected", [(0, ToolCallStatus.SUCCEEDED), (1, ToolCallStatus.FAILED)]
)
async def test_named_receipt_recovers_after_audit_transaction_failure(
    session_factory, tmp_path, exit_code, expected, corruption
):
    case = await _named_case(session_factory, tmp_path, exit_code=exit_code)

    class InterruptedSettlement(PostgresUnitOfWork):
        async def __aenter__(self):
            await super().__aenter__()

            async def interrupted(*args, **kwargs):
                raise RuntimeError("injected operation settlement interruption")

            self.operations.complete = interrupted
            return self

    original_factory = case.service._unit_of_work_factory
    case.service._unit_of_work_factory = lambda: InterruptedSettlement(session_factory)
    with pytest.raises(ToolInvocationError):
        await case.service.invoke(case.context, case.request)
    case.service._unit_of_work_factory = original_factory
    assert case.factory.calls == 1
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(case.context.invocation_id)
        assert call.status is ToolCallStatus.RUNNING
        operation = await work.operations.get(call.operation_intent_id)
        assert operation.status is OperationStatus.PENDING
        run = await work.runs.get(call.run_id)
        policy_record = await work.projects.get_policy(run.project_id, run.policy_version)
        policy = ProjectPolicy.model_validate(policy_record.document)
        adapter = NamedCheckOperationAdapter.for_recovery(
            worktree=case.service._worktree,
            policy=policy,
            artifacts=work.artifacts,
            artifact_store=case.store,
        )
        async with session_factory() as session, session.begin():
            row = await session.get(OperationRow, operation.id)
            row.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        recovered = await RecoveryService(PostgresOperationRepository(session_factory)).reconcile(
            operation.id, adapter
        )
        assert recovered.status is OperationStatus.SUCCEEDED
    recovery = ToolRecoveryService(lambda: PostgresUnitOfWork(session_factory), case.store)
    if corruption == "receipt":
        case.store.stored[recovered.outcome["receipt_digest"]] = b"substituted receipt"
    elif corruption == "scope":
        async with session_factory() as session, session.begin():
            row = await session.get(OperationRow, operation.id)
            row.request_payload = {**row.request_payload, "step_id": str(uuid4())}
            row.request_digest = canonical_digest(row.request_payload)
    result = await recovery.recover_one(call.id)
    if corruption is not None:
        expected_disposition = (
            ToolRecoveryDisposition.UNRESOLVED
            if corruption == "receipt"
            else ToolRecoveryDisposition.INTERVENTION
        )
        assert result.disposition is expected_disposition
        async with PostgresUnitOfWork(session_factory) as work:
            assert (await work.tool_calls.get(call.id)).status is ToolCallStatus.RUNNING
            events = await work.events.list_after(call.run_id, 0)
            assert not any(event.payload.get("tool_call_id") == str(call.id) for event in events)
        assert case.factory.calls == 1
        return
    assert result.disposition is ToolRecoveryDisposition.SETTLED
    assert (await recovery.recover_one(call.id)).disposition is ToolRecoveryDisposition.TERMINAL
    replay = await case.service.invoke(case.context, case.request)
    assert replay.status is expected
    assert case.factory.calls == 1
    async with PostgresUnitOfWork(session_factory) as work:
        final = await work.tool_calls.get(call.id)
        assert final.status is expected
        assert final.artifact_digests == replay.artifact_digests
        events = await work.events.list_after(call.run_id, 0)
        assert sum(event.payload.get("tool_call_id") == str(call.id) for event in events) == 1
