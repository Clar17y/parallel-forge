"""Receipt-bound requeue of stopped attempts after an explicit operator resume."""

from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.subscription_execution import SubscriptionResumption
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.operation import canonical_digest
from forge.domain.paths import policy_path_key
from forge.domain.plan import PlanOutput, decode_plan_output
from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.run import RunSnapshot, RunState
from forge.domain.subscription import (
    AcceptDecision,
    AttemptIdentity,
    AttemptTelemetry,
    BoundScopeResponseDecision,
    DelegateDecision,
    ExecutionEnvelope,
    ForwardFeedbackDecision,
    HandoffStatus,
    LogicalTaskContract,
    ReviewSelection,
    ScopeRequestDecision,
    SpecialistPurpose,
    TaskBudget,
    TaskHandoff,
    WaitDecision,
    decode_subscription_record,
    encode_subscription_record,
    is_read_only,
)
from forge.domain.subscription_budget import project_attempt_charge
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.persistence.models import RunEvent
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionDecisionRecord,
    SubscriptionTask,
)
from forge.persistence.models.subscription_quota import (
    SubscriptionQuotaAdmission,
    SubscriptionQuotaObservation,
)
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.models.subscription_usage import (
    SubscriptionAttemptConsumption,
    SubscriptionAttemptReservation,
)
from forge.persistence.repositories.commands import (
    CommandLeaseError,
    CommandNotFound,
    PostgresCommandRepository,
)
from forge.persistence.repositories.runs import PostgresRunRepository
from forge.persistence.repositories.subscription import PostgresSubscriptionRepository
from forge.persistence.repositories.subscription_budget import PostgresSubscriptionBudgetRepository
from forge.persistence.repositories.subscription_launch import launches_confirmed

_KIND = "paused_subscription_attempt"
_STATES = {RunState.PLANNING, RunState.IMPLEMENTING, RunState.REMEDIATING}


@dataclass(frozen=True, slots=True)
class _Source:
    attempt: SubscriptionAttempt
    result: SubscriptionAttemptResult
    task: SubscriptionTask
    scheduled: SubscriptionScheduledTask
    scheduler: SubscriptionSchedulerRun
    contract: LogicalTaskContract


def _invalid() -> CommandRecoveryRequired:
    return CommandRecoveryRequired("stopped subscription resume source differs")


async def quota_deferral_observation(session: AsyncSession, source: _Source) -> UUID | None:
    """Only a settled, durably observed quota failure avoids a repair debit."""
    payload = source.result.result_payload
    if payload.get("effective_failure") != "quota":
        return None
    from forge.persistence.repositories.subscription_quota import exhaustion_payload

    admission = await session.get(SubscriptionQuotaAdmission, source.attempt.id)
    observations = list(
        await session.scalars(
            select(SubscriptionQuotaObservation)
            .where(
                SubscriptionQuotaObservation.source_attempt_id == source.attempt.id,
            )
            .limit(2)
        )
    )
    if (
        payload.get("failure") != "quota"
        or payload.get("decision") is not None
        or admission is None
        or admission.finished_at is None
        or len(observations) != 1
    ):
        raise _invalid()
    observation = observations[0]
    evidence = exhaustion_payload(
        QuotaExhaustion(
            observation.observed_at,
            observation.reason,
            observation.reset_at,
        )
    )
    if (
        observation.actor_id is not None
        or (observation.provider, observation.account, observation.pool)
        != (admission.provider, admission.account, admission.pool)
        or observation.evidence_digest != canonical_digest(evidence)
        or ("quota_exhaustion" in payload and payload["quota_exhaustion"] != evidence)
    ):
        raise _invalid()
    return observation.id


