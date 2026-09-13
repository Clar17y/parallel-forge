"""Reconstruct historical dispatch and current validation authority separately."""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_acceptance import (
    PreparedSubscriptionAcceptance,
    RetainedSubscriptionAcceptance,
)
from forge.application.ports.subscription_acceptance_receipts import VerifiedAcceptanceReceipts
from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_validation import AcceptanceValidationBinding
from forge.application.ports.worktrees import ManagedWorktree
from forge.domain.operation import canonical_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.subscription import AcceptDecision, decode_subscription_record
from forge.persistence.models import Approval, RunEvent
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.artifacts import ArtifactRepository
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.projects import PostgresProjectRepository
from forge.persistence.repositories.runs import PostgresRunRepository
from forge.persistence.repositories.subscription_acceptance import (
    acceptance_evidence_proposal,
    acceptance_remote_proposal,
    acceptance_revision_proposal,
)
from forge.persistence.repositories.subscription_acceptance_receipts import (
    _receipt_sources,
    verify_acceptance_receipt_evidence,
)
from forge.persistence.repositories.subscription_review import historical_candidate_review_evidence

EVENT = "run.subscription_validation_requested"


async def acceptance_validation_binding(
    session: AsyncSession,
    attempt_id: UUID,
) -> AcceptanceValidationBinding | None:
    from forge.persistence.repositories.subscription_decisions import (
        PostgresSubscriptionDecisionRepository,
    )

    attempt = await session.get(SubscriptionAttempt, attempt_id)
    if attempt is None:
        raise SubscriptionDecisionError("acceptance dispatch source is absent")
    run = await PostgresRunRepository(session).get_for_update(attempt.run_id)
    events = tuple(
        (
            await session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == run.id,
                    RunEvent.event_type == EVENT,
                    RunEvent.payload["binding"]["source_attempt_id"].astext == str(attempt_id),
                )
            )
        ).all()
    )
    if not events:
        return None
    await PostgresSubscriptionDecisionRepository(session).prepare_acceptance(attempt_id)
    result = await session.get(SubscriptionAttemptResult, attempt_id, populate_existing=True)
    try:
        if len(events) != 1 or result is None or result.disposition != "acceptance_prepared":
            raise ValueError
        event = events[0]
        values = event.payload["binding"]
        source = result.application_payload
        if not isinstance(values, Mapping) or source is None:
            raise ValueError
        command_values = values["command"]
        if not isinstance(command_values, Mapping):
            raise TypeError
        command = await PostgresCommandRepository(session=session).get(
            UUID(str(command_values["id"]))
        )
        approval = await session.get(Approval, UUID(str(values["approval_id"])))
        semantic_attempt = command.payload.get("semantic_attempt")
        if (
            approval is None
            or approval.run_id != run.id
            or approval.gate != "plan"
            or approval.authenticated_actor_id != command.actor_id
            or approval.run_version >= event.run_version
            or command.run_id != run.id
            or command.command_type != "validate"
            or type(semantic_attempt) is not int
            or semantic_attempt < 1
            or command.idempotency_key != f"{run.id}:validate:{semantic_attempt}"
            or command.payload
            != {"semantic_attempt": semantic_attempt, "acceptance_attempt_id": str(attempt_id)}
            or command.payload_schema_version != 1
            or command.expected_run_version != event.run_version
            or event.actor_class != "worker"
            or event.actor_id != command.actor_id
            or event.payload_schema_version != 1
            or event.run_version > run.version
            or event.payload.get("target") != "VALIDATING"
        ):
            raise ValueError
        candidate = CandidateInspection.from_payload(source["observation"])
        review_sources = source["review_sources"]
        if not isinstance(review_sources, Mapping) or candidate != CandidateInspection.from_payload(
            review_sources["candidate"]
        ):
            raise ValueError
        proof = VerifiedAcceptanceReceipts.from_payload(source["receipt_verification"])
        receipt_digest = await _receipt_artifact(session, run.id, attempt_id, proof)
        binding = AcceptanceValidationBinding(
            attempt_id,
            result.result_digest,
            canonical_digest(source),
            candidate,
            receipt_digest,
            approval.id,
            command,
        )
        if result.application_digest != binding.application_digest or canonical_digest(
            values
        ) != canonical_digest(binding.payload()):
            raise ValueError
        return binding
    except KeyError, TypeError, ValueError:
        raise SubscriptionDecisionError("acceptance validation dispatch differs") from None


async def acceptance_validation_source(
    session: AsyncSession,
    attempt_id: UUID,
) -> tuple[PreparedSubscriptionAcceptance, VerifiedAcceptanceReceipts]:
    proposal = await acceptance_evidence_proposal(session, attempt_id)
    return proposal, await _current_acceptance_receipts(session, proposal)


