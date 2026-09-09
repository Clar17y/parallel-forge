"""Startup resource factories receive persisted run policy, never intent-supplied policy."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from forge.application.services.recovery import RecoveryError
from forge.domain.operation import OperationOutcome, OperationRequest, canonical_digest
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_pr_approval import authorized
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("kind", [
    "worktree.create", "worktree.teardown", "database.provision", "database.teardown",
    "worktree.environment.stage", "worktree.setup.command",
])
async def test_resource_registry_loads_bound_policy(tmp_path, workflow_session_factory, kind):
    from forge.worker.resource_recovery import resource_recovery_adapters

    factory = workflow_session_factory
    case, _git, _read, _handler, _command, _approval = await authorized(tmp_path, factory)
    calls = []

    class Inspection:
        async def reconcile(self, intent):
            calls.append(intent.id)
            return OperationOutcome(payload={"observed": True})

    def construct(run, policy, *, kind):
        assert run.id == case.run_id
        assert policy.id == run.project_id and policy.version == run.policy_version
        assert kind in {
            "worktree.create", "worktree.teardown", "database.provision", "database.teardown",
            "worktree.environment.stage", "worktree.setup.command",
        }
        return Inspection()

    adapters = resource_recovery_adapters(factory, SimpleNamespace(resource_recovery_adapter=construct))
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
    payload = {"policy_version": run.policy_version}
    request = OperationRequest(
        run_id=run.id, kind=kind, idempotency_key=f"{run.id}:{kind}:registry",
        request_digest=canonical_digest(payload), request_payload=payload,
    )
    operations = PostgresOperationRepository(factory)
    intent = await operations.begin(
        run_id=run.id, operation_type=kind, idempotency_key=request.idempotency_key,
        request_digest=request.request_digest, request_payload=payload,
    )
    with pytest.raises(RecoveryError):
        await adapters[kind].invoke(intent)
    assert (await adapters[kind].reconcile(intent)).payload == {"observed": True}
    assert calls == [intent.id]
    altered = {"policy_version": run.policy_version + 1}
    with pytest.raises(RecoveryError):
        await adapters[kind].reconcile(replace(
            intent, request_payload=altered, request_digest=canonical_digest(altered),
        ))
    assert calls == [intent.id]
