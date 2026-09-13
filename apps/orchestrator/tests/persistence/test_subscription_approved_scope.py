"""Only the exact human-approved plan supplies implementation scope and checks."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.ports.scheduling import SchedulingConflict
from forge.application.ports.subscription_plan_gate import SubscriptionPlanGateError
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_plan_gate import SubscriptionPlanGateService
from forge.domain.paths import policy_path_key
from forge.domain.subscription import decode_subscription_record, encode_subscription_record
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.subscription_plan_gate import (
    PostgresSubscriptionPlanGateRepository,
)
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_delegation_application import delegation_case
from test_subscription_plan_gate import proposal_case
from test_subscription_preparation import preparation_case
from test_subscription_usage import _reservation


@pytest.mark.integration
@pytest.mark.parametrize("paths", [("src/api", "tests/api"), ()])
async def test_prepared_primary_receives_approved_scope_and_checks(
    session_factory, tmp_path, paths
):
    factory, evidence, command, service, provisioner = await preparation_case(
        session_factory, tmp_path, plan_scope=paths
    )
    async with factory() as work:
        before = await work.subscription.get_task(
            evidence.producer.run_id, evidence.producer.task_id
        )
        assert before.owned_paths == before.named_checks == ()
    async with factory() as work:
        await service.execute(command, work)
    async with factory() as work:
        row = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        contract = decode_subscription_record(row.payload)
        scheduled = await work.session.get(SubscriptionScheduledTask, row.id)
        assert contract.owned_paths == paths
        assert tuple(scheduled.owned_paths) == tuple(policy_path_key(path) for path in paths)
        assert contract.named_checks == ("unit",)
        assert contract.budget == before.budget and contract.route == before.route
    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "scoped-primary", _reservation()
    )
    assert admission is not None and admission.task.owned_paths == paths
    assert admission.task.named_checks == ("unit",)
    async with factory() as work:
        with pytest.raises(SchedulingConflict, match="ownership"):
            await work.scheduler.admit_effect(
                admission.lease, uuid4(), owned_paths=("outside/file.py",)
            )
    async with factory() as work:
        await service.execute(command, work)
    assert provisioner.calls == 1


@pytest.mark.integration
async def test_approved_scope_allows_real_delegation_with_named_checks(session_factory, tmp_path):
    factory, parent, children, _ = await delegation_case(
        session_factory,
        tmp_path,
        lambda child, _: (
            replace(child, owned_paths=("src/api/file.py",), named_checks=("unit",)),
        ),
        plan_scope=("src/api",),
    )
    assert parent.task.owned_paths == ("src/api",) and parent.task.named_checks == ("unit",)
    assert (
        await SubscriptionDecisionApplication(factory).apply_delegation(parent.attempt.attempt_id)
    ).accepted
    child = await SubscriptionDecisionExecutor(factory).admit_next("approved-child", _reservation())
    assert child is not None and child.task == children[0]


@pytest.mark.integration
@pytest.mark.parametrize("change", ["cancel", "rollback", "source"])
async def test_scope_publication_keeps_current_authority_and_atomicity(
    session_factory, tmp_path, monkeypatch, change
):
    factory, evidence, command, service, _ = await preparation_case(
        session_factory, tmp_path, plan_scope=("src",)
    )
    if change == "rollback":
        original = PostgresSubscriptionPlanGateRepository.resume_prepared

        async def fail(self, *args, **kwargs):
            await original(self, *args, **kwargs)
            raise RuntimeError("injected scope publication failure")

        monkeypatch.setattr(PostgresSubscriptionPlanGateRepository, "resume_prepared", fail)
    else:
        async with factory() as work:
            if change == "cancel":
                (
                    await work.session.get(SubscriptionTask, evidence.producer.task_id)
                ).cancel_requested = True
            else:
                (
                    await work.session.get(SubscriptionAttemptResult, evidence.producer.attempt_id)
                ).result_digest = "f" * 64
            await work.commit()
    with pytest.raises((ValueError, RuntimeError)):
        async with factory() as work:
            await service.execute(command, work)
    async with factory() as work:
        task = await work.subscription.get_task(evidence.producer.run_id, evidence.producer.task_id)
        assert task.owned_paths == task.named_checks == ()
        scheduled = await work.session.get(SubscriptionScheduledTask, task.task_id)
        assert not scheduled.owned_paths and scheduled.state == "blocked"


@pytest.mark.integration
async def test_initial_preparation_replay_rechecks_installed_scope(session_factory, tmp_path):
    factory, evidence, command, service, _ = await preparation_case(
        session_factory, tmp_path, plan_scope=("src",)
    )
    async with factory() as work:
        await service.execute(command, work)
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        task.payload = encode_subscription_record(
            replace(decode_subscription_record(task.payload), owned_paths=("outside",))
        )
        await work.commit()
    with pytest.raises(SubscriptionPlanGateError, match="prepared primary replay differs"):
        async with factory() as work:
            await service.execute(command, work)


@pytest.mark.integration
async def test_scoped_plan_cannot_approve_unregistered_checks(session_factory, tmp_path):
    factory, store, evidence, _, _ = await proposal_case(
        session_factory, tmp_path, plan_scope=("src",), plan_checks=("unregistered",)
    )
    outcome = await SubscriptionPlanGateService(store, factory).request_settled(
        evidence.producer.attempt_id
    )
    assert not outcome.accepted and outcome.disposition == "plan_repair_queued"
    async with factory() as work:
        assert await work.subscription_plan_gate.get(evidence.producer.attempt_id) is None
        assert (
            await work.subscription.get_task(evidence.producer.run_id, evidence.producer.task_id)
        ).owned_paths == ()