async def _source(
    session: AsyncSession,
    run: RunSnapshot,
    attempt_id: UUID,
    *,
    historical: bool,
    pending_decision: bool = False,
    allow_fenced: bool = False,
    settled_idle: bool = False,
) -> _Source:
    if settled_idle and (not historical or pending_decision or allow_fenced):
        raise _invalid()
    attempt = await session.get(SubscriptionAttempt, attempt_id, with_for_update=True)
    result = await session.get(SubscriptionAttemptResult, attempt_id, with_for_update=True)
    if attempt is None or result is None or attempt.run_id != run.id:
        raise _invalid()
    task = await session.get(SubscriptionTask, attempt.task_row_id, with_for_update=True)
    scheduled = await session.get(
        SubscriptionScheduledTask, attempt.task_row_id, with_for_update=True
    )
    scheduler = await session.get(SubscriptionSchedulerRun, run.id, with_for_update=True)
    current_envelope = await PostgresSubscriptionRepository(session).envelope_for_run(run.id)
    try:
        payload = result.result_payload
        context = payload["proposal_context"]
        raw_identity, raw_telemetry = payload["attempt"], payload["telemetry"]
        if (
            not isinstance(context, dict)
            or not isinstance(raw_identity, dict)
            or not isinstance(raw_telemetry, dict)
        ):
            raise TypeError
        contract = decode_subscription_record(context["task"])
        envelope = decode_subscription_record(context["envelope"])
        identity = decode_subscription_record(raw_identity)
        telemetry = decode_subscription_record(raw_telemetry)
        proof = SubscriptionLaunchTerminalProof.model_validate(payload.get("launch_proof"))
        if (
            type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 4
            or result.disposition
            not in (
                (
                    "quota_deferred",
                    "repair_queued",
                    "scope_requested",
                    "decision_repair_queued",
                    "handoff_repair_queued",
                    "stale",
                    "fenced",
                )
                if settled_idle
                else ("decision_pending",)
                if pending_decision
                else ("stale", "fenced")
                if allow_fenced
                else ("stale",)
            )
            or (not settled_idle and result.accepted)
            or (settled_idle and attempt.status != "terminal")
            or canonical_digest(payload) != result.result_digest
            or not isinstance(contract, LogicalTaskContract)
            or not isinstance(envelope, ExecutionEnvelope)
            or not isinstance(identity, AttemptIdentity)
            or not isinstance(telemetry, AttemptTelemetry)
            or envelope != current_envelope
            or envelope.run_id != run.id
            or envelope.safety_policy_version != run.policy_version
            or (identity.run_id, identity.task_id, identity.attempt_id, identity.attempt_number)
            != (run.id, attempt.task_row_id, attempt.id, attempt.attempt_number)
            or contract.run_id != run.id
            or contract.task_id != attempt.task_row_id
            or attempt.envelope_digest != canonical_digest(encode_subscription_record(envelope))
            or attempt.task_digest != canonical_digest(encode_subscription_record(contract))
            or attempt.route_payload != encode_subscription_record(contract.route)
            or attempt.telemetry_payload != raw_telemetry
            or context
            != {
                "envelope": encode_subscription_record(envelope),
                "task": encode_subscription_record(contract),
                "route": encode_subscription_record(contract.route),
                "budget": encode_subscription_record(contract.budget),
                "candidate_epoch": attempt.candidate_epoch,
                "task_version": attempt.task_version,
            }
            or task is None
            or scheduled is None
            or scheduler is None
            or task.run_id != run.id
            or task.task_id != contract.task_id
            or scheduled.run_id != run.id
            or scheduled.task_id != contract.task_id
            or attempt.task_version is None
            or attempt.candidate_epoch is None
            or attempt.lease_owner is None
            or attempt.lease_generation is None
        ):
            raise _invalid()
    except KeyError, TypeError, ValueError:
        raise _invalid() from None
    launches = (
        await session.scalars(
            select(SubscriptionClientLaunch)
            .where(SubscriptionClientLaunch.attempt_id == attempt.id)
            .with_for_update()
        )
    ).all()
    consumption = await session.get(SubscriptionAttemptConsumption, attempt.id)
    reservation = await session.get(SubscriptionAttemptReservation, attempt.id)
    if (
        not launches_confirmed(
            launches,
            proof,
            require_decision=pending_decision,
            worker_identity=attempt.lease_owner,
        )
        or consumption is None
        or reservation is None
        or consumption.telemetry_payload != payload["telemetry"]
        or reservation.run_id != run.id
        or reservation.task_id != task.id
        or await session.scalar(
            select(SubscriptionScheduledEffect.id)
            .where(
                SubscriptionScheduledEffect.run_id == run.id,
                SubscriptionScheduledEffect.task_id == task.id,
                SubscriptionScheduledEffect.lease_generation == attempt.lease_generation,
                SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
            )
            .limit(1)
        )
        is not None
    ):
        raise _invalid()
    reserved_budget = decode_subscription_record(reservation.budget_payload)
    if not isinstance(reserved_budget, TaskBudget):
        raise _invalid()
    charge = project_attempt_charge(reserved_budget, telemetry, repair=False)
    budget_repository = PostgresSubscriptionBudgetRepository(session)
    _, primary, _, _ = await budget_repository._contracts(run.id, task.id, attempt.id)
    violations = []
    for _, scope, budget in await budget_repository._budget_scopes(
        run.id, task.id, contract, primary
    ):
        try:
            budget.unknown_telemetry_policy.validate_telemetry(telemetry, budget.billing_mode)
        except ValueError:
            violations.append(f"{scope}:unknown_telemetry")
    if (
        canonical_digest(consumption.observed) != canonical_digest(dict(charge.observed.values()))
        or canonical_digest(consumption.charged) != canonical_digest(dict(charge.charged.values()))
        or tuple(consumption.unknown_fields) != charge.unknown_fields
        or tuple(consumption.exceeded_fields) != charge.exceeded_fields
        or consumption.policy_violations != violations
        or consumption.uncertain
        != (
            not (
                telemetry.is_token_telemetry_known
                and telemetry.is_cost_known
                and telemetry.is_quota_known
            )
        )
    ):
        raise _invalid()
    current_version: int | None = attempt.task_version + 1
    if not historical and pending_decision:
        from forge.persistence.repositories.subscription_task_stop_receipts import (
            pending_decision_task_version,
        )

        current_version = await pending_decision_task_version(session, attempt, result)
    if not historical and (
        task.state != "reconciling"
        or scheduled.state != "reconciling"
        or attempt.status != "reconciling"
        or task.version != current_version
        or task.payload != encode_subscription_record(contract)
        or task.parent_task_id != contract.parent_task_id
        or scheduled.parent_task_id != contract.parent_task_id
        or scheduled.provider != contract.route.effective.provider
        or tuple(scheduled.owned_paths)
        != tuple(policy_path_key(path) for path in contract.owned_paths)
        or tuple(scheduled.dependency_task_ids) != contract.dependency_task_ids
        or scheduled.read_only != is_read_only(contract.purpose)
        or scheduled.max_repairs != contract.max_repairs
        or task.pause_requested
        or task.cancel_requested
        or scheduled.pause_requested
        or scheduled.cancel_requested
        or scheduled.lease_owner != attempt.lease_owner
        or scheduled.lease_generation != attempt.lease_generation
        or not scheduler.admitted
        or scheduler.candidate_epoch != attempt.candidate_epoch
        or scheduler.candidate_state not in {"open", "closed", "draining"}
        or result.application_payload is not None
        or result.application_digest is not None
    ):
        raise _invalid()
    return _Source(attempt, result, task, scheduled, scheduler, contract)


