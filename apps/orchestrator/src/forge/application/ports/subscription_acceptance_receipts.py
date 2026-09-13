"""Historical receipt evidence for final acceptance; never approval authority."""

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.subscription_handoff import HandoffCallProof
from forge.application.ports.tools import ToolCallRecord
from forge.domain.artifact import validate_artifact_digest
from forge.domain.subscription import LogicalTaskContract
from forge.domain.tool import ToolName
from pydantic import TypeAdapter


class ReceiptClaimErrorKind(StrEnum):
    ABSENT_OR_FOREIGN = "absent_or_foreign"
    NON_SUBSCRIPTION_PRODUCER = "non_subscription_producer"
    AMBIGUOUS_CALLBACK = "ambiguous_callback"
    CALLBACK_BINDING_DIFFERS = "callback_binding_differs"
    UNSUPPORTED_TOOL = "unsupported_tool"


class AcceptanceReceiptClaimError(ValueError):
    """A stopped, quiescent source cites absent, foreign or incompatible receipts.

    This is distinct from unavailable artifact storage or damaged retained source
    evidence. A caller must reprove the claim under current authority before repair.
    """

    def __init__(self, kind: ReceiptClaimErrorKind, receipt_id: UUID) -> None:
        if not isinstance(kind, ReceiptClaimErrorKind):
            raise TypeError("receipt claim error kind is invalid")
        if not isinstance(receipt_id, UUID) or not receipt_id.int:
            raise ValueError("receipt claim identifier is invalid")
        self.kind, self.receipt_id = kind, receipt_id
        super().__init__(f"acceptance receipt {receipt_id}: {kind.value.replace('_', ' ')}")

    def payload(self) -> dict[str, object]:
        return {"receipt_id": str(self.receipt_id), "claim_error": self.kind.value}

    @classmethod
    def from_payload(cls, value: object) -> AcceptanceReceiptClaimError:
        if not isinstance(value, dict):
            raise TypeError("receipt claim rejection must be an object")
        identity, kind = value.get("receipt_id"), value.get("claim_error")
        if not isinstance(identity, str) or not isinstance(kind, str):
            raise TypeError("receipt claim rejection fields must be strings")
        issue = cls(ReceiptClaimErrorKind(kind), UUID(identity))
        if issue.payload() != value:
            raise ValueError("receipt claim rejection is not canonical")
        return issue


@dataclass(frozen=True, slots=True)
class AcceptanceReceiptSource:
    call: ToolCallRecord
    task: LogicalTaskContract
    producer_digest: str
    receipt_digest: str


@dataclass(frozen=True, slots=True)
class AcceptanceReceiptProof:
    """Exact producer evidence with optional selected-tree binding.

    Snapshots bind the full candidate identity; named checks need equal before
    and after tree digests. Commit receipts retain their proven commit SHA but
    cannot certify a potentially dirty working tree from its HEAD alone.
    """

    task_id: UUID
    attempt_id: UUID
    producer_digest: str
    tool_name: ToolName
    call: HandoffCallProof
    snapshot: CandidateInspection | None = None
    command_name: str | None = None
    command_result_digest: str | None = None
    commit_sha: str | None = None
    matches_candidate: bool = False

    def __post_init__(self) -> None:
        validate_artifact_digest(self.producer_digest)
        validate_artifact_digest(self.call.call_digest)
        validate_artifact_digest(self.call.receipt_digest)
        if not self.task_id.int or not self.attempt_id.int or not self.call.call_id.int:
            raise ValueError("receipt identities must be non-nil")
        if type(self.matches_candidate) is not bool:
            raise ValueError("receipt candidate binding must be boolean")
        if self.tool_name is ToolName.GIT_DIFF:
            if self.snapshot is None or self.call.terminal is not None:
                raise ValueError("snapshot receipt shape differs")
        elif self.tool_name is ToolName.BUILD_RUN_NAMED_CHECK:
            if not self.command_name or self.command_result_digest is None:
                raise ValueError("check receipt shape differs")
            validate_artifact_digest(self.command_result_digest)
        elif self.tool_name is ToolName.GIT_COMMIT:
            if (
                self.commit_sha is None
                or re.fullmatch("[0-9a-f]{40}", self.commit_sha) is None
                or self.matches_candidate
            ):
                raise ValueError("commit receipt shape differs")
        else:
            raise ValueError("unsupported acceptance receipt")
        if (
            (self.snapshot is not None) != (self.tool_name is ToolName.GIT_DIFF)
            or (self.command_name is not None) != (self.tool_name is ToolName.BUILD_RUN_NAMED_CHECK)
            or (self.command_result_digest is not None)
            != (self.tool_name is ToolName.BUILD_RUN_NAMED_CHECK)
            or (self.commit_sha is not None) != (self.tool_name is ToolName.GIT_COMMIT)
            or (self.call.terminal is not None) != (self.tool_name is not ToolName.GIT_DIFF)
        ):
            raise ValueError("receipt proof shape differs")


@dataclass(frozen=True, slots=True)
class VerifiedAcceptanceReceipts:
    result_digest: str
    review_digest: str
    receipts: tuple[AcceptanceReceiptProof, ...]
    artifact_proofs: tuple[tuple[str, str], ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        validate_artifact_digest(self.result_digest)
        validate_artifact_digest(self.review_digest)
        ids = [item.call.call_id for item in self.receipts]
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or not 1 <= len(ids) <= 128
            or len(set(ids)) != len(ids)
            or not 1 <= len(self.artifact_proofs) <= 256
            or len(dict(self.artifact_proofs)) != len(self.artifact_proofs)
        ):
            raise ValueError("acceptance receipt proof bounds differ")
        for digest, descriptor_digest in self.artifact_proofs:
            validate_artifact_digest(digest)
            validate_artifact_digest(descriptor_digest)

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = _ADAPTER.dump_python(self, mode="json")
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> VerifiedAcceptanceReceipts:
        proof = _ADAPTER.validate_json(json.dumps(payload), strict=True)
        # Reject unknown keys and noncanonical UUID/enum/collection encodings.
        if proof.payload() != payload:
            raise ValueError("acceptance receipt payload is not canonical")
        return proof


_ADAPTER = TypeAdapter(VerifiedAcceptanceReceipts)
