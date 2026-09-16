"""Short PostgreSQL transactions for frozen attempt admission."""

import hashlib
import json
from dataclasses import replace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_execution import (
    SubscriptionAdmission,
    SubscriptionInvocationContext,
    SubscriptionResumption,
    SubscriptionSettlement,
)
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.tools import _safe_metadata
from forge.domain.command import CommandEnvelope
from forge.domain.operation import canonical_digest
from forge.domain.payload import validate_durable_payload
from forge.domain.plan import PlanOutput
from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.run import RunState
from forge.domain.scheduling import TaskLease
from forge.domain.subscription import (
    AttemptIdentity,
    HandoffStatus,
    LogicalTaskContract,
    TaskHandoff,
    decode_subscription_record,
    encode_subscription_record,
    is_read_only,
    subscription_record_fingerprint,
)
from forge.domain.subscription_execution import run_allows_subscription_attempt
from forge.observability.redaction import Redactor
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.runs import PostgresRunRepository
from forge.persistence.repositories.scheduling import PostgresSchedulingRepository
from forge.persistence.repositories.subscription import (
    PostgresSubscriptionRepository,
    SubscriptionConflict,
)
from forge.persistence.repositories.subscription_budget import PostgresSubscriptionBudgetRepository
from forge.persistence.repositories.subscription_feedback import (
    PostgresSubscriptionFeedbackRepository,
)
from forge.persistence.repositories.subscription_launch import launches_confirmed
from forge.persistence.repositories.subscription_quota import (
    PostgresSubscriptionQuotaRepository,
    exhaustion_payload,
)


def _matches_admission(attempt: SubscriptionAttempt, admission: SubscriptionAdmission) -> bool:
    identity, lease = admission.attempt, admission.lease
    return (
        (identity.run_id, identity.task_id) == (lease.run_id, lease.task_id)
        and attempt.run_id == identity.run_id
        and attempt.task_row_id == identity.task_id
        and attempt.attempt_number == identity.attempt_number
        and attempt.lease_owner == lease.owner
        and attempt.lease_generation == lease.generation
        and attempt.candidate_epoch == admission.candidate_epoch
        and attempt.task_version == admission.task_version
        and attempt.envelope_digest
        == canonical_digest(encode_subscription_record(admission.envelope))
        and attempt.task_digest == canonical_digest(encode_subscription_record(admission.task))
        and attempt.route_payload == encode_subscription_record(admission.task.route)
    )


