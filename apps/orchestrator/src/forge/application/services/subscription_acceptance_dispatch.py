"""Admit final controller validation after primary, candidate and receipt proof."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.subscription_acceptance import (
    PreparedSubscriptionAcceptance,
    acceptance_intent_payload,
)
from forge.application.ports.subscription_acceptance_receipts import AcceptanceReceiptClaimError
from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_execution import SubscriptionSettlement
from forge.application.ports.subscription_validation import AcceptanceValidationBinding
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.subscription_acceptance_receipts import (
    SubscriptionAcceptanceReceiptVerification,
)
from forge.domain.approval import SubscriptionPlanApprovalEvidence
from forge.domain.command import CommandStatus
from forge.domain.operation import canonical_digest
from forge.domain.run import RunState
from forge.domain.subscription_execution import SUBSCRIPTION_WORK_STATES


class SubscriptionAcceptanceDispatch:
    def __init__(
        self,
        work_factory: Callable[[], UnitOfWork],
        artifacts: ArtifactStore,
        snapshot: Callable[[PreparedSubscriptionAcceptance], Awaitable[GitWorkingTreeSnapshot]],
        receipts: SubscriptionAcceptanceReceiptVerification,
    ) -> None:
        self._factory, self._store, self._snapshot, self._receipts = (
            work_factory,
            artifacts,
            snapshot,
            receipts,
        )
        self._approved = ApprovedPlanLoader(artifacts)

    async def apply(self, attempt_id: UUID) -> SubscriptionSettlement:
        try:
            return await self._apply(attempt_id)
        except Exception:
            async with self._factory() as work:
                if (
                    await work.subscription_decisions.acceptance_validation_binding(attempt_id)
                    is not None
                ):
                    return SubscriptionSettlement(True, "acceptance_validation_queued", True)
            raise

    async def _apply(self, attempt_id: UUID) -> SubscriptionSettlement:
        async with self._factory() as work:
            if (
                await work.subscription_decisions.acceptance_validation_binding(attempt_id)
                is not None
            ):
                return SubscriptionSettlement(True, "acceptance_validation_queued", True)
            prepared = await work.subscription_decisions.prepare_acceptance(attempt_id)
            if prepared.disposition != "acceptance_prepared":
                await work.commit()
                return prepared
            proposal = await work.subscription_decisions.acceptance_proposal(attempt_id)
            await work.commit()
        snapshot = await self._snapshot(proposal)
        async with self._factory() as work:
            if (
                await work.subscription_decisions.acceptance_validation_binding(attempt_id)
                is not None
            ):
                return SubscriptionSettlement(True, "acceptance_validation_queued", True)
            if CandidateInspection.from_snapshot(snapshot) != proposal.review.candidate:
                rejected = await work.subscription_decisions.reject_acceptance_mismatch(
                    proposal, snapshot
                )
                await work.commit()
                return rejected
            await work.subscription_decisions.record_acceptance_inspection(proposal, snapshot)
            await work.commit()
        try:
            proof = await self._receipts.verify(attempt_id)
        except AcceptanceReceiptClaimError:
            return await self._receipts.reject_claims(attempt_id)
        if proof is None:
            raise SubscriptionDecisionError("acceptance receipt verification is unavailable")
        async with self._factory() as work:
            if (
                await work.subscription_decisions.acceptance_validation_binding(attempt_id)
                is not None
            ):
                return SubscriptionSettlement(True, "acceptance_validation_queued", True)
            proposal = await work.subscription_decisions.acceptance_proposal(attempt_id)
            await work.rollback()
        # Receipt IO may take time; a prior matching snapshot does not establish
        # the current worktree. The queued controller will inspect it again.
        snapshot = await self._snapshot(proposal)
        wire = json.dumps(
            proof.payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        stored = await self._store.put_bytes(wire, media_type="application/json")
        if (
            stored.digest != hashlib.sha256(wire).hexdigest()
            or stored.byte_count != len(wire)
            or await self._store.open_bytes(stored.digest) != wire
        ):
            raise SubscriptionDecisionError("acceptance receipt storage differs")
        async with self._factory() as work:
            if (
                await work.subscription_decisions.acceptance_validation_binding(attempt_id)
                is not None
            ):
                return SubscriptionSettlement(True, "acceptance_validation_queued", True)
            if CandidateInspection.from_snapshot(snapshot) != proposal.review.candidate:
                rejected = await work.subscription_decisions.reject_acceptance_mismatch(
                    proposal, snapshot
                )
                await work.commit()
                return rejected
            await work.subscription_decisions.record_acceptance_inspection(proposal, snapshot)
            await work.subscription_decisions.record_acceptance_receipts(proposal, proof)
            approved = await self._approved.load(work, proposal.decision.run_id)
            if (
                not isinstance(approved.evidence, SubscriptionPlanApprovalEvidence)
                or approved.run.state not in SUBSCRIPTION_WORK_STATES
                or approved.run.version != proposal.run_version
                or approved.policy != proposal.policy
            ):
                raise SubscriptionDecisionError("acceptance validation plan authority differs")
            run = approved.run
            attempt = await work.controller_steps.next_attempt(run.id, "validate")
            payload = {"semantic_attempt": attempt, "acceptance_attempt_id": str(attempt_id)}
            command = await work.commands.enqueue(
                run_id=run.id,
                command_type="validate",
                idempotency_key=f"{run.id}:validate:{attempt}",
                payload=payload,
                expected_run_version=run.version + 1,
                actor_id=approved.approval_actor_id,
            )
            if (
                command.status is not CommandStatus.PENDING
                or command.command_type != "validate"
                or canonical_digest(command.payload) != canonical_digest(payload)
                or command.payload_schema_version != 1
                or command.expected_run_version != run.version + 1
                or command.actor_id != approved.approval_actor_id
            ):
                raise SubscriptionDecisionError("acceptance validation command differs")
            artifact = await work.artifacts.record(
                stored,
                run_id=run.id,
                producer_type="subscription_acceptance_receipts",
                producer_id=attempt_id,
                parent_digests=tuple(sorted(digest for digest, _ in proof.artifact_proofs)),
            )
            source = acceptance_intent_payload(
                proposal.decision, proposal.review, proposal.result_digest
            )
            assert source is not None
            source |= {
                "observation": proposal.review.candidate.payload(),
                "receipt_verification": proof.payload(),
            }
            binding = AcceptanceValidationBinding(
                attempt_id,
                proposal.result_digest,
                canonical_digest(source),
                proposal.review.candidate,
                artifact.digest,
                approved.approval_id,
                command,
            )
            await work.runs.transition(
                run.id,
                run.version,
                RunState.VALIDATING,
                "run.subscription_validation_requested",
                {"binding": binding.payload(), "target": RunState.VALIDATING.value},
                actor_class="worker",
                actor_id=approved.approval_actor_id,
            )
            await work.commit()
        return SubscriptionSettlement(True, "acceptance_validation_queued")
