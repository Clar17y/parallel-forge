"""Planning reads cross the real attempt, broker, tool and receipt boundaries."""

import pytest
from forge.application.services.runs import RunService
from forge.application.services.subscription_broker import (
    ControlledSubscriptionEffect,
    SubscriptionToolBroker,
)
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.tools import ControlledToolService
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunState
from forge.domain.scheduling import ScheduleTask
from forge.domain.subscription import BrokerAuthorizationBinding, SpecialistPurpose
from forge.domain.tool import (
    SubscriptionToolAuthorizationContext,
    ToolName,
    repository_resource_identity,
)
from forge.persistence.models import Run
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.tools.repository import RepositoryReader
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _remove_disposable_subscription_rows,
    _route,
)
from test_subscription_usage import _reservation
from test_task10_run_service_integration import StableInspector, _seed_project_task


@pytest.mark.integration
async def test_planning_read_has_subscription_lineage_and_durable_replay(session_factory, tmp_path):
    actor, project_id, task_id = await _seed_project_task(session_factory, tmp_path)
    factory = lambda: PostgresUnitOfWork(session_factory)
    run = await RunService(
        factory,
        repository_inspector=StableInspector(tmp_path / "repo"),
        data_root=tmp_path / "data",
    ).create_run(actor=actor, idempotency_key="planning-read", task_id=task_id)
    resource = repository_resource_identity(project_id)
    async with factory() as work:
        policy_record = await work.projects.get_policy(project_id, 1)
        policy = ProjectPolicy.model_validate(policy_record.document)
        primary = await _admit_run(work, run, (_route("p"),))
        row = await work.session.get(Run, run.id)
        row.state = RunState.PLANNING.value
        await work.scheduler.enqueue(
            ScheduleTask(
                run_id=run.id,
                task_id=primary,
                worktree_id=resource,
                owned_paths=("apps",),
                max_repairs=3,
            )
        )
        await work.commit()
    admission = await SubscriptionDecisionExecutor(factory).admit_next("planner", _reservation())
    assert admission is not None
    permitted = frozenset({ToolName.REPOSITORY_READ_FILE})
    authority = BrokerAuthorizationBinding(
        run_id=run.id,
        task_id=primary,
        attempt_id=admission.attempt.attempt_id,
        worktree_id=resource,
        role=SpecialistPurpose.PRIMARY,
        policy_version=1,
        permitted_tools=permitted,
        broker_token="planning-test-token",
    )
    context = SubscriptionToolAuthorizationContext(
        run_id=run.id,
        task_id=primary,
        attempt_id=admission.attempt.attempt_id,
        worktree_id=resource,
        purpose=SpecialistPurpose.PRIMARY,
        policy_version=1,
        permitted_tools=permitted,
    )
    source = tmp_path / "repo" / "README.md"
    source.write_text("canonical planning source", encoding="utf-8")
    service = ControlledToolService(
        factory,
        repository_reader=RepositoryReader(
            tmp_path / "repo", secret_paths=policy.effective_secret_paths
        ),
    )
    broker = SubscriptionToolBroker(
        factory,
        lease=admission.lease,
        authority=authority,
        effect=ControlledSubscriptionEffect(service, context),
    )
    arguments = {
        "token": "planning-test-token",
        "provider_call_key": "read-once",
        "tool_name": ToolName.REPOSITORY_READ_FILE,
        "arguments": {"path": "README.md"},
    }
    receipt = await broker.invoke(**arguments)
    assert receipt.accepted
    source.write_text("changed after receipt", encoding="utf-8")
    replay = await broker.invoke(**arguments)
    assert replay == receipt
    async with factory() as work:
        records = await work.tool_calls.list_for_run(run.id)
        assert len(records) == 1
        record = records[0]
        assert record.subscription_attempt_id == admission.attempt.attempt_id
        assert record.agent_execution_id is None and record.resource_id == resource