def _pending_decision_is_admissible(
    source: _Source, run: RunSnapshot, *, phase: RunState | None = None
) -> bool:
    """Validate retained decisions that the existing dispatcher can apply."""
    payload = source.result.result_payload
    try:
        context = payload["proposal_context"]
        if (
            payload["effective_failure"] is not None
            or payload["failure"] is not None
            or payload["failure_detail"] is not None
            or "quota_exhaustion" in payload
            or not isinstance(context, dict)
            or not isinstance(context.get("envelope"), dict)
            or not (envelope := decode_subscription_record(context["envelope"]))
            or not isinstance(envelope, ExecutionEnvelope)
            or not envelope.permits_route(source.contract.purpose, source.contract.route)
        ):
            return False
        raw_decision = payload["decision"]
        decision: object
        if isinstance(raw_decision, dict) and raw_decision.get("type") == "PlanOutput":
            decision = decode_plan_output(raw_decision["value"])
            permitted_phase = (run.suspended_state if phase is None else phase) is RunState.PLANNING
        else:
            if not isinstance(raw_decision, dict):
                return False
            decision = decode_subscription_record(raw_decision)
            permitted_phase = (run.suspended_state if phase is None else phase) in {
                RunState.IMPLEMENTING,
                RunState.REMEDIATING,
            }
        if (
            not isinstance(
                decision,
                (
                    PlanOutput,
                    DelegateDecision,
                    WaitDecision,
                    TaskHandoff,
                    ScopeRequestDecision,
                    BoundScopeResponseDecision,
                    AcceptDecision,
                    ReviewSelection,
                    ForwardFeedbackDecision,
                ),
            )
            or not permitted_phase
        ):
            return False
        if isinstance(decision, TaskHandoff) and decision.status is not HandoffStatus.COMPLETED:
            return False
        worker_decision = isinstance(decision, (TaskHandoff, ScopeRequestDecision))
        primary = source.contract.purpose is SpecialistPurpose.PRIMARY
        if (
            primary == worker_decision
            or source.contract.route.is_primary != primary
            or (source.contract.parent_task_id is None) != primary
        ):
            return False
        # Reconstruct the original result through the shared boundary.  Its
        # identity rules intentionally cover the decision variants which may
        # target a related task, while the frozen source covers the attempt.
        raw_identity, raw_telemetry = payload["attempt"], payload["telemetry"]
        if not isinstance(raw_identity, dict) or not isinstance(raw_telemetry, dict):
            return False
        identity = decode_subscription_record(raw_identity)
        telemetry = decode_subscription_record(raw_telemetry)
        if not isinstance(identity, AttemptIdentity) or not isinstance(telemetry, AttemptTelemetry):
            return False
        SubscriptionInvocationResult(
            attempt=identity,
            decision=decision,
            telemetry=telemetry,
            launch_proof=SubscriptionLaunchTerminalProof.model_validate(payload["launch_proof"]),
        )
        return True
    except KeyError, TypeError, ValueError:
        return False


