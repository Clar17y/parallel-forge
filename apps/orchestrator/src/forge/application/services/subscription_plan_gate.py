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
from forge.domain.plan import PlanOutput
from forge.domain.run import RunState


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
        plan_bytes = plan.model_dump_json(by_alias=False).encode()
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
                if (
                    run.state is not RunState.AWAITING_PLAN_APPROVAL
                    or run.pending_gate is not ApprovalGate.PLAN
                    or existing.evidence_digest != canonical_digest(evidence)
                    or run.pending_evidence_digest != existing.evidence_digest
                ):
                    raise SubscriptionPlanGateError("subscription plan replay conflicts")
                await work.subscription_plan_gate.verify(evidence)
                await work.commit()
                return SubscriptionPlanGateOutcome(existing.evidence_digest, run.version, True)
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
            transitioned = await work.runs.await_approval(
                run.id,
                run.version,
                ApprovalGate.PLAN,
                evidence_descriptor.digest,
                "run.plan_approval_requested",
                {
                    "attempt_id": str(evidence.producer.attempt_id),
                    "result_digest": evidence.result_digest,
                },
                actor_class="worker",
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
