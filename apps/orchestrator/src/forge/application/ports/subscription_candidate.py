"""Prepared candidate context and trusted Git observation, never approval."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from forge.application.ports.worktrees import GitWorkingTreeSnapshot, ManagedWorktree
from forge.domain.artifact import validate_artifact_digest
from forge.domain.operation import canonical_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.subscription import ReviewSelection


@dataclass(frozen=True, slots=True)
class CandidateInspection:
    head_sha: str
    base_sha: str
    tree_digest: str
    manifest_digest: str

    def __post_init__(self) -> None:
        GitWorkingTreeSnapshot(
            head_sha=self.head_sha, base_sha=self.base_sha, files=(), changed_paths=()
        )
        validate_artifact_digest(self.tree_digest)
        validate_artifact_digest(self.manifest_digest)

    @classmethod
    def from_snapshot(cls, snapshot: GitWorkingTreeSnapshot) -> CandidateInspection:
        return cls(
            snapshot.head_sha,
            snapshot.base_sha,
            snapshot.candidate_tree_digest,
            canonical_digest(snapshot.manifest()),
        )

    def payload(self) -> dict[str, object]:
        return {
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "tree_digest": self.tree_digest,
            "manifest_digest": self.manifest_digest,
        }

    @classmethod
    def from_payload(cls, payload: object) -> CandidateInspection:
        keys = ("head_sha", "base_sha", "tree_digest", "manifest_digest")
        if not isinstance(payload, Mapping) or set(payload) != set(keys):
            raise ValueError("candidate observation shape differs")
        values = tuple(payload[key] for key in keys)
        if not all(isinstance(value, str) for value in values):
            raise ValueError("candidate observation values differ")
        return cls(*values)


@dataclass(frozen=True, slots=True)
class PreparedReviewSelection:
    attempt_id: UUID
    selection: ReviewSelection
    result_digest: str
    candidate_epoch: int
    task_version: int
    run_version: int
    policy: ProjectPolicy
    worktree: ManagedWorktree
    inspection: CandidateInspection | None = None