async def pending_decision_quiescence_exemptions(
    session: AsyncSession, run_id: UUID, exclude_command_id: UUID | None
) -> tuple[tuple[UUID, UUID], ...]:
    """Return only stopped, retained decisions proved by the current resume lease.

    This is intentionally a narrow count exemption.  It does not mutate the
    retained result, task, scheduler, budget, or decision application state.
    """
    if exclude_command_id is None:
        return ()
    try:
        run = await PostgresRunRepository(session).get_for_update(run_id)
        resume = await PostgresCommandRepository(session=session).assert_current_lease(
            await PostgresCommandRepository(session=session).get(exclude_command_id)
        )
        if (
            resume.run_id != run_id
            or resume.command_type != "resume"
            or resume.actor_id is None
            or resume.payload_schema_version != 1
            or resume.payload != {}
            or run.state is not RunState.PAUSED
            or run.version != resume.expected_run_version
            or run.suspended_state not in _STATES
            or await _pause_id(session, resume) is None
        ):
            return ()
        attempt_ids = (
            await session.scalars(
                select(SubscriptionAttempt.id)
                .join(SubscriptionTask, SubscriptionTask.id == SubscriptionAttempt.task_row_id)
                .join(
                    SubscriptionAttemptResult,
                    SubscriptionAttemptResult.attempt_id == SubscriptionAttempt.id,
                )
                .where(
                    SubscriptionAttempt.run_id == run_id,
                    SubscriptionAttempt.status == "reconciling",
                    SubscriptionAttemptResult.disposition == "decision_pending",
                    SubscriptionTask.pause_requested.is_(False),
                    SubscriptionTask.cancel_requested.is_(False),
                )
                .order_by(SubscriptionAttempt.task_row_id, SubscriptionAttempt.id)
            )
        ).all()
        proven = []
        for attempt_id in attempt_ids:
            source = await _source(
                session, run, attempt_id, historical=False, pending_decision=True
            )
            if _pending_decision_is_admissible(source, run):
                proven.append((source.attempt.id, source.task.id))
        return tuple(proven)
    except (
        CommandLeaseError,
        CommandNotFound,
        CommandRecoveryRequired,
        KeyError,
        TypeError,
        ValueError,
    ):
        return ()


