"""Run-locked operator controls with dedicated stopped-attempt authority."""

from dataclasses import replace
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_task_controls import TaskControlTransition
from forge.domain.operation import canonical_digest
from forge.domain.paths import policy_path_key
from forge.domain.run import RunState
from forge.domain.subscription import (
    ExecutionEnvelope,
    HandoffStatus,
    LogicalTaskContract,
    SpecialistPurpose,
    TaskHandoff,
    decode_subscription_record,
    encode_subscription_record,
    is_read_only,
)
from forge.domain.subscription_delegation import validate_child_authority
from forge.domain.subscription_execution import run_allows_subscription_attempt
from forge.domain.subscription_task_controls import (
    AttemptTaskControlProof,
    IdleTaskControlProof,
    StoredTaskControl,
    SubscriptionTaskControlRequest,
    TaskControlConflict,
    TaskControlProof,
    TaskControlSource,
    TaskControlStatus,
)
from forge.persistence.models.execution import ToolCall
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionEnvelope,
    SubscriptionOperationBinding,
    SubscriptionTask,
    SubscriptionTaskDependency,
)
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.models.subscription_task_stops import SubscriptionTaskStop
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.runs import PostgresRunRepository
from forge.persistence.repositories.scheduling import PostgresSchedulingRepository
from forge.persistence.repositories.subscription import PostgresSubscriptionRepository
from forge.persistence.repositories.subscription_budget import PostgresSubscriptionBudgetRepository
from forge.persistence.repositories.subscription_task_stop_receipts import (
    load_control,
    pending_decision_task_version,
    settlement_matches,
    settlement_payload,
    stop_matches_receipt,
    verify_control_receipt,
)

if TYPE_CHECKING:
    from forge.persistence.repositories.subscription_resumption import _Source


