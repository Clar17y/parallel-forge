"""Materialize a settled subscription PlanOutput into the existing human plan gate."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.subscription_execution import SubscriptionSettlement
from forge.application.ports.subscription_plan_gate import SubscriptionPlanGateError
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.internal_preparation import (
    INTERNAL_PREPARATION_EVENT,
    InternalPreparationError,
    enqueue_prepare_worktree,
    originating_operator_id,
    record_internal_preparation_approval,
)
from forge.application.services.plan_evidence import (
    InvalidSubscriptionPlan,
    PlanEvidenceValidationError,
    build_subscription_plan_evidence,
    validate_subscription_plan_fields,
)
from forge.domain.approval import (
    ApprovalGate,
    SubscriptionPlanApprovalEvidence,
    canonical_digest,
)
from forge.domain.plan import PlanOutput, material_questions, plan_digest_bytes
from forge.domain.run import RunState
from forge.domain.task_usage_contract import SubjectKey


@dataclass(frozen=True, slots=True)
class SubscriptionPlanGateOutcome:
    evidence_digest: str
    run_version: int
    replayed: bool


class SubscriptionPlanGateService:
    """Persist bytes first, then atomically bind them to one current plan gate.

    Publication can resume from an attempt ID or validate supplied evidence.
    Both paths prove the settled primary producer against durable source state.
    """

    def __init__(
        self, artifacts: ArtifactStore, unit_of_work_factory: Callable[[], UnitOfWork]
    ) -> None:
        self._artifacts = artifacts
        self._unit_of_work_factory = unit_of_work_factory

    async def _checkpoint_active(self, work: UnitOfWork, run_id: UUID) -> bool:
        run = await work.runs.get(run_id)
        return (
            await work.task_usage_checkpoints.active_for_subject(
                SubjectKey(project_id=run.project_id, kind="run", subject_id=run.id)
            )
            is not None
        )

    async def request_settled(
        self, attempt_id: UUID
    ) -> SubscriptionPlanGateOutcome | SubscriptionSettlement:
        """Resume publication from PostgreSQL without caller-assembled evidence."""
        async with self._unit_of_work_factory() as work:
            replay = await work.subscription_plan_gate.rejection(attempt_id)
            if replay is not None:
                await work.rollback()
                return replay
            proposal = await work.subscription_plan_gate.proposal(attempt_id)
            run = await work.runs.get_for_update(proposal.producer.run_id)
            if await self._checkpoint_active(work, run.id):
                raise SubscriptionPlanGateError("plan publication is fenced by usage checkpoint")
            try:
                evidence = await build_subscription_plan_evidence(
                    work, run, proposal.producer, proposal.result_digest, proposal.plan
                )
            except InvalidSubscriptionPlan as invalid:
                outcome = await work.subscription_plan_gate.reject(invalid.evidence)
                await work.commit()
                return outcome
            except PlanEvidenceValidationError, ValueError, TypeError:
                raise SubscriptionPlanGateError("subscription approval source differs") from None
            await work.rollback()
        # Storage and final authority checks have the same transaction boundary
        # as direct publication; a stop or policy change between reads wins.
        return await self.request(evidence, proposal.plan)

    async def request(
        self, evidence: SubscriptionPlanApprovalEvidence, plan: PlanOutput
    ) -> SubscriptionPlanGateOutcome:
        async with self._unit_of_work_factory() as work:
            if await work.subscription_plan_gate.get(
                evidence.producer.attempt_id
            ) is None and await self._checkpoint_active(work, evidence.producer.run_id):
                raise SubscriptionPlanGateError("plan publication is fenced by usage checkpoint")
        evidence_bytes = _canonical_bytes(evidence)
        if hashlib.sha256(evidence_bytes).hexdigest() != canonical_digest(evidence):
            raise SubscriptionPlanGateError("subscription evidence cannot be canonically stored")
        # Object storage can block; do it without holding a database lock.
        evidence_descriptor = await self._artifacts.put_bytes(
            evidence_bytes,
            media_type="application/json",
            max_bytes=1_048_576,
            bounding_policy="head_tail",
        )
        if evidence_descriptor.digest != canonical_digest(evidence):
            raise SubscriptionPlanGateError("subscription evidence storage digest differs")
        plan_bytes = plan_digest_bytes(plan)
        plan_descriptor = await self._artifacts.put_bytes(
            plan_bytes,
            media_type="application/json",
            max_bytes=1_048_576,
            bounding_policy="head_tail",
        )
        if plan_descriptor.digest != evidence.plan_digest:
            raise SubscriptionPlanGateError("subscription plan storage digest differs")
        for descriptor in (evidence_descriptor, plan_descriptor):
            if descriptor.truncated or await self._artifacts.verify(descriptor.digest) is not True:
                raise SubscriptionPlanGateError("subscription proposal artifact is not verified")
            data = await self._artifacts.open_bytes(descriptor.digest, max_bytes=1_048_576)
            if (
                len(data) != descriptor.byte_count
                or hashlib.sha256(data).hexdigest() != descriptor.digest
            ):
                raise SubscriptionPlanGateError("subscription proposal artifact bytes differ")
        async with self._unit_of_work_factory() as work:
            run = await work.runs.get_for_update(evidence.producer.run_id)
            try:
                await validate_subscription_plan_fields(work, run, evidence, plan)
            except PlanEvidenceValidationError, ValueError, TypeError:
                raise SubscriptionPlanGateError("subscription approval fields differ") from None
            existing = await work.subscription_plan_gate.get(evidence.producer.attempt_id)
            if existing is not None:
                is_replay = existing.evidence_digest == canonical_digest(evidence) and (
                    (
                        run.state is RunState.AWAITING_PLAN_APPROVAL
                        and run.pending_gate is ApprovalGate.PLAN
                        and run.pending_evidence_digest == existing.evidence_digest
                    )
                    or (
                        run.state
                        in {
                            RunState.PREPARING_WORKTREE,
                            RunState.IMPLEMENTING,
                            RunState.VALIDATING,
                            RunState.REVIEWING,
                            RunState.REMEDIATING,
                            RunState.AWAITING_PR_APPROVAL,
                            RunState.PUBLISHING_PR,
                            RunState.MONITORING_PR,
                            RunState.AWAITING_MERGE_APPROVAL,
                            RunState.MERGING,
                            RunState.COMPLETED,
                        }
                        or (
                            run.state is RunState.PAUSED
                            and run.suspended_state
                            in {
                                RunState.PREPARING_WORKTREE,
                                RunState.IMPLEMENTING,
                                RunState.VALIDATING,
                                RunState.REVIEWING,
                                RunState.REMEDIATING,
                                RunState.AWAITING_PR_APPROVAL,
                            }
                        )
                    )
                )
                if not is_replay:
                    raise SubscriptionPlanGateError("subscription plan replay conflicts")
                await work.subscription_plan_gate.verify(evidence)
                await work.commit()
                return SubscriptionPlanGateOutcome(existing.evidence_digest, run.version, True)
            if await self._checkpoint_active(work, run.id):
                raise SubscriptionPlanGateError("plan publication is fenced by usage checkpoint")
            if (
                run.state is not RunState.PLANNING
                or run.policy_version != evidence.policy_version
                or run.base_ref != evidence.base_ref
                or run.base_sha != evidence.base_sha
            ):
                raise SubscriptionPlanGateError("subscription proposal is stale")
            await work.subscription_plan_gate.record(evidence)
            await work.artifacts.record(
                plan_descriptor,
                run_id=run.id,
                producer_type="subscription_plan",
                producer_id=evidence.producer.attempt_id,
            )
            await work.artifacts.record(
                evidence_descriptor,
                run_id=run.id,
                producer_type="subscription_plan_approval_evidence",
                producer_id=evidence.producer.attempt_id,
                parent_digests=(plan_descriptor.digest,),
            )
            questions = material_questions(plan)
            review_plan_first = run.workflow.review_plan_first if run.workflow is not None else True
            if review_plan_first or questions:
                gate_payload: dict[str, object] = {
                    "attempt_id": str(evidence.producer.attempt_id),
                    "result_digest": evidence.result_digest,
                }
                if questions:
                    gate_payload["material_questions"] = list(questions)
                transitioned = await work.runs.await_approval(
                    run.id,
                    run.version,
                    ApprovalGate.PLAN,
                    evidence_descriptor.digest,
                    "run.plan_approval_requested",
                    gate_payload,
                    actor_class="worker",
                )
            else:
                events = await work.events.list_after(run.id, 0)
                originator_hint = originating_operator_id(events)
                if originator_hint is None:
                    raise SubscriptionPlanGateError(
                        "internal preparation requires recorded run-creation provenance"
                    )
                try:
                    _, originator, payload = await record_internal_preparation_approval(
                        work,
                        run,
                        evidence_digest=evidence_descriptor.digest,
                        authorized_by=originator_hint,
                        authorization="run_creation",
                    )
                except InternalPreparationError as error:
                    raise SubscriptionPlanGateError(str(error)) from None
                transitioned = await work.runs.prepare_plan_internally(
                    run.id,
                    run.version,
                    evidence,
                    INTERNAL_PREPARATION_EVENT,
                    payload,
                    actor_class="worker",
                    actor_id=None,
                )
                await enqueue_prepare_worktree(
                    work,
                    run_id=run.id,
                    run_version=transitioned.version,
                    actor_id=originator,
                )
            await work.subscription_plan_gate.mark_applied(evidence)
            await work.commit()
            return SubscriptionPlanGateOutcome(
                evidence_descriptor.digest, transitioned.version, False
            )


def _canonical_bytes(evidence: SubscriptionPlanApprovalEvidence) -> bytes:
    return json.dumps(
        evidence.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


__all__ = ["SubscriptionPlanGateOutcome", "SubscriptionPlanGateService"]