def _handoff(source: _Source) -> TaskHandoff:
    return TaskHandoff(
        run_id=source.attempt.run_id,
        task_id=source.task.id,
        attempt_id=source.attempt.id,
        status=HandoffStatus.FAILED,
        summary="The previous invocation stopped while run authority was suspended. "
        "Its result is retained without applying the proposed decision. Inspect the existing "
        "work within the unchanged contract, then continue with fresh evidence. "
        f"Stopped attempt: {source.attempt.id}; result: {source.result.result_digest}.",
    )


def _receipt(
    source: _Source,
    resume: CommandEnvelope,
    pause_id: UUID,
    repairs: int,
    quota_observation: UUID | None = None,
) -> dict[str, object]:
    attempt = source.attempt
    assert attempt.task_version is not None
    return {
        "schema_version": 1,
        "kind": _KIND,
        "run_id": str(resume.run_id),
        "resume_command_id": str(resume.id),
        "pause_command_id": str(pause_id),
        "paused_version": resume.expected_run_version,
        "attempt_id": str(attempt.id),
        "task_id": str(source.task.id),
        "result_digest": source.result.result_digest,
        "result_payload_digest": canonical_digest(source.result.result_payload),
        "task_digest": attempt.task_digest,
        "envelope_digest": attempt.envelope_digest,
        "candidate_epoch": attempt.candidate_epoch,
        "source_task_version": attempt.task_version,
        "restored_task_version": attempt.task_version + 2,
        "lease_generation": attempt.lease_generation,
        "repairs": repairs,
        "handoff_digest": canonical_digest(encode_subscription_record(_handoff(source))),
        **(
            {"quota_observation_id": str(quota_observation)}
            if quota_observation is not None
            else {}
        ),
    }


async def _pause_id(session: AsyncSession, resume: CommandEnvelope) -> UUID:
    events = (
        await session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == resume.run_id,
                RunEvent.run_version == resume.expected_run_version,
                RunEvent.event_type == "run.paused",
            )
        )
    ).all()
    if len(events) != 1:
        raise _invalid()
    event = events[0]
    try:
        pause = await PostgresCommandRepository(session=session).get(
            UUID(str(event.payload["command_id"]))
        )
    except KeyError, TypeError, ValueError:
        raise _invalid() from None
    if (
        pause.run_id != resume.run_id
        or pause.command_type != "pause"
        or pause.status is not CommandStatus.COMPLETED
        or pause.actor_id is None
        or pause.payload_schema_version != 1
        or pause.payload != {}
        or pause.expected_run_version != resume.expected_run_version - 1
        or event.actor_class != "operator"
        or event.actor_id != pause.actor_id
        or event.payload_schema_version != 1
        or event.payload
        != {
            "command_id": str(pause.id),
            "command_type": "pause",
            "command_payload": {},
            "expected_run_version": pause.expected_run_version,
        }
    ):
        raise _invalid()
    return pause.id