class PostgresSubscriptionExecutionRepository:
    def __init__(
        self, session: AsyncSession, *, quota: PostgresSubscriptionQuotaRepository | None = None
    ):
        self._session = session
        self._quota = quota or PostgresSubscriptionQuotaRepository(session)

    async def resume_paused_attempts(
        self, resume: CommandEnvelope, pause: CommandEnvelope
    ) -> tuple[SubscriptionResumption, ...]:
        from forge.persistence.repositories.subscription_resumption import resume_paused_attempts

        return await resume_paused_attempts(self._session, resume, pause)

    async def verify_paused_attempts(
        self, resume: CommandEnvelope
    ) -> tuple[SubscriptionResumption, ...]:
        from forge.persistence.repositories.subscription_resumption import verify_paused_attempts

        return await verify_paused_attempts(self._session, resume)

    async def invocation_context(
        self, admission: SubscriptionAdmission
    ) -> SubscriptionInvocationContext:
        """Check current authority under the run lock before assembling a request.

        This returns a resource identity and candidate state, never a filesystem path. The caller
        keeps this transaction short; launch and tool fences still revalidate
        after it commits.
        """
        if not isinstance(admission, SubscriptionAdmission):
            raise TypeError("typed admission required")
        lease = admission.lease
        run = await PostgresRunRepository(self._session).get_for_update(lease.run_id)
        if not run_allows_subscription_attempt(run.state, run.pending_gate) or (
            await PostgresCommandRepository(session=self._session).has_pending_current_control_stop(
                run_id=run.id, expected_run_version=run.version
            )
        ):
            raise SubscriptionConflict("invocation run is stopped")
        attempt = await self._session.get(
            SubscriptionAttempt,
            admission.attempt.attempt_id,
            with_for_update=True,
            populate_existing=True,
        )
        if (
            attempt is None
            or not _matches_admission(attempt, admission)
            or attempt.status != "running"
        ):
            raise SubscriptionConflict("invocation admission binding differs")
        task = await self._session.get(
            SubscriptionTask, lease.task_id, with_for_update=True, populate_existing=True
        )
        scheduled = await self._session.scalar(
            select(SubscriptionScheduledTask)
            .where(
                SubscriptionScheduledTask.run_id == lease.run_id,
                SubscriptionScheduledTask.task_id == lease.task_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        scheduler_run = await self._session.get(
            SubscriptionSchedulerRun, lease.run_id, with_for_update=True, populate_existing=True
        )
        envelope = await PostgresSubscriptionRepository(self._session).envelope_for_run(run.id)
        if (
            task is None
            or task.run_id != run.id
            or task.state != "running"
            or task.version != admission.task_version
            or task.payload != encode_subscription_record(admission.task)
            or task.pause_requested
            or task.cancel_requested
            or scheduled is None
            or scheduled.state != "leased"
            or tuple(scheduled.owned_paths) != admission.task.owned_paths
            or tuple(scheduled.dependency_task_ids) != admission.task.dependency_task_ids
            or scheduled.parent_task_id != admission.task.parent_task_id
            or scheduled.provider != admission.task.route.effective.provider
            or scheduled.read_only != is_read_only(admission.task.purpose)
            or scheduled.lease_owner != lease.owner
            or scheduled.lease_generation != lease.generation
            or scheduled.lease_expires_at is None
            or scheduled.lease_expires_at <= self._quota.now()
            or scheduled.pause_requested
            or scheduled.cancel_requested
            or scheduler_run is None
            or not scheduler_run.admitted
            or scheduler_run.candidate_epoch != admission.candidate_epoch
            or envelope != admission.envelope
            or run.policy_version != admission.envelope.safety_policy_version
        ):
            raise SubscriptionConflict("invocation current context differs")
        pending = await self._session.scalar(
            select(SubscriptionScheduledEffect.id)
            .where(
                SubscriptionScheduledEffect.run_id == run.id,
                SubscriptionScheduledEffect.task_id == lease.task_id,
                SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
            )
            .limit(1)
        )
        if pending is not None:
            raise SubscriptionConflict("invocation has unresolved effects")
        await self._require_candidate_admission(scheduler_run, scheduled)
        return SubscriptionInvocationContext(
            scheduled.worktree_id, scheduler_run.candidate_state == "closed"
        )

    async def _require_candidate_admission(
        self, scheduler: SubscriptionSchedulerRun, task: SubscriptionScheduledTask
    ) -> None:
        if not scheduler.admitted or (
            scheduler.candidate_state != "open"
            and not (
                scheduler.candidate_state == "closed"
                and await PostgresSchedulingRepository(self._session)._can_read_candidate(task)
            )
        ):
            raise SubscriptionConflict("candidate barrier denies invocation")

    async def admit(self, lease: TaskLease, attempt_id: UUID) -> SubscriptionAdmission:
        await PostgresRunRepository(self._session).get_for_update(lease.run_id)
        subscription = PostgresSubscriptionRepository(self._session)
        envelope = await subscription.envelope_for_run(lease.run_id)
        task = await self._session.scalar(
            select(SubscriptionTask)
            .where(SubscriptionTask.run_id == lease.run_id, SubscriptionTask.id == lease.task_id)
            .with_for_update()
        )
        scheduled = await self._session.scalar(
            select(SubscriptionScheduledTask)
            .where(
                SubscriptionScheduledTask.run_id == lease.run_id,
                SubscriptionScheduledTask.task_id == lease.task_id,
            )
            .with_for_update()
        )
        scheduler_run = await self._session.get(
            SubscriptionSchedulerRun, lease.run_id, with_for_update=True
        )
        if envelope is None or task is None or scheduled is None or scheduler_run is None:
            raise SubscriptionConflict("execution admission lacks durable context")
        await self._require_candidate_admission(scheduler_run, scheduled)
        if (
            scheduled.state != "leased"
            or scheduled.lease_owner != lease.owner
            or scheduled.lease_generation != lease.generation
            or scheduled.lease_expires_at is None
            or scheduled.lease_expires_at <= self._quota.now()
            or scheduled.pause_requested
            or scheduled.cancel_requested
            or task.pause_requested
            or task.cancel_requested
        ):
            raise SubscriptionConflict("execution admission lease is revoked")
        pending = await self._session.scalar(
            select(SubscriptionScheduledEffect.id)
            .where(
                SubscriptionScheduledEffect.run_id == lease.run_id,
                SubscriptionScheduledEffect.task_id == lease.task_id,
                SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
            )
            .limit(1)
        )
        if pending is not None:
            raise SubscriptionConflict("execution admission has unresolved effects")
        contract = decode_subscription_record(task.payload)
        if (
            not isinstance(contract, LogicalTaskContract)
            or contract.run_id != lease.run_id
            or contract.task_id != lease.task_id
        ):
            raise SubscriptionConflict("execution task identity differs")
        previous = (
            await self._session.scalars(
                select(SubscriptionAttempt)
                .where(SubscriptionAttempt.task_row_id == lease.task_id)
                .order_by(SubscriptionAttempt.attempt_number)
                .with_for_update()
            )
        ).all()
        consumed = (
            set(
                (
                    await self._session.scalars(
                        select(SubscriptionAttemptConsumption.attempt_id).where(
                            SubscriptionAttemptConsumption.attempt_id.in_(
                                [row.id for row in previous]
                            )
                        )
                    )
                ).all()
            )
            if previous
            else set()
        )
        if any(
            row.status != "terminal" or row.lease_owner is None or row.id not in consumed
            for row in previous
        ):
            raise SubscriptionConflict("previous attempt requires reconciliation")
        if task.state != "queued":
            raise SubscriptionConflict("execution task is not queued")
        if not await self._quota.eligible(contract.route.effective):
            raise SubscriptionConflict("provider quota pool is blocked")
        attempt = AttemptIdentity(
            run_id=lease.run_id,
            task_id=lease.task_id,
            attempt_id=attempt_id,
            attempt_number=1 + max((row.attempt_number for row in previous), default=0),
        )
        await subscription.create_attempt(
            attempt, route_payload=contract.route, idempotency_key=f"lease:{lease.generation}"
        )
        row = await self._session.get(SubscriptionAttempt, attempt_id)
        if row is None:
            raise SubscriptionConflict("admitted attempt is missing")
        row.status = "running"
        row.lease_owner, row.lease_generation = lease.owner, lease.generation
        row.candidate_epoch = scheduler_run.candidate_epoch
        row.envelope_digest = canonical_digest(encode_subscription_record(envelope))
        row.task_digest = canonical_digest(encode_subscription_record(contract))
        task.state = "running"
        task.version += 1
        row.task_version = task.version
        await self._session.flush()
        await self._quota.admit(contract.route.effective, attempt_id)
        return SubscriptionAdmission(
            lease, contract, attempt, envelope, scheduler_run.candidate_epoch, task.version
        )

    async def settle(
        self, admission: SubscriptionAdmission, result: SubscriptionInvocationResult
    ) -> SubscriptionSettlement:
        if not isinstance(admission, SubscriptionAdmission) or not isinstance(
            result, SubscriptionInvocationResult
        ):
            raise TypeError("typed admission and result required")
        identity, lease = admission.attempt, admission.lease
        if result.attempt != identity or (identity.run_id, identity.task_id) != (
            lease.run_id,
            lease.task_id,
        ):
            raise SubscriptionConflict("result attempt identity differs")
        run = await PostgresRunRepository(self._session).get_for_update(identity.run_id)
        attempt = await self._session.get(
            SubscriptionAttempt, identity.attempt_id, with_for_update=True
        )
        if attempt is None or not _matches_admission(attempt, admission):
            raise SubscriptionConflict("result admission binding differs")
        decision = result.decision
        failure = result.failure
        decision_payload: dict[str, object] | None
        if decision is None:
            decision_payload = None
        elif isinstance(decision, PlanOutput):
            decision_payload = {"type": "PlanOutput", "value": decision.model_dump(mode="json")}
            try:
                validate_durable_payload(decision_payload)
            except ValueError:
                decision_payload = {
                    "rejected_plan_digest": hashlib.sha256(
                        json.dumps(decision_payload, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                }
                decision = None
                failure = SubscriptionFailure.PROTOCOL
        else:
            try:
                decision_payload = encode_subscription_record(decision)
            except ValueError:
                # Retain exact replay identity, never the rejected text. Usage
                # must still settle when a typed response is unsafe to persist.
                decision_payload = {
                    "rejected_record_digest": subscription_record_fingerprint(decision)
                }
                decision = None
                failure = SubscriptionFailure.PROTOCOL
        redactor = Redactor()
        telemetry = replace(
            result.telemetry,
            currency=None
            if result.telemetry.currency is None
            else str(redactor.redact(result.telemetry.currency)),
            subscription_allowance_charge=None
            if result.telemetry.subscription_allowance_charge is None
            else str(redactor.redact(result.telemetry.subscription_allowance_charge)),
            unknown_telemetry_reasons=tuple(
                str(redactor.redact(reason))
                for reason in result.telemetry.unknown_telemetry_reasons
            ),
        )
        raw_payload = {
            "schema_version": 4,
            "launch_proof": None
            if result.launch_proof is None
            else result.launch_proof.model_dump(mode="json"),
            "attempt": encode_subscription_record(identity),
            "decision": decision_payload,
            "telemetry": encode_subscription_record(telemetry),
            "telemetry_fingerprint": subscription_record_fingerprint(result.telemetry),
            "effective_failure": None if failure is None else failure.value,
            "failure": None if result.failure is None else result.failure.value,
            "failure_detail": result.failure_detail,
            # This is deliberately recorded before settlement changes the
            # scheduler/task rows.  A later approval must validate the frozen
            # admission, never infer authority from the current task.
            "proposal_context": {
                "envelope": encode_subscription_record(admission.envelope),
                "task": encode_subscription_record(admission.task),
                "route": encode_subscription_record(admission.task.route),
                "budget": encode_subscription_record(admission.task.budget),
                "candidate_epoch": admission.candidate_epoch,
                "task_version": admission.task_version,
            },
        }
        if result.quota_exhaustion is not None:
            raw_payload["quota_exhaustion"] = exhaustion_payload(result.quota_exhaustion)
        payload = _safe_metadata(
            {
                key: value
                for key, value in raw_payload.items()
                if key not in {"decision", "telemetry", "proposal_context", "launch_proof"}
            }
        )
        # Validated records remain lossless; generic observability limits apply
        # only to the surrounding diagnostics, never to executable decisions.
        payload["decision"] = decision_payload
        payload["telemetry"] = raw_payload["telemetry"]
        payload["proposal_context"] = raw_payload["proposal_context"]
        payload["launch_proof"] = raw_payload["launch_proof"]
        # Hash the in-memory response before redaction so distinct rejected or
        # redacted responses cannot alias. Raw text never crosses persistence.
        digest = hashlib.sha256(
            json.dumps(raw_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        prior = await self._session.get(
            SubscriptionAttemptResult, identity.attempt_id, with_for_update=True
        )
        if prior is not None:
            if prior.result_payload.get("schema_version") == 1:
                legacy = dict(raw_payload)
                legacy["schema_version"] = 1
                legacy.pop("launch_proof")
                legacy.pop("telemetry_fingerprint")
                legacy.pop("effective_failure")
                legacy.pop("proposal_context")
                legacy["telemetry"] = encode_subscription_record(result.telemetry)
                if isinstance(result.decision, PlanOutput):
                    legacy["decision"] = {
                        "type": "PlanOutput",
                        "value": result.decision.model_dump(mode="json"),
                    }
                digest = hashlib.sha256(
                    json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                payload = _safe_metadata(legacy)
            elif prior.result_payload.get("schema_version") == 2:
                legacy = dict(raw_payload)
                legacy["schema_version"] = 2
                legacy.pop("launch_proof")
                legacy.pop("proposal_context")
                digest = hashlib.sha256(
                    json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                payload = _safe_metadata(
                    {
                        key: value
                        for key, value in legacy.items()
                        if key not in {"decision", "telemetry", "proposal_context", "launch_proof"}
                    }
                )
                payload["decision"] = legacy["decision"]
                payload["telemetry"] = legacy["telemetry"]
            elif prior.result_payload.get("schema_version") == 3:
                legacy = dict(raw_payload)
                legacy["schema_version"] = 3
                legacy.pop("launch_proof")
                digest = hashlib.sha256(
                    json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                payload.pop("launch_proof")
                payload["schema_version"] = 3
            if prior.result_digest != digest or prior.result_payload != payload:
                raise SubscriptionConflict("result replay conflicts")
            return SubscriptionSettlement(prior.accepted, prior.disposition, True)
        budget = PostgresSubscriptionBudgetRepository(self._session)
        await budget.settle_attempt(
            identity.run_id, identity.task_id, identity.attempt_id, telemetry
        )
        task = await self._session.get(SubscriptionTask, identity.task_id, with_for_update=True)
        scheduled = await self._session.scalar(
            select(SubscriptionScheduledTask)
            .where(
                SubscriptionScheduledTask.run_id == identity.run_id,
                SubscriptionScheduledTask.task_id == identity.task_id,
            )
            .with_for_update()
        )
        scheduler_run = await self._session.get(
            SubscriptionSchedulerRun, identity.run_id, with_for_update=True
        )
        envelope = await PostgresSubscriptionRepository(self._session).envelope_for_run(
            identity.run_id
        )
        if task is None or scheduled is None or scheduler_run is None or envelope is None:
            raise SubscriptionConflict("result durable context is missing")
        current = (
            scheduled.state == "leased"
            and scheduled.lease_owner == lease.owner
            and scheduled.lease_generation == lease.generation
            and scheduled.lease_expires_at is not None
            and scheduled.lease_expires_at > self._quota.now()
            and not scheduled.pause_requested
            and not scheduled.cancel_requested
            and attempt.task_version is not None
            and task.version == attempt.task_version
            and not task.pause_requested
            and not task.cancel_requested
            and scheduler_run.candidate_epoch == admission.candidate_epoch
            and canonical_digest(task.payload) == attempt.task_digest
            and canonical_digest(encode_subscription_record(envelope)) == attempt.envelope_digest
            and run.policy_version == envelope.safety_policy_version
            and run.pending_gate is None
            and run.state
            not in {
                RunState.PAUSED,
                RunState.CANCELLED,
                RunState.FAILED,
                RunState.COMPLETED,
                RunState.AWAITING_HUMAN_INTERVENTION,
            }
        )
        effects = await self._session.scalar(
            select(SubscriptionScheduledEffect.id)
            .where(
                SubscriptionScheduledEffect.run_id == identity.run_id,
                SubscriptionScheduledEffect.task_id == identity.task_id,
                SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
            )
            .limit(1)
        )
        launches = (
            await self._session.scalars(
                select(SubscriptionClientLaunch)
                .where(SubscriptionClientLaunch.attempt_id == identity.attempt_id)
                .with_for_update()
            )
        ).all()
        fenced = (
            effects is not None
            or result.failure is SubscriptionFailure.UNCERTAIN
            or not launches_confirmed(
                launches,
                result.launch_proof,
                require_decision=result.decision is not None,
                worker_identity=attempt.lease_owner,
            )
        )
        exhaustion = result.quota_exhaustion
        if exhaustion is None and result.failure is SubscriptionFailure.QUOTA:
            exhaustion = QuotaExhaustion(self._quota.now(), "provider_usage_exhausted")
        await self._quota.settle(
            identity.attempt_id,
            exhaustion=exhaustion,
            succeeded=failure is None and decision is not None,
            stopped=result.failure is not SubscriptionFailure.UNCERTAIN
            and launches_confirmed(
                launches,
                result.launch_proof,
                require_decision=False,
                worker_identity=attempt.lease_owner,
            ),
        )
        accepted = False
        if fenced or not current:
            disposition = "fenced" if fenced else "stale"
            attempt.status = "reconciling"
            if (
                scheduled.lease_owner == lease.owner
                and scheduled.lease_generation == lease.generation
            ):
                scheduled.state = "reconciling"
                task.state = "reconciling"
                task.version += 1
        elif failure is SubscriptionFailure.QUOTA:
            await PostgresSchedulingRepository(self._session, quota=self._quota).defer_quota(lease)
            attempt.status = "terminal"
            task.state = "queued"
            task.version += 1
            disposition = "quota_deferred"
            accepted = True
        elif decision is not None and not (
            isinstance(decision, TaskHandoff) and decision.status is not HandoffStatus.COMPLETED
        ):
            # Retain the exact result for the typed decision dispatcher. Merely
            # recording a provider decision must never claim it was applied.
            disposition = "decision_pending"
            attempt.status = "reconciling"
            scheduled.state = "reconciling"
            task.state = "reconciling"
            task.version += 1
        else:
            handoff_result = decision if isinstance(decision, TaskHandoff) else None
            retryable = (
                handoff_result.status is HandoffStatus.FAILED
                if handoff_result is not None
                else failure
                in {
                    SubscriptionFailure.PROTOCOL,
                    SubscriptionFailure.DEADLINE,
                    SubscriptionFailure.INTERRUPTED,
                }
            )
            repair = (
                retryable
                and scheduled.repairs < scheduled.max_repairs
                and await budget.try_debit_repair(
                    identity.run_id, identity.task_id, identity.attempt_id
                )
            )
            await PostgresSchedulingRepository(self._session).finish(
                lease, successful=False, allow_repair=repair
            )
            attempt.status = "terminal"
            task.state = "queued" if repair else "terminal"
            task.version += 1
            disposition = "repair_queued" if repair else "failed"
            if handoff_result is not None:
                handoff = (
                    replace(handoff_result, status=HandoffStatus.REPAIRS_EXHAUSTED)
                    if retryable and not repair
                    else handoff_result
                )
                if not retryable and handoff.status in {
                    HandoffStatus.BLOCKED,
                    HandoffStatus.SCOPE_EXPANSION_REQUESTED,
                }:
                    disposition = "handoff"
            else:
                if failure is None:
                    raise SubscriptionConflict("failed result has no failure classification")
                handoff = TaskHandoff(
                    run_id=identity.run_id,
                    task_id=identity.task_id,
                    attempt_id=identity.attempt_id,
                    status=HandoffStatus.FAILED
                    if repair
                    else (HandoffStatus.REPAIRS_EXHAUSTED if retryable else HandoffStatus.BLOCKED),
                    summary="Provider attempt failed: " + failure.value,
                )
            await PostgresSubscriptionRepository(self._session).record_decision(
                handoff, idempotency_key=f"result:{identity.attempt_id}"
            )
            accepted = True
        attempt.telemetry_payload = encode_subscription_record(telemetry)
        self._session.add(
            SubscriptionAttemptResult(
                attempt_id=identity.attempt_id,
                result_digest=digest,
                result_payload=payload,
                disposition=disposition,
                accepted=accepted,
            )
        )
        await self._session.flush()
        feedback = PostgresSubscriptionFeedbackRepository(self._session)
        # Request delivery is proved by the stopped official-client launch; a
        # concurrent pause/cancel may revoke the result but cannot undo receipt.
        if launches_confirmed(
            launches,
            result.launch_proof,
            require_decision=False,
            worker_identity=attempt.lease_owner,
        ):
            await feedback.settle_delivery(identity.attempt_id)
        if task.state == "terminal":
            await feedback.requeue_undelivered(identity.run_id, identity.task_id)
            await feedback.requeue_failed_primary(identity.run_id, identity.task_id)
        return SubscriptionSettlement(accepted, disposition)