class PostgresSubscriptionTaskControlRepository:
    def __init__(self, session: AsyncSession, *, scheduler: PostgresSchedulingRepository) -> None:
        self._session, self._scheduler = session, scheduler

    async def verify_receipt(
        self,
        stored: StoredTaskControl,
        *,
        actor_id: UUID,
        request_digest: str,
    ) -> None:
        await verify_control_receipt(
            self._session, stored, actor_id=actor_id, request_digest=request_digest
        )

    async def apply(
        self,
        run_id: UUID,
        task_id: UUID,
        request: SubscriptionTaskControlRequest,
        *,
        pause: StoredTaskControl | None,
        receipt_id: UUID,
    ) -> TaskControlTransition:
        run = await self._session.get(Run, run_id, with_for_update=True, populate_existing=True)
        if run is None or run.version != request.expected_run_version:
            raise TaskControlConflict("run version differs; refresh operator state")
        if run.state in ("COMPLETED", "CANCELLED", "FAILED"):
            raise TaskControlConflict("task control requires a nonterminal run")
        if request.action == "pause" and not run_allows_subscription_attempt(
            run.state, run.pending_gate
        ):
            raise TaskControlConflict(
                "run controls already stop execution; resume the run before pausing a task"
            )
        task = await self._session.get(
            SubscriptionTask, task_id, with_for_update=True, populate_existing=True
        )
        if task is None or task.run_id != run_id:
            raise TaskControlConflict("subscription task was not found in this run")
        contract = self._contract(task)
        if contract.purpose is SpecialistPurpose.PRIMARY:
            raise TaskControlConflict("use run controls for the primary coordinator")
        if task.version != request.expected_task_version:
            raise TaskControlConflict("task version differs; refresh operator state")
        scheduled = await self._session.get(
            SubscriptionScheduledTask, task_id, with_for_update=True, populate_existing=True
        )
        scheduler_run = await self._session.get(
            SubscriptionSchedulerRun, run_id, with_for_update=True, populate_existing=True
        )
        if scheduled is None or scheduler_run is None or not scheduler_run.admitted:
            raise TaskControlConflict("task is not in the subscription scheduler")
        proof = await self._proof(run, task, contract, scheduled, scheduler_run)
        attempt = await self._session.scalar(
            select(SubscriptionAttempt)
            .where(SubscriptionAttempt.task_row_id == task.id)
            .order_by(SubscriptionAttempt.attempt_number.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if attempt is not None:
            if (
                attempt.status == "terminal"
                and task.state in ("queued", "blocked")
                and not (pause is not None and isinstance(pause.proof, AttemptTaskControlProof))
            ):
                return await self._idle_control(
                    run, task, scheduled, attempt, proof, request, pause
                )
            return await self._attempt_control(
                run, task, scheduled, attempt, proof, request, pause, receipt_id
            )
        await self._never_attempted(task, scheduled)
        status: Literal["paused", "cancelled", "queued"]
        if request.action == "resume":
            if (
                pause is None
                or pause.receipt.task_version != task.version
                or pause.receipt.pause_receipt_id is not None
                or pause.receipt.run_id != run_id
                or pause.receipt.task_id != task_id
                or pause.proof != proof
                or not task.pause_requested
                or not scheduled.pause_requested
                or task.state != "blocked"
                or scheduled.state != "blocked"
            ):
                raise TaskControlConflict("current task does not match its pause receipt")
            if not run_allows_subscription_attempt(
                run.state, run.pending_gate
            ) or await PostgresCommandRepository(
                session=self._session
            ).has_pending_current_control_stop(run_id=run_id, expected_run_version=run.version):
                raise TaskControlConflict("run controls prevent task resume")
            task.pause_requested = scheduled.pause_requested = False
            task.state = scheduled.state = "queued"
            status = "queued"
        elif request.action == "pause":
            if task.pause_requested or scheduled.pause_requested:
                raise TaskControlConflict("task is already paused; use its existing receipt")
            task.pause_requested = scheduled.pause_requested = True
            task.state = scheduled.state = "blocked"
            status = "paused"
        else:
            task.cancel_requested = scheduled.cancel_requested = True
            task.state = "terminal"
            # Cancel reuses guarded parent wake; pause never terminalizes scheduling.
            await self._scheduler.request_stop(run_id, task_id, cancel=True)
            status = "cancelled"
        task.version += 1
        await self._session.flush()
        return TaskControlTransition(status, run.version, task.version, proof)

    async def _idle_control(
        self,
        run: Run,
        task: SubscriptionTask,
        scheduled: SubscriptionScheduledTask,
        attempt: SubscriptionAttempt,
        current: TaskControlProof,
        request: SubscriptionTaskControlRequest,
        pause: StoredTaskControl | None,
    ) -> TaskControlTransition:
        from forge.persistence.repositories.subscription_idle_task_controls import (
            idle_history_digest,
        )

        source = await self._retained_source(run, attempt.id, pending=False, settled_idle=True)
        history = await idle_history_digest(self._session, source)
        proof = IdleTaskControlProof(
            **current.model_dump(),
            source_attempt_id=attempt.id,
            history_digest=history,
            idle_state=cast(Literal["queued", "blocked"], task.state),
        )
        status: TaskControlStatus
        if request.action == "resume":
            if (
                pause is None
                or not isinstance(pause.proof, IdleTaskControlProof)
                or pause.receipt.task_version != task.version
                or pause.proof != proof.model_copy(update={"idle_state": pause.proof.idle_state})
                or not task.pause_requested
                or not scheduled.pause_requested
                or task.state != "blocked"
                or not run_allows_subscription_attempt(run.state, run.pending_gate)
                or await PostgresCommandRepository(
                    session=self._session
                ).has_pending_current_control_stop(run_id=run.id, expected_run_version=run.version)
            ):
                raise TaskControlConflict("idle task differs from its pause receipt")
            proof = pause.proof
            task.pause_requested = scheduled.pause_requested = False
            task.state = scheduled.state = proof.idle_state
            status = proof.idle_state
        elif request.action == "pause":
            if task.pause_requested or scheduled.pause_requested:
                raise TaskControlConflict("task is already paused; use its existing receipt")
            task.pause_requested = scheduled.pause_requested = True
            task.state = scheduled.state = "blocked"
            status = "paused"
        else:
            task.cancel_requested = scheduled.cancel_requested = True
            task.state = "terminal"
            await self._scheduler.request_stop(run.id, task.id, cancel=True)
            status = "cancelled"
        task.version += 1
        await self._session.flush()
        return TaskControlTransition(status, run.version, task.version, proof)

    async def _attempt_control(
        self,
        run: Run,
        task: SubscriptionTask,
        scheduled: SubscriptionScheduledTask,
        attempt: SubscriptionAttempt,
        proof: TaskControlProof,
        request: SubscriptionTaskControlRequest,
        pause: StoredTaskControl | None,
        receipt_id: UUID,
    ) -> TaskControlTransition:
        if request.action == "resume":
            return await self._resume_attempt(
                run, task, scheduled, attempt, proof, pause, receipt_id
            )
        if task.cancel_requested or scheduled.cancel_requested or task.state == "terminal":
            raise TaskControlConflict("task cancellation or terminal state prevents this control")
        if task.pause_requested or scheduled.pause_requested:
            if request.action == "cancel":
                return await self._cancel_paused(run, task, scheduled, attempt, proof, receipt_id)
            raise TaskControlConflict("task is already paused; use its existing receipt")
        envelope = await PostgresSubscriptionRepository(self._session).envelope_for_run(run.id)
        if (
            attempt.run_id != run.id
            or attempt.task_row_id != task.id
            or attempt.task_version is None
            or attempt.lease_owner is None
            or attempt.lease_generation is None
            or attempt.envelope_digest != canonical_digest(encode_subscription_record(envelope))
            or attempt.task_digest != proof.task_digest
            or attempt.route_payload != encode_subscription_record(self._contract(task).route)
            or attempt.candidate_epoch != proof.candidate_epoch
            or (scheduled.lease_owner, scheduled.lease_generation)
            != (attempt.lease_owner, attempt.lease_generation)
        ):
            raise TaskControlConflict("task attempt authority differs")
        result = await self._session.get(
            SubscriptionAttemptResult, attempt.id, with_for_update=True
        )
        if result is None:
            if (
                task.state != "running"
                or scheduled.state not in ("leased", "reconciling")
                or attempt.status != "running"
                or task.version != attempt.task_version
            ):
                raise TaskControlConflict("task requires stopped-attempt recovery")
            source = TaskControlSource(
                kind="active",
                attempt_id=attempt.id,
                admission_version=attempt.task_version,
                task_version=task.version,
                lease_owner=attempt.lease_owner,
                lease_generation=attempt.lease_generation,
            )
        else:
            pending = result.disposition == "decision_pending"
            retained = await self._retained_source(run, attempt.id, pending=pending)
            expected = (
                await pending_decision_task_version(self._session, attempt, result)
                if pending
                else attempt.task_version + 1
            )
            if (
                task.state != "reconciling"
                or scheduled.state != "reconciling"
                or attempt.status != "reconciling"
                or task.version != expected
                or result.application_payload is not None
                or result.application_digest is not None
            ):
                raise TaskControlConflict("retained task source is no longer current")
            if pending:
                from forge.persistence.repositories.subscription_resumption import (
                    _pending_decision_is_admissible,
                )

                snapshot = await PostgresRunRepository(self._session).get(run.id)
                phase = RunState(run.state)
                if request.action == "cancel" and phase is RunState.PAUSED and run.suspended_state:
                    phase = RunState(run.suspended_state)
                if not _pending_decision_is_admissible(retained, snapshot, phase=phase):
                    raise TaskControlConflict("retained decision cannot be resumed")
            source = TaskControlSource(
                kind="pending" if pending else "stale",
                attempt_id=attempt.id,
                admission_version=attempt.task_version,
                task_version=task.version,
                lease_owner=attempt.lease_owner,
                lease_generation=attempt.lease_generation,
                result_digest=result.result_digest,
            )
        bound = AttemptTaskControlProof(**proof.model_dump(), source=source)
        task.pause_requested = scheduled.pause_requested = request.action == "pause"
        task.cancel_requested = scheduled.cancel_requested = request.action == "cancel"
        task.version += 1
        stop = SubscriptionTaskStop(
            id=receipt_id,
            run_id=run.id,
            task_id=task.id,
            attempt_id=attempt.id,
            stop_task_version=task.version,
            lease_generation=scheduled.lease_generation,
            state="requested",
        )
        self._session.add(stop)
        status: TaskControlStatus = (
            "pause_requested" if request.action == "pause" else "cancel_requested"
        )
        if result is not None:
            await self._mark_stopped(
                stop, bound, result, task, scheduled, attempt, cancel=request.action == "cancel"
            )
            status = "paused" if request.action == "pause" else "cancelled"
        await self._session.flush()
        return TaskControlTransition(status, run.version, task.version, bound)

    async def _cancel_paused(
        self,
        run: Run,
        task: SubscriptionTask,
        scheduled: SubscriptionScheduledTask,
        attempt: SubscriptionAttempt,
        current: TaskControlProof,
        receipt_id: UUID,
    ) -> TaskControlTransition:
        previous = await self._session.scalar(
            select(SubscriptionTaskStop)
            .where(SubscriptionTaskStop.attempt_id == attempt.id)
            .order_by(SubscriptionTaskStop.stop_task_version.desc())
            .limit(1)
            .with_for_update()
        )
        if previous is None or previous.state not in ("requested", "paused"):
            raise TaskControlConflict("task pause authority is unavailable")
        pause = await load_control(self._session, previous.id, run.id, task.id)
        old_proof = pause.proof
        comparable = current
        if run.state == "PAUSED" and run.suspended_state == old_proof.run_state.value:
            comparable = current.model_copy(update={"run_state": old_proof.run_state})
        if (
            not isinstance(old_proof, AttemptTaskControlProof)
            or not stop_matches_receipt(previous, pause)
            or pause.receipt.action != "pause"
            or comparable.model_dump() != old_proof.model_dump(exclude={"source"})
            or not task.pause_requested
            or not scheduled.pause_requested
        ):
            raise TaskControlConflict("task pause authority differs")
        result = await self._session.get(
            SubscriptionAttemptResult, attempt.id, with_for_update=True
        )
        if result is None:
            if (
                old_proof.source.kind != "active"
                or previous.state != "requested"
                or task.version != previous.stop_task_version
                or task.state != "running"
                or scheduled.state not in ("leased", "reconciling")
                or attempt.status != "running"
                or (attempt.lease_owner, attempt.lease_generation, attempt.task_version)
                != (
                    old_proof.source.lease_owner,
                    old_proof.source.lease_generation,
                    old_proof.source.admission_version,
                )
                or (scheduled.lease_owner, scheduled.lease_generation)
                != (attempt.lease_owner, attempt.lease_generation)
            ):
                raise TaskControlConflict("task pause is awaiting different work")
            kind: Literal["active", "pending", "stale"] = "active"
        else:
            if previous.state == "requested":
                await self.reconcile_stop(run.id, previous.id)
            source = await self._retained_source(
                run, attempt.id, pending=old_proof.source.kind == "pending"
            )
            await self._check_stopped_current(run, source, previous, old_proof, cancel=False)
            if not settlement_matches(previous, old_proof, result):
                raise TaskControlConflict("task pause settlement differs")
            kind = "pending" if old_proof.source.kind == "pending" else "stale"
        bound = AttemptTaskControlProof(
            **current.model_dump(),
            source=TaskControlSource(
                kind=kind,
                attempt_id=attempt.id,
                admission_version=old_proof.source.admission_version,
                task_version=task.version,
                lease_owner=old_proof.source.lease_owner,
                lease_generation=old_proof.source.lease_generation,
                result_digest=result.result_digest if result is not None else None,
                previous_stop_receipt_id=previous.id,
            ),
        )
        task.pause_requested = scheduled.pause_requested = False
        task.cancel_requested = scheduled.cancel_requested = True
        task.version += 1
        stop = SubscriptionTaskStop(
            id=receipt_id,
            run_id=run.id,
            task_id=task.id,
            attempt_id=attempt.id,
            stop_task_version=task.version,
            lease_generation=scheduled.lease_generation,
            state="requested",
        )
        self._session.add(stop)
        previous.state, previous.superseding_receipt_id = "superseded", receipt_id
        if result is not None:
            await self._mark_stopped(stop, bound, result, task, scheduled, attempt, cancel=True)
        await self._session.flush()
        return TaskControlTransition(
            "cancelled" if result is not None else "cancel_requested",
            run.version,
            task.version,
            bound,
        )

    async def _retained_source(
        self, run: Run, attempt_id: UUID, *, pending: bool, settled_idle: bool = False
    ) -> _Source:
        from forge.application.ports.commands import CommandRecoveryRequired
        from forge.persistence.repositories.subscription_resumption import _source

        try:
            snapshot = await PostgresRunRepository(self._session).get(run.id)
            source = await _source(
                self._session,
                snapshot,
                attempt_id,
                historical=True,
                pending_decision=pending,
                allow_fenced=not pending and not settled_idle,
                settled_idle=settled_idle,
            )
            if (
                await self._session.scalar(
                    select(ToolCall.id)
                    .where(
                        ToolCall.run_id == run.id,
                        ToolCall.subscription_task_id == source.task.id,
                        ToolCall.status.in_(("PENDING", "RUNNING")),
                    )
                    .limit(1)
                )
                is not None
                or await self._session.scalar(
                    select(SubscriptionOperationBinding.id)
                    .where(
                        SubscriptionOperationBinding.attempt_id == attempt_id,
                        SubscriptionOperationBinding.receipt_payload.is_(None),
                    )
                    .limit(1)
                )
                is not None
                or await self._session.scalar(
                    select(SubscriptionScheduledEffect.id)
                    .where(
                        SubscriptionScheduledEffect.run_id == run.id,
                        SubscriptionScheduledEffect.task_id == source.task.id,
                        SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
                    )
                    .limit(1)
                )
                is not None
                or await self._session.scalar(
                    select(SubscriptionAttempt.id)
                    .where(
                        SubscriptionAttempt.task_row_id == source.task.id,
                        SubscriptionAttempt.id != attempt_id,
                        SubscriptionAttempt.status != "terminal",
                    )
                    .limit(1)
                )
                is not None
            ):
                raise CommandRecoveryRequired("task work requires reconciliation")
            return source
        except CommandRecoveryRequired:
            raise TaskControlConflict(
                "task client, result or effects still require reconciliation"
            ) from None

    async def _mark_stopped(
        self,
        stop: SubscriptionTaskStop,
        proof: AttemptTaskControlProof,
        result: SubscriptionAttemptResult,
        task: SubscriptionTask,
        scheduled: SubscriptionScheduledTask,
        attempt: SubscriptionAttempt,
        *,
        cancel: bool,
    ) -> None:
        stop.state = "cancelled" if cancel else "paused"
        stop.settled_task_version = task.version
        stop.settlement_payload = settlement_payload(stop, proof, result)
        stop.settlement_digest = canonical_digest(stop.settlement_payload)
        if cancel:
            task.state = attempt.status = "terminal"
            result.disposition = "task_cancelled"
            result.application_payload = {
                "kind": "operator_task_cancelled",
                "stop_receipt_id": str(stop.id),
                "settlement_digest": stop.settlement_digest,
            }
            result.application_digest = canonical_digest(result.application_payload)
            await self._scheduler.reconcile_expired(stop.run_id, task.id, retry=False)

    async def pending_stops(
        self, after_id: UUID | None, limit: int
    ) -> tuple[tuple[UUID, UUID], ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("task stop page size must be 1 to 100")
        query = select(SubscriptionTaskStop.run_id, SubscriptionTaskStop.id).where(
            SubscriptionTaskStop.state == "requested"
        )
        if after_id is not None:
            query = query.where(SubscriptionTaskStop.id > after_id)
        return tuple(
            (run_id, receipt_id)
            for run_id, receipt_id in (
                await self._session.execute(query.order_by(SubscriptionTaskStop.id).limit(limit))
            ).all()
        )

    async def reconcile_stop(self, run_id: UUID, receipt_id: UUID) -> bool | None:
        run = await self._session.get(
            Run, run_id, with_for_update={"skip_locked": True}, populate_existing=True
        )
        if run is None:
            return None
        stop = await self._session.get(
            SubscriptionTaskStop, receipt_id, with_for_update=True, populate_existing=True
        )
        if stop is None or stop.run_id != run_id:
            raise TaskControlConflict("task stop was not found in this run")
        stored = await load_control(self._session, stop.id, run_id, stop.task_id)
        if not stop_matches_receipt(stop, stored):
            raise TaskControlConflict("task stop receipt binding differs")
        if stop.state != "requested":
            return False
        proof = stored.proof
        assert isinstance(proof, AttemptTaskControlProof)
        source = await self._retained_source(
            run, stop.attempt_id, pending=proof.source.kind == "pending"
        )
        await self._check_stopped_current(
            run, source, stop, proof, cancel=stored.receipt.action == "cancel"
        )
        await self._mark_stopped(
            stop,
            proof,
            source.result,
            source.task,
            source.scheduled,
            source.attempt,
            cancel=stored.receipt.action == "cancel",
        )
        await self._session.flush()
        return True

    async def _check_stopped_current(
        self,
        run: Run,
        source: _Source,
        stop: SubscriptionTaskStop,
        proof: AttemptTaskControlProof,
        *,
        cancel: bool,
    ) -> None:
        task, scheduled, attempt, result = (
            source.task,
            source.scheduled,
            source.attempt,
            source.result,
        )
        if proof.source.previous_stop_receipt_id is not None:
            previous = await self._session.get(
                SubscriptionTaskStop, proof.source.previous_stop_receipt_id
            )
            previous_receipt = await load_control(
                self._session, proof.source.previous_stop_receipt_id, run.id, task.id
            )
            if (
                previous is None
                or not stop_matches_receipt(previous, previous_receipt)
                or previous_receipt.receipt.action != "pause"
                or previous.state != "superseded"
                or previous.superseding_receipt_id != stop.id
                or previous.attempt_id != stop.attempt_id
                or previous.lease_generation != stop.lease_generation
                or previous.stop_task_version >= stop.stop_task_version
                or not cancel
            ):
                raise TaskControlConflict("superseded task stop differs")
        current = await self._proof(run, task, source.contract, scheduled, source.scheduler)
        if run.state == "PAUSED" and run.suspended_state == proof.run_state.value:
            current = current.model_copy(update={"run_state": proof.run_state})
        if (
            current.model_dump() != proof.model_dump(exclude={"source"})
            or attempt.id != proof.source.attempt_id
            or attempt.task_version != proof.source.admission_version
            or (scheduled.lease_owner, scheduled.lease_generation)
            != (proof.source.lease_owner, proof.source.lease_generation)
            or (attempt.lease_owner, attempt.lease_generation)
            != (proof.source.lease_owner, proof.source.lease_generation)
            or task.state != "reconciling"
            or scheduled.state != "reconciling"
            or attempt.status != "reconciling"
            or task.version != stop.stop_task_version + int(proof.source.kind == "active")
            or task.cancel_requested != cancel
            or scheduled.cancel_requested != cancel
            or task.pause_requested == cancel
            or scheduled.pause_requested == cancel
            or result.application_payload is not None
            or result.application_digest is not None
            or (
                proof.source.result_digest is not None
                and proof.source.result_digest != result.result_digest
            )
        ):
            raise TaskControlConflict("stopped task no longer matches its operator control")

    async def _resume_attempt(
        self,
        run: Run,
        task: SubscriptionTask,
        scheduled: SubscriptionScheduledTask,
        attempt: SubscriptionAttempt,
        current: TaskControlProof,
        pause: StoredTaskControl | None,
        receipt_id: UUID,
    ) -> TaskControlTransition:
        if pause is None or not isinstance(pause.proof, AttemptTaskControlProof):
            raise TaskControlConflict("attempted task requires its exact pause receipt")
        proof = pause.proof
        if (
            not run_allows_subscription_attempt(run.state, run.pending_gate)
            or await PostgresCommandRepository(
                session=self._session
            ).has_pending_current_control_stop(run_id=run.id, expected_run_version=run.version)
            or current.model_dump() != proof.model_dump(exclude={"source"})
            or attempt.id != proof.source.attempt_id
        ):
            raise TaskControlConflict("run or task authority prevents resume")
        stop = await self._session.get(
            SubscriptionTaskStop,
            pause.receipt.receipt_id,
            with_for_update=True,
            populate_existing=True,
        )
        if stop is None or not stop_matches_receipt(stop, pause):
            raise TaskControlConflict("task pause lifecycle differs")
        if stop.state == "requested":
            await self.reconcile_stop(run.id, stop.id)
        source = await self._retained_source(
            run, attempt.id, pending=proof.source.kind == "pending"
        )
        await self._check_stopped_current(run, source, stop, proof, cancel=False)
        if (
            stop.state != "paused"
            or task.version != stop.settled_task_version
            or not settlement_matches(stop, proof, source.result)
        ):
            raise TaskControlConflict("task pause has not been proved settled")
        pending = proof.source.kind == "pending"
        if not pending:
            from forge.application.ports.commands import CommandRecoveryRequired
            from forge.persistence.repositories.subscription_resumption import (
                quota_deferral_observation,
            )

            try:
                quota_observation = await quota_deferral_observation(self._session, source)
            except CommandRecoveryRequired, TypeError, ValueError:
                raise TaskControlConflict("stopped quota evidence differs") from None
            if quota_observation is None and (
                scheduled.repairs >= scheduled.max_repairs
                or await self._session.get(SubscriptionRepairDebit, attempt.id) is not None
                or not await PostgresSubscriptionBudgetRepository(self._session).try_debit_repair(
                    run.id, task.id, attempt.id
                )
            ):
                raise TaskControlConflict("stopped task resume requires remaining repair budget")
            handoff = TaskHandoff(
                run_id=run.id,
                task_id=task.id,
                attempt_id=attempt.id,
                status=HandoffStatus.FAILED,
                summary="The invocation stopped under an operator task control. Its result remains unaccepted. Inspect the existing work within the unchanged contract, then continue with fresh evidence. "
                f"Stopped attempt: {attempt.id}; result: {source.result.result_digest}.",
            )
            await PostgresSubscriptionRepository(self._session).record_decision(
                handoff, idempotency_key=f"task-resume:{receipt_id}:{attempt.id}"
            )
            if quota_observation is None:
                scheduled.repairs += 1
            task.state = scheduled.state = "queued"
            scheduled.lease_owner = scheduled.lease_expires_at = None
            attempt.status = "terminal"
            source.result.application_payload = {
                "kind": "operator_task_resumed",
                "resume_receipt_id": str(receipt_id),
                "stop_receipt_id": str(stop.id),
                "settlement_digest": stop.settlement_digest,
                "handoff_digest": canonical_digest(encode_subscription_record(handoff)),
                "repairs": scheduled.repairs,
            }
            if quota_observation is not None:
                source.result.application_payload["quota_observation_id"] = str(quota_observation)
            source.result.application_digest = canonical_digest(source.result.application_payload)
        task.pause_requested = scheduled.pause_requested = False
        task.version += 1
        stop.state, stop.resume_receipt_id, stop.resumed_task_version = (
            "resumed",
            receipt_id,
            task.version,
        )
        await self._session.flush()
        return TaskControlTransition(
            "decision_pending" if pending else "queued", run.version, task.version, proof
        )

    async def _never_attempted(
        self, task: SubscriptionTask, scheduled: SubscriptionScheduledTask
    ) -> None:
        if (
            task.cancel_requested
            or scheduled.cancel_requested
            or task.state not in ("queued", "blocked")
            or scheduled.state not in ("queued", "blocked")
            or scheduled.run_id != task.run_id
            or scheduled.task_id != task.id
            or scheduled.lease_owner is not None
            or scheduled.lease_expires_at is not None
            or scheduled.lease_generation != 0
            or scheduled.repairs != 0
        ):
            raise TaskControlConflict("task requires stopped-attempt recovery or is terminal")
        attempt = await self._session.scalar(
            select(SubscriptionAttempt.id)
            .where(SubscriptionAttempt.task_row_id == task.id)
            .limit(1)
        )
        effect = await self._session.scalar(
            select(SubscriptionScheduledEffect.id)
            .where(SubscriptionScheduledEffect.task_id == task.id)
            .limit(1)
        )
        if attempt is not None or effect is not None:
            raise TaskControlConflict("task requires stopped-attempt recovery")

    @staticmethod
    def _contract(task: SubscriptionTask) -> LogicalTaskContract:
        try:
            contract = decode_subscription_record(task.payload)
            if (
                not isinstance(contract, LogicalTaskContract)
                or (contract.run_id, contract.task_id, contract.parent_task_id)
                != (task.run_id, task.id, task.parent_task_id)
                or task.task_id != task.id
            ):
                raise ValueError
            return contract
        except TypeError, ValueError:
            raise TaskControlConflict("task contract identity differs") from None

    async def _proof(
        self,
        run: Run,
        task: SubscriptionTask,
        contract: LogicalTaskContract,
        scheduled: SubscriptionScheduledTask,
        scheduler_run: SubscriptionSchedulerRun,
    ) -> TaskControlProof:
        parent = await self._session.get(
            SubscriptionTask, task.parent_task_id, populate_existing=True
        )
        envelope_row = await self._session.get(
            SubscriptionEnvelope, task.run_id, populate_existing=True
        )
        try:
            if parent is None or envelope_row is None:
                raise ValueError
            parent_contract = self._contract(parent)
            envelope = decode_subscription_record(envelope_row.payload)
            if (
                parent_contract.purpose is not SpecialistPurpose.PRIMARY
                or parent.run_id != task.run_id
                or not isinstance(envelope, ExecutionEnvelope)
                or envelope.run_id != run.id
                or envelope.safety_policy_version != run.policy_version
                or not envelope.permits_route(contract.purpose, contract.route)
                or scheduled.parent_task_id != contract.parent_task_id
                or tuple(scheduled.owned_paths)
                != tuple(policy_path_key(path) for path in contract.owned_paths)
                or tuple(scheduled.dependency_task_ids) != contract.dependency_task_ids
                or scheduled.provider != contract.route.effective.provider
                or scheduled.read_only != is_read_only(contract.purpose)
                or scheduled.max_repairs != contract.max_repairs
                or not scheduled.worktree_id.strip()
            ):
                raise ValueError
            # The exact approved fallback was checked above; retain the remaining
            # delegation checks against the envelope's original preferred binding.
            validate_child_authority(
                parent_contract,
                replace(contract, route=envelope.route_for(contract.purpose)),
                envelope,
            )
            dependencies = set(
                await self._session.scalars(
                    select(SubscriptionTaskDependency.dependency_task_id).where(
                        SubscriptionTaskDependency.run_id == task.run_id,
                        SubscriptionTaskDependency.task_id == task.id,
                    )
                )
            )
            if dependencies != set(contract.dependency_task_ids):
                raise ValueError
            return TaskControlProof(
                task_digest=canonical_digest(task.payload),
                parent_digest=canonical_digest(parent.payload),
                envelope_digest=canonical_digest(envelope_row.payload),
                scheduling_digest=canonical_digest(
                    {
                        "worktree_id": scheduled.worktree_id,
                        "provider": scheduled.provider,
                        "owned_paths": list(scheduled.owned_paths),
                        "dependencies": [str(value) for value in scheduled.dependency_task_ids],
                        "parent_task_id": str(scheduled.parent_task_id),
                        "read_only": scheduled.read_only,
                        "max_repairs": scheduled.max_repairs,
                        "repairs": scheduled.repairs,
                        "lease_generation": scheduled.lease_generation,
                    }
                ),
                run_state=RunState(run.state),
                candidate_epoch=scheduler_run.candidate_epoch,
                candidate_state=cast(
                    Literal["open", "draining", "closed"], scheduler_run.candidate_state
                ),
            )
        except KeyError, TypeError, ValueError:
            raise TaskControlConflict(
                "task control contract, envelope or scheduling proof differs"
            ) from None


async def paused_task_quiescence_exemptions(
    session: AsyncSession, run_id: UUID
) -> tuple[tuple[UUID, UUID], ...]:
    """Prove physical task stops independently of whole-run resumption authority."""
    from forge.persistence.repositories.mutations import MutationRepositoryError

    run = await session.get(Run, run_id, with_for_update=True, populate_existing=True)
    if run is None:
        return ()
    repository = PostgresSubscriptionTaskControlRepository(
        session, scheduler=PostgresSchedulingRepository(session)
    )
    stops = await session.scalars(
        select(SubscriptionTaskStop)
        .where(SubscriptionTaskStop.run_id == run_id, SubscriptionTaskStop.state == "paused")
        .order_by(SubscriptionTaskStop.task_id, SubscriptionTaskStop.stop_task_version)
    )
    proven = []
    for stop in stops:
        try:
            pause = await load_control(session, stop.id, run_id, stop.task_id)
            proof = pause.proof
            if (
                not isinstance(proof, AttemptTaskControlProof)
                or not stop_matches_receipt(stop, pause)
                or pause.receipt.action != "pause"
            ):
                continue
            source = await repository._retained_source(
                run, stop.attempt_id, pending=proof.source.kind == "pending"
            )
            await repository._check_stopped_current(run, source, stop, proof, cancel=False)
            if source.task.version == stop.settled_task_version and settlement_matches(
                stop, proof, source.result
            ):
                proven.append((stop.attempt_id, stop.task_id))
        except TaskControlConflict, MutationRepositoryError, TypeError, ValueError:
            # A malformed stop remains counted; valid stops elsewhere stay independent.
            continue
    return tuple(proven)