async def resume_paused_attempts(
    session: AsyncSession, resume: CommandEnvelope, pause: CommandEnvelope
) -> tuple[SubscriptionResumption, ...]:
    run = await PostgresRunRepository(session).get_for_update(resume.run_id)
    if run.suspended_state not in _STATES:
        return ()
    sources = (
        await session.scalars(
            select(SubscriptionAttempt.id)
            .join(SubscriptionTask, SubscriptionTask.id == SubscriptionAttempt.task_row_id)
            .join(
                SubscriptionAttemptResult,
                SubscriptionAttemptResult.attempt_id == SubscriptionAttempt.id,
            )
            .where(
                SubscriptionAttempt.run_id == run.id,
                SubscriptionAttempt.status == "reconciling",
                SubscriptionAttemptResult.disposition == "stale",
                SubscriptionTask.pause_requested.is_(False),
                SubscriptionTask.cancel_requested.is_(False),
            )
            .order_by(SubscriptionAttempt.task_row_id, SubscriptionAttempt.id)
        )
    ).all()
    if not sources:
        return ()
    current = await PostgresCommandRepository(session=session).assert_current_lease(resume)
    if (
        replace(current, lease_expires_at=resume.lease_expires_at) != resume
        or resume.command_type != "resume"
        or resume.actor_id is None
        or resume.payload_schema_version != 1
        or resume.payload != {}
        or run.state is not RunState.PAUSED
        or run.version != resume.expected_run_version
        or await _pause_id(session, resume) != pause.id
    ):
        raise _invalid()
    resumptions = []
    for attempt_id in sources:
        source = await _source(session, run, attempt_id, historical=False)
        quota_observation = await quota_deferral_observation(session, source)
        if quota_observation is None and (
            source.scheduled.repairs >= source.scheduled.max_repairs
            or not await PostgresSubscriptionBudgetRepository(session).try_debit_repair(
                run.id, source.task.id, attempt_id
            )
        ):
            raise CommandRecoveryRequired(
                "stopped subscription resume requires remaining repair budget"
            )
        await PostgresSubscriptionRepository(session).record_decision(
            _handoff(source), idempotency_key=f"paused-resume:{resume.id}:{attempt_id}"
        )
        if quota_observation is None:
            source.scheduled.repairs += 1
        source.task.version += 1
        source.task.state = source.scheduled.state = "queued"
        source.scheduled.lease_owner = source.scheduled.lease_expires_at = None
        source.attempt.status = "terminal"
        receipt = _receipt(source, resume, pause.id, source.scheduled.repairs, quota_observation)
        source.result.application_payload = receipt
        source.result.application_digest = canonical_digest(receipt)
        resumptions.append(SubscriptionResumption(attempt_id, source.result.application_digest))
    await session.flush()
    return tuple(resumptions)


async def verify_paused_attempts(
    session: AsyncSession, resume: CommandEnvelope
) -> tuple[SubscriptionResumption, ...]:
    rows = (
        await session.scalars(
            select(SubscriptionAttemptResult)
            .join(
                SubscriptionAttempt, SubscriptionAttempt.id == SubscriptionAttemptResult.attempt_id
            )
            .where(
                SubscriptionAttempt.run_id == resume.run_id,
                SubscriptionAttemptResult.application_payload["kind"].astext == _KIND,
                SubscriptionAttemptResult.application_payload["resume_command_id"].astext
                == str(resume.id),
            )
            .order_by(SubscriptionAttempt.task_row_id, SubscriptionAttempt.id)
        )
    ).all()
    if not rows:
        return ()
    run = await PostgresRunRepository(session).get_for_update(resume.run_id)
    pause_id = await _pause_id(session, resume)
    resumptions = []
    for result in rows:
        source = await _source(session, run, result.attempt_id, historical=True)
        receipt = result.application_payload
        assert receipt is not None
        repairs = receipt.get("repairs")
        debit = await session.get(SubscriptionRepairDebit, result.attempt_id)
        quota_observation = None
        if "quota_observation_id" in receipt:
            quota_observation = await quota_deferral_observation(session, source)
            if quota_observation is None or receipt["quota_observation_id"] != str(
                quota_observation
            ):
                raise _invalid()
        handoff = await session.scalar(
            select(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.run_id == run.id,
                SubscriptionDecisionRecord.idempotency_key
                == f"paused-resume:{resume.id}:{result.attempt_id}",
            )
        )
        if (
            type(repairs) is not int
            or repairs < (1 if quota_observation is None else 0)
            or canonical_digest(receipt) != result.application_digest
            or canonical_digest(receipt)
            != canonical_digest(_receipt(source, resume, pause_id, repairs, quota_observation))
            or source.attempt.status != "terminal"
            or source.attempt.task_version is None
            or source.task.version < source.attempt.task_version + 2
            or source.scheduled.repairs < repairs
            or (debit is None if quota_observation is None else debit is not None)
            or handoff is None
            or handoff.attempt_id != result.attempt_id
            or handoff.task_row_id != source.task.id
            or handoff.payload != encode_subscription_record(_handoff(source))
        ):
            raise _invalid()
        assert result.application_digest is not None
        resumptions.append(SubscriptionResumption(result.attempt_id, result.application_digest))
    return tuple(resumptions)
