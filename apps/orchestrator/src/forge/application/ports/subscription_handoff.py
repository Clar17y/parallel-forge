"""Immutable evidence bindings consumed by the locked handoff transaction."""

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.tool_recovery import VerifiedTerminalEffect, _value
from forge.application.ports.worktrees import ManagedWorktree
from forge.domain.artifact import ArtifactDescriptor, thaw_metadata, validate_artifact_digest
from forge.domain.operation import canonical_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.subscription import (
    LogicalTaskContract,
    TaskHandoff,
    ToolCallBinding,
    encode_subscription_record,
)


@dataclass(frozen=True, slots=True)
class SettledSubscriptionHandoff:
    """Current retained source, still requiring evidence and locked application.

    Loading this proposal leaves the task reconciling and retains its ownership.
    It does not provide a whole-tree snapshot fence or authorize completion.
    """

    task: LogicalTaskContract
    handoff: TaskHandoff
    result_digest: str
    policy: ProjectPolicy
    worktree: ManagedWorktree
    candidate_epoch: int
    task_version: int
    selected_candidate: CandidateInspection | None = None


@dataclass(frozen=True, slots=True)
class HandoffObservation:
    proposal: SettledSubscriptionHandoff
    token: UUID
    expires_at: datetime


def operation_evidence_digest(binding: ToolCallBinding, receipt: Mapping[str, object]) -> str:
    return canonical_digest({"binding": encode_subscription_record(binding), "receipt": receipt})


def artifact_descriptor_digest(descriptor: ArtifactDescriptor) -> str:
    return canonical_digest(
        {
            "digest": descriptor.digest,
            "media_type": descriptor.media_type,
            "byte_count": descriptor.byte_count,
            "storage_pointer": descriptor.storage_pointer,
            "run_id": str(descriptor.run_id),
            "producer_type": descriptor.producer_type,
            "producer_id": str(descriptor.producer_id),
            "parent_digests": descriptor.parent_digests,
            "schema_version": descriptor.schema_version,
            "created_at": descriptor.created_at.isoformat(),
            "metadata": thaw_metadata(descriptor.metadata),
            "truncated": descriptor.truncated,
            "original_byte_count": descriptor.original_byte_count,
            "truncation_policy": descriptor.truncation_policy,
            "artifact_id": str(descriptor.artifact_id),
        }
    )


@dataclass(frozen=True, slots=True)
class HandoffCallProof:
    call_id: UUID
    call_digest: str
    receipt_digest: str
    terminal: VerifiedTerminalEffect | None = None


@dataclass(frozen=True, slots=True)
class VerifiedSubscriptionHandoff:
    """Historical observations, not acceptance authority.

    The dispatcher must recheck every binding and current owned outputs under
    its fences. checks_match_snapshot requires matching before/after tree
    observations for every named check. output_digest binds the task's observed
    file scope. current_tree_digest is absent for historical-only verification;
    when present it records a supplied current snapshot with equal scoped outputs.
    Neither observation replaces locked source/candidate checks or human approval.
    """

    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    snapshot_call_id: UUID
    manifest_digest: str
    candidate_tree_digest: str
    policy_version: int
    task_digest: str
    handoff_digest: str
    call_proofs: tuple[HandoffCallProof, ...]
    artifact_proofs: tuple[tuple[str, str], ...]
    checks_match_snapshot: bool
    output_digest: str
    current_tree_digest: str | None = None


def handoff_claim_error(
    handoff: TaskHandoff,
    task: LogicalTaskContract,
    selected_candidate: CandidateInspection | None = None,
) -> str | None:
    """Prove intrinsic claim invalidity only; absent external evidence is not a reason."""
    if selected_candidate is not None and (
        handoff.candidate_tree_digest != selected_candidate.tree_digest
        or (
            handoff.candidate_commit is not None
            and handoff.candidate_commit != selected_candidate.head_sha
        )
    ):
        return "candidate_claim_differs"
    values = handoff.evidence_receipt_ids
    try:
        identities = tuple(UUID(value) for value in values)
    except ValueError:
        return "invalid_receipt_claims"
    if (
        not 1 <= len(values) <= 128
        or len(set(identities)) != len(identities)
        or any(str(identity) != value for identity, value in zip(identities, values, strict=True))
    ):
        return "invalid_receipt_claims"
    checks = handoff.check_results
    # Ordered history can retain failed checks followed by a successful repair.
    # The verifier proves that order against the retained command timestamps.
    latest = {item.command_name: item for item in checks}
    receipts = {item.receipt_id for item in checks}
    required = set(task.named_checks) | {
        name for criterion in task.typed_acceptance for name in criterion.required_check_names
    }
    if (
        len(receipts) != len(checks)
        or not receipts <= set(values)
        or not required <= set(latest)
        or any(not item.passed for item in latest.values())
    ):
        return "invalid_check_claims"
    return None


class HandoffRejectionReason(StrEnum):
    OUTPUTS_CHANGED = "outputs_changed"
    COMMIT_CHANGED = "commit_changed"
    CHECKS_NOT_BOUND = "checks_not_bound"


@dataclass(frozen=True, slots=True)
class RejectedSubscriptionHandoff:
    """Verified historical evidence plus a current observation of a definite mismatch."""

    verified: VerifiedSubscriptionHandoff
    current_tree_digest: str
    current_output_digest: str
    current_head_sha: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.verified, VerifiedSubscriptionHandoff)
            or self.verified.current_tree_digest is not None
        ):
            raise ValueError("rejection requires historical verified evidence")
        validate_artifact_digest(self.current_tree_digest)
        validate_artifact_digest(self.current_output_digest)
        if (
            not isinstance(self.current_head_sha, str)
            or re.fullmatch("[0-9a-f]{40}", self.current_head_sha) is None
        ):
            raise ValueError("current head must be a canonical commit SHA")

    def reason(self, handoff: TaskHandoff) -> HandoffRejectionReason | None:
        if self.current_output_digest != self.verified.output_digest:
            return HandoffRejectionReason.OUTPUTS_CHANGED
        if (
            handoff.candidate_commit is not None
            and handoff.candidate_commit != self.current_head_sha
        ):
            return HandoffRejectionReason.COMMIT_CHANGED
        if self.verified.checks_match_snapshot is False:
            return HandoffRejectionReason.CHECKS_NOT_BOUND
        return None


def verified_handoff_digest(
    proof: VerifiedSubscriptionHandoff | RejectedSubscriptionHandoff,
) -> str:
    return canonical_digest({key: _value(value) for key, value in asdict(proof).items()})