async def acceptance_revision_source(
    session: AsyncSession, attempt_id: UUID
) -> tuple[PreparedSubscriptionAcceptance, VerifiedAcceptanceReceipts]:
    proposal = await acceptance_revision_proposal(session, attempt_id)
    return proposal, await _current_acceptance_receipts(session, proposal)


async def acceptance_remote_source(
    session: AsyncSession, attempt_id: UUID, *, allow_paused: bool = False
) -> tuple[PreparedSubscriptionAcceptance, VerifiedAcceptanceReceipts]:
    proposal = await acceptance_remote_proposal(session, attempt_id, allow_paused=allow_paused)
    return proposal, await _current_acceptance_receipts(session, proposal)


async def _current_acceptance_receipts(
    session: AsyncSession, proposal: PreparedSubscriptionAcceptance
) -> VerifiedAcceptanceReceipts:
    result = await session.get(SubscriptionAttemptResult, proposal.attempt_id)
    assert result is not None and result.application_payload is not None
    proof = VerifiedAcceptanceReceipts.from_payload(
        result.application_payload.get("receipt_verification")
    )
    if proposal.inspection != proposal.review.candidate:
        raise SubscriptionDecisionError("acceptance validation candidate differs")
    await verify_acceptance_receipt_evidence(
        session, proposal, proof, await _receipt_sources(session, proposal)
    )
    await _receipt_artifact(session, proposal.decision.run_id, proposal.attempt_id, proof)
    return proof


async def retained_acceptance_source(
    session: AsyncSession, attempt_id: UUID
) -> RetainedSubscriptionAcceptance:
    """Re-prove frozen sources and receipts without requiring a current phase."""
    binding = await acceptance_validation_binding(session, attempt_id)
    if binding is None:
        raise SubscriptionDecisionError("acceptance was not dispatched")
    attempt = await session.get(SubscriptionAttempt, attempt_id, populate_existing=True)
    result = await session.get(SubscriptionAttemptResult, attempt_id, populate_existing=True)
    assert attempt is not None and result is not None and result.application_payload is not None
    run = await PostgresRunRepository(session).get_for_update(attempt.run_id)
    approval = await session.get(Approval, binding.approval_id)
    assert approval is not None
    policy_record = await PostgresProjectRepository(session).get_policy(
        run.project_id, approval.policy_version
    )
    policy = ProjectPolicy.model_validate(policy_record.document)
    if (
        (policy.id, policy.version) != (run.project_id, approval.policy_version)
        or policy_record.document_schema_version != 1
        or policy_record.policy_digest != canonical_digest(policy_record.document)
        or not run.worktree_path
        or not run.branch_name
        or type(attempt.candidate_epoch) is not int
    ):
        raise SubscriptionDecisionError("retained acceptance context differs")
    decision_payload = result.result_payload["decision"]
    if not isinstance(decision_payload, Mapping):
        raise SubscriptionDecisionError("retained acceptance decision shape differs")
    decision = decode_subscription_record(decision_payload)
    review = await historical_candidate_review_evidence(
        session, run.id, attempt.task_row_id, attempt.candidate_epoch
    )
    if (
        not isinstance(decision, AcceptDecision)
        or review is None
        or review.candidate != binding.candidate
        or decision.task_id != attempt.task_row_id
        or decision.run_id != run.id
    ):
        raise SubscriptionDecisionError("retained acceptance source differs")
    proof = VerifiedAcceptanceReceipts.from_payload(
        result.application_payload["receipt_verification"]
    )
    source = RetainedSubscriptionAcceptance(
        attempt_id,
        decision,
        result.result_digest,
        review,
        policy,
        ManagedWorktree(
            identity=WorktreeIdentity.for_run(
                run.project_id, run.id, run.branch_name, policy.database.enabled
            ),
            path=Path(run.worktree_path),
            base_sha=binding.candidate.base_sha,
        ),
        proof,
    )
    await verify_acceptance_receipt_evidence(
        session, source, proof, await _receipt_sources(session, source)
    )
    await _receipt_artifact(session, run.id, attempt_id, proof)
    return source


async def _receipt_artifact(
    session: AsyncSession,
    run_id: UUID,
    attempt_id: UUID,
    proof: VerifiedAcceptanceReceipts,
) -> str:
    wire = json.dumps(
        proof.payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    digest = hashlib.sha256(wire).hexdigest()
    artifact = await ArtifactRepository(session=session).get_by_digest(digest, run_id=run_id)
    if (
        artifact.producer_type != "subscription_acceptance_receipts"
        or artifact.producer_id != attempt_id
        or artifact.media_type != "application/json"
        or artifact.schema_version != 1
        or artifact.byte_count != len(wire)
        or artifact.truncated
        or artifact.parent_digests != tuple(sorted(digest for digest, _ in proof.artifact_proofs))
    ):
        raise SubscriptionDecisionError("acceptance validation receipt artifact differs")
    return digest
