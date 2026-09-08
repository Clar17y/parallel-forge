"""Expected-head base updates; acknowledgement alone is never completion evidence."""

from dataclasses import asdict, replace

from forge.application.ports.github import GitHubPort
from forge.application.ports.github_write import GitHubWritePort
from forge.application.ports.release import ReleaseRecord
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationRequest,
    canonical_digest,
)
from forge.domain.release import GitHubPullRequest
from forge.release.controller import ReleaseReconciliationRequired, _validate_intent


class BaseUpdateOperation:
    """Remote phase only; local adoption must separately prove commit ancestry."""

    def __init__(
        self,
        record: ReleaseRecord,
        reads: GitHubPort,
        writes: GitHubWritePort,
        base_sha: str,
        policy_version: int,
        observation_digest: str,
        remote_attempt: int,
    ) -> None:
        if (
            len(base_sha) != 40
            or any(c not in "0123456789abcdef" for c in base_sha)
            or len(observation_digest) != 64
            or any(c not in "0123456789abcdef" for c in observation_digest)
            or type(policy_version) is not int
            or policy_version < 1
            or type(remote_attempt) is not int
            or remote_attempt < 1
            or base_sha == record.pull_request.base_sha
            or record.pull_request.state != "open"
            or record.pull_request.merged
            or record.pull_request.merge_sha is not None
        ):
            raise ReleaseReconciliationRequired()
        self._record, self._reads, self._writes, self._base = record, reads, writes, base_sha
        pull = record.pull_request
        payload: dict[str, object] = {
            "pull_request_id": str(record.id),
            "node_id": pull.node_id,
            "repository": pull.base_repository,
            "pull_request_number": pull.number,
            "head_sha": pull.head_sha,
            "previous_base_sha": pull.base_sha,
            "base_sha": base_sha,
            "base_ref": pull.base_ref,
            "policy_version": policy_version,
            "observation_digest": observation_digest,
            "remote_attempt": remote_attempt,
        }
        digest = canonical_digest(payload)
        self.request = OperationRequest(
            run_id=record.run_id,
            kind="update_branch",
            idempotency_key=f"{record.run_id}:update-branch:{digest}",
            request_digest=digest,
            request_payload=payload,
        )

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        _validate_intent(intent, self.request)
        old = self._record.pull_request
        pull = await self._writes.get_pull_request(old.base_repository, old.number)
        protection = await self._reads.get_merge_protection(old.base_repository, old.base_ref)
        if (
            pull != replace(old, base_sha=self._base)
            or not protection.safe_for_managed_merge
            or await self._reads.get_base(old.base_repository, old.base_ref) != self._base
            or await self._writes.get_branch_sha(old.head_repository, old.head_ref) != old.head_sha
        ):
            raise ReleaseReconciliationRequired()
        updated = await self._writes.update_branch(old.base_repository, old.number, old.head_sha)
        return await self._outcome(updated)

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        _validate_intent(intent, self.request)
        old = self._record.pull_request
        return await self._outcome(
            await self._writes.get_pull_request(old.base_repository, old.number)
        )

    async def _outcome(self, pull: GitHubPullRequest) -> OperationOutcome:
        old = self._record.pull_request
        if (
            pull != replace(old, head_sha=pull.head_sha, base_sha=self._base)
            or pull.head_sha == old.head_sha
            or len(pull.head_sha) != 40
            or any(c not in "0123456789abcdef" for c in pull.head_sha)
            or await self._writes.get_branch_sha(old.head_repository, old.head_ref) != pull.head_sha
        ):
            raise ReleaseReconciliationRequired()
        return OperationOutcome(remote_resource_id=pull.node_id, payload=asdict(pull))
