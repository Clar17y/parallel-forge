"""Persisted remote identity and the exact operation receipts that created it."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from forge.domain.release import GitHubPullRequest


@dataclass(frozen=True, slots=True)
class ReleaseRecord:
    id: UUID
    run_id: UUID
    pull_request: GitHubPullRequest
    push_intent_id: UUID
    publication_intent_id: UUID
    merge_intent_id: UUID | None
    reviewed_push_intent_id: UUID | None = None
    candidate_evidence_digest: str | None = None
    base_update_intent_id: UUID | None = None
    base_adoption_intent_id: UUID | None = None


class ReleaseRepository(Protocol):
    async def record_observation(
        self, run_id: UUID, source_command_id: UUID, digest: str, wire: bytes
    ) -> None: ...

    async def record_base_update(
        self,
        run_id: UUID,
        pull_request: GitHubPullRequest,
        update_intent_id: UUID,
        adoption_intent_id: UUID,
    ) -> ReleaseRecord: ...

    async def record_merge(
        self, run_id: UUID, pull_request: GitHubPullRequest, merge_intent_id: UUID
    ) -> ReleaseRecord: ...

    async def record_reviewed_push(
        self, run_id: UUID, pull_request: GitHubPullRequest, push_intent_id: UUID
    ) -> ReleaseRecord: ...

    async def get_for_run(self, run_id: UUID) -> ReleaseRecord | None: ...

    async def record_publication(
        self,
        run_id: UUID,
        pull_request: GitHubPullRequest,
        push_intent_id: UUID,
        publication_intent_id: UUID,
    ) -> ReleaseRecord: ...
