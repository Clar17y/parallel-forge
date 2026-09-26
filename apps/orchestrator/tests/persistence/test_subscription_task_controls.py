"""Real PostgreSQL operator controls for tasks that have never attempted work."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.domain.scheduling import ScheduleTask
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _enqueue,
    _remove_disposable_subscription_rows,
    _route,
)


def _service(session_factory):
    from forge.application.services.subscription_task_controls import SubscriptionTaskControlService

    return SubscriptionTaskControlService(lambda: PostgresUnitOfWork(session_factory))


def _request(action, version=0, **overrides):
    from forge.domain.subscription_task_controls import SubscriptionTaskControlRequest

    return SubscriptionTaskControlRequest(
        action=action,
        expected_run_version=0,
        expected_task_version=version,
        reason="Operator investigation",
        **overrides,
    )


async def _control(service, run_id, task_id, request, *, key=None, actor=None):
    return await service.control(
        run_id=run_id,
        task_id=task_id,
        request=request,
        actor=actor or LocalOperatorProfileActor(),
        idempotency_key=key or str(uuid4()),
    )


async def _seed(session_factory, persisted_run, *, fallbacks=()):
    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.session.get(Run, persisted_run.id)
        run.state = "IMPLEMENTING"
        primary = await _admit_run(
            work,
            persisted_run,
            (_route("p"), _route("p")),
            worker_fallbacks=fallbacks,
        )
        await work.scheduler.enqueue(
            ScheduleTask(
                run_id=persisted_run.id,
                task_id=primary,
                worktree_id="task-control-tree",
                owned_paths=("apps",),
                max_repairs=3,
            )
        )
        parent = await work.session.get(SubscriptionScheduledTask, primary)
        parent.state = "blocked"
        logical_parent = await work.session.get(SubscriptionTask, primary)
        logical_parent.state = "blocked"
        child = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="task-control-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()
        return primary, child


@pytest.mark.integration
async def test_queued_pause_resume_survives_service_restart_without_waking_parent_or_budget(
    session_factory, persisted_run
):
    from forge.application.services.subscription_task_controls import SubscriptionTaskControlService
    from forge.domain.subscription_task_controls import SubscriptionTaskControlRequest

    primary, child = await _seed(session_factory, persisted_run)
    clock = [datetime(2026, 9, 12, 12, tzinfo=UTC)]
    factory = lambda: PostgresUnitOfWork(session_factory)
    actor = LocalOperatorProfileActor()
    request = SubscriptionTaskControlRequest(
        action="pause",
        expected_run_version=0,
        expected_task_version=0,
        reason="Inspect partial work",
    )
    service = SubscriptionTaskControlService(factory, now=lambda: clock[0])
    paused = await service.control(
        run_id=persisted_run.id,
        task_id=child,
        actor=actor,
        idempotency_key=str(uuid4()),
        request=request,
    )
    assert paused.status == "paused" and paused.task_version == 1
    async with factory() as work:
        logical = await work.session.get(SubscriptionTask, child)
        scheduled = await work.session.get(SubscriptionScheduledTask, child)
        parent = await work.session.get(SubscriptionScheduledTask, primary)
        assert logical.pause_requested and scheduled.pause_requested
        assert logical.state == scheduled.state == "blocked"
        assert parent.state == "blocked"
        assert await work.scheduler.claim_ready("during-pause", timedelta(seconds=30)) is None
        usage = await work.subscription_budget.usage(persisted_run.id, child)
        assert usage.outstanding.provider_attempts == usage.consumed.provider_attempts == 0
    clock[0] += timedelta(days=1)
    resumed = await SubscriptionTaskControlService(factory, now=lambda: clock[0]).control(
        run_id=persisted_run.id,
        task_id=child,
        actor=actor,
        idempotency_key=str(uuid4()),
        request=SubscriptionTaskControlRequest(
            action="resume",
            expected_run_version=0,
            expected_task_version=1,
            reason="Continue",
            pause_receipt_id=paused.receipt_id,
        ),
    )
    assert resumed.status == "queued" and resumed.task_version == 2
    async with factory() as work:
        logical = await work.session.get(SubscriptionTask, child)
        scheduled = await work.session.get(SubscriptionScheduledTask, child)
        assert not logical.pause_requested and not scheduled.pause_requested
        assert scheduled.repairs == 0 and scheduled.lease_generation == 0
        claim = await work.scheduler.claim_ready("after-restart", timedelta(seconds=30))
        assert claim is not None and claim.task_id == child


@pytest.mark.integration
async def test_resume_rejects_receipt_version_tampering_against_immutable_audit(
    session_factory, persisted_run
):
    from copy import deepcopy

    from forge.domain.subscription_task_controls import TaskControlConflict
    from forge.persistence.models.api import ApiMutation

    _, child = await _seed(session_factory, persisted_run)
    service = _service(session_factory)
    paused = await _control(service, persisted_run.id, child, _request("pause"))
    async with PostgresUnitOfWork(session_factory) as work:
        mutation = await work.session.get(ApiMutation, paused.receipt_id)
        payload = deepcopy(mutation.response_payload)
        payload["receipt"]["task_version"] = 3
        mutation.response_payload = payload
        task = await work.session.get(SubscriptionTask, child)
        task.version = 3
        await work.commit()
    with pytest.raises(TaskControlConflict):
        await _control(
            service,
            persisted_run.id,
            child,
            _request(
                "resume",
                3,
                pause_receipt_id=paused.receipt_id,
            ),
        )


@pytest.mark.integration
async def test_exact_replay_after_resume_and_cancel_returns_original_redacted_receipt(
    session_factory, persisted_run
):
    import json

    from forge.domain.subscription_task_controls import SubscriptionTaskControlRequest
    from forge.persistence.models.api import ApiMutation, OperatorAuditEvent
    from forge.persistence.repositories.mutations import MutationConflict
    from sqlalchemy import select

    _, child = await _seed(session_factory, persisted_run)
    service, key = _service(session_factory), str(uuid4())
    request = SubscriptionTaskControlRequest(
        action="pause",
        expected_run_version=0,
        expected_task_version=0,
        reason="token=credential-must-not-be-retained",
    )
    paused = await _control(service, persisted_run.id, child, request, key=key)
    await _control(
        service, persisted_run.id, child, _request("resume", 1, pause_receipt_id=paused.receipt_id)
    )
    await _control(service, persisted_run.id, child, _request("cancel", 2))
    replay = await _control(_service(session_factory), persisted_run.id, child, request, key=key)
    assert replay == paused
    with pytest.raises(MutationConflict):
        await _control(service, persisted_run.id, child, _request("cancel"), key=key)
    async with PostgresUnitOfWork(session_factory) as work:
        audits = list(
            await work.session.scalars(
                select(OperatorAuditEvent).where(
                    OperatorAuditEvent.subject_id == child,
                )
            )
        )
        mutation = await work.session.get(ApiMutation, paused.receipt_id)
        assert len(audits) == 3
        stored = json.dumps([mutation.response_payload, *(event.payload for event in audits)])
        assert "credential-must-not-be-retained" not in stored
        assert key not in stored


@pytest.mark.integration
@pytest.mark.parametrize("same_key", [False, True])
async def test_simultaneous_controls_use_one_transition_or_exact_replay(
    session_factory, persisted_run, same_key
):
    import asyncio

    from forge.domain.subscription_task_controls import TaskControlConflict, TaskControlReceipt
    from forge.persistence.models.api import OperatorAuditEvent
    from sqlalchemy import func, select

    _, child = await _seed(session_factory, persisted_run)
    key = str(uuid4())
    results = await asyncio.gather(
        _control(_service(session_factory), persisted_run.id, child, _request("pause"), key=key),
        _control(
            _service(session_factory),
            persisted_run.id,
            child,
            _request("pause"),
            key=key if same_key else str(uuid4()),
        ),
        return_exceptions=True,
    )
    if same_key:
        assert results[0] == results[1] and isinstance(results[0], TaskControlReceipt)
    else:
        assert sum(isinstance(result, TaskControlReceipt) for result in results) == 1
        assert sum(isinstance(result, TaskControlConflict) for result in results) == 1
    async with PostgresUnitOfWork(session_factory) as work:
        task = await work.session.get(SubscriptionTask, child)
        assert task.version == 1
        assert (
            await work.session.scalar(
                select(func.count())
                .select_from(OperatorAuditEvent)
                .where(
                    OperatorAuditEvent.subject_id == child,
                )
            )
            == 1
        )


@pytest.mark.integration
async def test_cancel_wakes_parent_with_cancelled_outcome_and_no_success_handoff(
    session_factory, persisted_run
):
    from forge.domain.subscription_task_controls import TaskControlConflict

    primary, child = await _seed(session_factory, persisted_run)
    service = _service(session_factory)
    result = await _control(service, persisted_run.id, child, _request("cancel"))
    assert result.status == "cancelled"
    async with PostgresUnitOfWork(session_factory) as work:
        task = await work.session.get(SubscriptionTask, child)
        scheduled = await work.session.get(SubscriptionScheduledTask, child)
        parent = await work.session.get(SubscriptionScheduledTask, primary)
        assert task.cancel_requested and scheduled.cancel_requested
        assert task.state == scheduled.state == "terminal"
        assert parent.state == "queued"
        outcome = (await work.subscription.invocation_outcomes(persisted_run.id, (child,)))[0]
        assert outcome.cancel_requested and outcome.recorded_handoff is None
        assert scheduled.repairs == 0
    with pytest.raises(TaskControlConflict):
        await _control(service, persisted_run.id, child, _request("pause", 1))


@pytest.mark.integration
@pytest.mark.parametrize("blocked_by", ["attempt", "lease", "effect", "terminal", "primary"])
async def test_unsafe_or_non_specialist_task_cannot_use_queued_control_path(
    session_factory, persisted_run, blocked_by
):
    from forge.domain.subscription import AttemptIdentity, decode_subscription_record
    from forge.domain.subscription_task_controls import TaskControlConflict
    from forge.persistence.models.scheduling import SubscriptionScheduledEffect

    primary, child = await _seed(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        if blocked_by == "attempt":
            task = await work.session.get(SubscriptionTask, child)
            contract = decode_subscription_record(task.payload)
            await work.subscription.create_attempt(
                AttemptIdentity(
                    run_id=persisted_run.id, task_id=child, attempt_id=uuid4(), attempt_number=1
                ),
                route_payload=contract.route,
                idempotency_key=str(uuid4()),
            )
        elif blocked_by == "lease":
            assert (
                await work.scheduler.claim_ready("racing-worker", timedelta(seconds=30)) is not None
            )
        elif blocked_by == "effect":
            work.session.add(
                SubscriptionScheduledEffect(
                    id=uuid4(),
                    run_id=persisted_run.id,
                    task_id=child,
                    lease_owner="uncertain-worker",
                    lease_generation=1,
                    candidate_epoch=0,
                    state="reconciling",
                )
            )
        elif blocked_by == "terminal":
            task = await work.session.get(SubscriptionTask, child)
            task.state = "terminal"
        await work.commit()
    with pytest.raises(
        TaskControlConflict, match="run controls" if blocked_by == "primary" else None
    ):
        await _control(
            _service(session_factory),
            persisted_run.id,
            primary if blocked_by == "primary" else child,
            _request("pause"),
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "changed", ["task", "parent", "candidate", "worktree", "run_phase", "task_version"]
)
async def test_resume_requires_current_pause_contract_candidate_and_versions(
    session_factory, persisted_run, changed
):
    from dataclasses import replace

    from forge.domain.subscription import decode_subscription_record, encode_subscription_record
    from forge.domain.subscription_task_controls import TaskControlConflict
    from forge.persistence.models.scheduling import SubscriptionSchedulerRun

    primary, child = await _seed(session_factory, persisted_run)
    service = _service(session_factory)
    paused = await _control(service, persisted_run.id, child, _request("pause"))
    async with PostgresUnitOfWork(session_factory) as work:
        if changed in {"task", "parent"}:
            task = await work.session.get(SubscriptionTask, child if changed == "task" else primary)
            contract = decode_subscription_record(task.payload)
            task.payload = encode_subscription_record(
                replace(contract, owned_paths=("apps/narrowed",))
            )
        elif changed == "candidate":
            candidate = await work.session.get(SubscriptionSchedulerRun, persisted_run.id)
            candidate.candidate_epoch += 1
        elif changed == "worktree":
            scheduled = await work.session.get(SubscriptionScheduledTask, child)
            scheduled.worktree_id = "foreign-tree"
        elif changed == "run_phase":
            run = await work.session.get(Run, persisted_run.id)
            run.state = "REMEDIATING"
        else:
            task = await work.session.get(SubscriptionTask, child)
            task.version += 1
        await work.commit()
    with pytest.raises(TaskControlConflict):
        await _control(
            service,
            persisted_run.id,
            child,
            _request("resume", 1, pause_receipt_id=paused.receipt_id),
        )


@pytest.mark.integration
async def test_paused_task_uses_no_capacity_and_other_worktrees_progress(
    session_factory, persisted_run
):
    primary, child = await _seed(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        other = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="independent-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()
    await _control(_service(session_factory), persisted_run.id, child, _request("pause"))
    async with PostgresUnitOfWork(session_factory) as work:
        claim = await work.scheduler.claim_ready("eligible-worker", timedelta(seconds=30))
        assert claim is not None and claim.task_id == other
        assert await work.scheduler._active_count() == 1
        paused = await work.session.get(SubscriptionScheduledTask, child)
        assert paused.lease_owner is None and paused.lease_expires_at is None


@pytest.mark.integration
async def test_preapproved_fallback_binding_and_new_quota_block_survive_pause_resume(
    session_factory, persisted_run
):
    from dataclasses import replace

    from forge.domain.provider_quota import QuotaExhaustion
    from forge.domain.subscription import (
        RouteBinding,
        RouteMapping,
        decode_subscription_record,
        encode_subscription_record,
    )
    from forge.domain.subscription_quota import QuotaPoolKey

    fallback = _route("q")
    _, child = await _seed(session_factory, persisted_run, fallbacks=(fallback,))
    async with PostgresUnitOfWork(session_factory) as work:
        task = await work.session.get(SubscriptionTask, child)
        contract = decode_subscription_record(task.payload)
        binding = RouteBinding(
            requested=contract.route.requested,
            effective=fallback,
            mapping_applied=RouteMapping(
                requested=contract.route.requested,
                effective=fallback,
                approved_by="profile:test",
                approval_id="profile:test:1",
                reason="Approved fallback",
            ),
        )
        payload = encode_subscription_record(replace(contract, route=binding))
        task.payload = payload
        scheduled = await work.session.get(SubscriptionScheduledTask, child)
        scheduled.provider = "q"
        await work.commit()
    service = _service(session_factory)
    paused = await _control(service, persisted_run.id, child, _request("pause"))
    async with PostgresUnitOfWork(session_factory) as work:
        # Both approved routes may become blocked while the task is paused.
        for provider in ("p", "q"):
            await work.quota.report_exhaustion(
                QuotaPoolKey(provider, "local", "subscription-allowance_only"),
                QuotaExhaustion(
                    datetime.now(UTC), "operator_report", datetime.now(UTC) + timedelta(hours=2)
                ),
                actor_id=uuid4(),
                idempotency_key=str(uuid4()),
            )
        await work.commit()
    await _control(
        service, persisted_run.id, child, _request("resume", 1, pause_receipt_id=paused.receipt_id)
    )
    async with PostgresUnitOfWork(session_factory) as work:
        task = await work.session.get(SubscriptionTask, child)
        assert task.payload == payload
        assert (
            await work.scheduler.claim_execution_ready("blocked-provider", timedelta(seconds=30))
            is None
        )
        usage = await work.subscription_budget.usage(persisted_run.id, child)
        assert usage.consumed.provider_attempts == usage.outstanding.provider_attempts == 0


@pytest.mark.integration
async def test_foreign_and_older_pause_receipts_cannot_resume_a_newer_control(
    session_factory, persisted_run
):
    from forge.domain.subscription_task_controls import TaskControlConflict

    primary, child = await _seed(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        other = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="other-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()
    service = _service(session_factory)
    first = await _control(service, persisted_run.id, child, _request("pause"))
    foreign = await _control(service, persisted_run.id, other, _request("pause"))
    with pytest.raises(TaskControlConflict):
        await _control(
            service,
            persisted_run.id,
            child,
            _request("resume", 1, pause_receipt_id=foreign.receipt_id),
        )
    await _control(
        service, persisted_run.id, child, _request("resume", 1, pause_receipt_id=first.receipt_id)
    )
    newer = await _control(service, persisted_run.id, child, _request("pause", 2))
    with pytest.raises(TaskControlConflict):
        await _control(
            service,
            persisted_run.id,
            child,
            _request("resume", 3, pause_receipt_id=first.receipt_id),
        )
    resumed = await _control(
        service, persisted_run.id, child, _request("resume", 3, pause_receipt_id=newer.receipt_id)
    )
    assert resumed.status == "queued" and resumed.task_version == 4


@pytest.mark.integration
async def test_actor_keys_and_current_run_versions_remain_independent(
    session_factory, persisted_run
):
    from forge.domain.subscription_task_controls import TaskControlConflict

    _, child = await _seed(session_factory, persisted_run)
    service, key = _service(session_factory), str(uuid4())
    paused = await _control(service, persisted_run.id, child, _request("pause"), key=key)
    with pytest.raises(TaskControlConflict):
        await _control(
            service,
            persisted_run.id,
            child,
            _request("pause"),
            key=key,
            actor=LocalOperatorProfileActor(actor_id=uuid4()),
        )
    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.session.get(Run, persisted_run.id)
        run.version += 1
        await work.commit()
    request = _request("resume", 1, pause_receipt_id=paused.receipt_id)
    with pytest.raises(TaskControlConflict):
        await _control(service, persisted_run.id, child, request)
    resumed = await _control(
        service, persisted_run.id, child, request.model_copy(update={"expected_run_version": 1})
    )
    assert resumed.run_version == 1 and resumed.status == "queued"


@pytest.mark.integration
async def test_audit_failure_rolls_back_flags_versions_and_mutation_reservation(
    session_factory, persisted_run
):
    from types import SimpleNamespace

    from forge.application.services.subscription_task_controls import SubscriptionTaskControlService
    from forge.persistence.models.api import ApiMutation, OperatorAuditEvent
    from sqlalchemy import func, select

    _, child = await _seed(session_factory, persisted_run)

    class BrokenAuditWork(PostgresUnitOfWork):
        async def __aenter__(self):
            await super().__aenter__()
            audit = self.audit

            async def fail(**kwargs):
                await audit.append(**kwargs)
                raise RuntimeError("injected audit failure")

            self.audit = SimpleNamespace(append=fail)
            return self

    service = SubscriptionTaskControlService(lambda: BrokenAuditWork(session_factory))
    with pytest.raises(RuntimeError, match="injected audit failure"):
        await _control(service, persisted_run.id, child, _request("pause"))
    async with PostgresUnitOfWork(session_factory) as work:
        task = await work.session.get(SubscriptionTask, child)
        scheduled = await work.session.get(SubscriptionScheduledTask, child)
        assert task.version == 0 and not task.pause_requested and not scheduled.pause_requested
        assert task.state == scheduled.state == "queued"
        assert await work.session.scalar(select(func.count()).select_from(ApiMutation)) == 0
        assert await work.session.scalar(select(func.count()).select_from(OperatorAuditEvent)) == 0


@pytest.mark.integration
async def test_pause_and_real_attempt_admission_have_only_one_winner(
    session_factory, persisted_run
):
    import asyncio

    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.domain.subscription_task_controls import TaskControlConflict, TaskControlReceipt
    from test_subscription_usage import _reservation

    _, child = await _seed(session_factory, persisted_run)
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    control, admission = await asyncio.gather(
        _control(_service(session_factory), persisted_run.id, child, _request("pause")),
        executor.admit_next("racing-provider", _reservation()),
        return_exceptions=True,
    )
    if isinstance(control, TaskControlReceipt):
        assert admission is None
    else:
        assert isinstance(control, TaskControlConflict)
        assert admission is not None and not isinstance(admission, BaseException)
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id, child)
        assert usage.outstanding.provider_attempts == (0 if admission is None else 1)


@pytest.mark.integration
async def test_paused_dependency_is_pending_until_a_terminal_cancel(session_factory, persisted_run):
    from forge.domain.subscription import (
        LogicalTaskContract,
        RouteBinding,
        SpecialistPurpose,
        TaskBudget,
    )

    primary, child = await _seed(session_factory, persisted_run)
    dependent = uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription.create_task(
            LogicalTaskContract(
                run_id=persisted_run.id,
                task_id=dependent,
                parent_task_id=primary,
                purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
                route=RouteBinding(requested=_route("p"), effective=_route("p")),
                budget=TaskBudget(),
                owned_paths=("apps/after",),
                dependency_task_ids=(child,),
            ),
            idempotency_key=str(uuid4()),
        )
        await work.scheduler.enqueue(
            ScheduleTask(
                run_id=persisted_run.id,
                task_id=dependent,
                parent_task_id=primary,
                worktree_id="task-control-tree",
                owned_paths=("apps/after",),
                dependency_task_ids=(child,),
                max_repairs=3,
            )
        )
        await work.commit()
    service = _service(session_factory)
    await _control(service, persisted_run.id, child, _request("pause"))
    async with PostgresUnitOfWork(session_factory) as work:
        assert (
            await work.scheduler.claim_execution_ready("pending-dependency", timedelta(seconds=30))
            is None
        )
    await _control(service, persisted_run.id, child, _request("cancel", 1))
    async with PostgresUnitOfWork(session_factory) as work:
        claim = await work.scheduler.claim_execution_ready(
            "settled-dependency", timedelta(seconds=30)
        )
        assert claim is not None and claim.task_id == dependent


@pytest.mark.integration
async def test_whole_run_pause_does_not_create_an_unusable_individual_pause_receipt(
    session_factory, persisted_run
):
    from forge.domain.subscription_task_controls import TaskControlConflict

    _, child = await _seed(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        paused_run = await work.runs.pause(persisted_run.id, 0, "run.paused", {})
        await work.commit()
    with pytest.raises(TaskControlConflict, match="run controls"):
        await _control(
            _service(session_factory),
            persisted_run.id,
            child,
            _request("pause").model_copy(update={"expected_run_version": paused_run.version}),
        )
