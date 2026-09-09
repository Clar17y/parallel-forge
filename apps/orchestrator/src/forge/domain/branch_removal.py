"""Immutable branch-removal identity shared by execution and read projections."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from forge.domain.event import RunEvent
from forge.domain.operation import OperationIntent, OperationStatus, canonical_digest
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunSnapshot

BRANCH_REMOVAL_KIND = "git.branch_delete"


class BranchRemovalError(RuntimeError):
    """An immutable branch-removal operation requires reconciliation."""


class BranchSourceRejected(BranchRemovalError):
    """The source authority check rejected access before any Git operation."""


class BranchRemovalBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: UUID
    project_id: UUID
    source_command_id: UUID
    policy_version: int = Field(strict=True, ge=1)
    branch: str = Field(strict=True, min_length=1, max_length=255)
    database_enabled: bool = Field(strict=True)
    worktree_name: str = Field(strict=True, min_length=1, max_length=128)
    base_sha: str = Field(strict=True, min_length=40, max_length=40, pattern=r"^[a-f0-9]{40}$")
    expected_head: str | None = Field(
        strict=True, min_length=40, max_length=40, pattern=r"^[a-f0-9]{40}$"
    )

    def identity(self) -> WorktreeIdentity:
        identity = WorktreeIdentity.for_run(
            self.project_id, self.run_id, self.branch, self.database_enabled
        )
        if identity.worktree_name != self.worktree_name:
            raise BranchRemovalError("branch removal identity is invalid")
        return identity


def branch_removal_recorded(run: RunSnapshot, event: RunEvent, intent: OperationIntent) -> bool:
    """Validate a historical removal checkpoint against its exact successful intent."""
    try:
        binding = BranchRemovalBinding.model_validate(dict(intent.request_payload))
        binding.identity()
        payload = binding.model_dump(mode="json")
        expected_event = {
            "operation_intent_id": str(intent.id),
            "request_digest": intent.request_digest,
            "source_command_id": str(binding.source_command_id),
            "branch": binding.branch,
            "expected_head": binding.expected_head,
        }
        expected_outcome: dict[str, object] = {
            key: value for key, value in expected_event.items() if key != "operation_intent_id"
        }
        expected_outcome["removed"] = True
        return (
            event.run_id == run.id == intent.run_id == binding.run_id
            and binding.project_id == run.project_id
            and binding.policy_version == run.policy_version
            and binding.branch == run.branch_name
            and binding.base_sha == run.base_sha
            and event.event_type == "resource.branch_removed"
            and event.actor_class == "worker"
            and event.actor_id is None
            and event.payload_schema_version == 1
            and event.run_version <= run.version
            and canonical_digest(event.payload) == canonical_digest(expected_event)
            and intent.kind == BRANCH_REMOVAL_KIND
            and intent.request_schema_version == 1
            and intent.idempotency_key == f"{run.id}:branch_delete:{binding.source_command_id}"
            and intent.request_digest == canonical_digest(payload)
            and canonical_digest(intent.request_payload) == canonical_digest(payload)
            and intent.status is OperationStatus.SUCCEEDED
            and intent.outcome_schema_version == 1
            and intent.remote_resource_id is None
            and intent.outcome is not None
            and canonical_digest(intent.outcome) == canonical_digest(expected_outcome)
        )
    except ValueError, TypeError, BranchRemovalError:
        return False
