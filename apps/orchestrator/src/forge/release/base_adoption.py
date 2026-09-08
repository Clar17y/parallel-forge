"""Durable local adoption bound to an already-settled remote base update."""

from dataclasses import asdict, replace

from forge.application.ports.base_adoption import BaseAdoptionPort
from forge.application.ports.release import ReleaseRecord
from forge.application.ports.worktrees import ManagedWorktree
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationRequest,
    OperationStatus,
    canonical_digest,
)
from forge.domain.policy import ProjectPolicy
from forge.domain.release import GitHubPullRequest
from forge.release.controller import ReleaseReconciliationRequired, _validate_intent
from forge.release.git_push import _SHA


class BaseAdoptionOperation:
    def __init__(
        self,
        record: ReleaseRecord,
        update: OperationIntent,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        adoption: BaseAdoptionPort,
    ) -> None:
        try:
            pull = GitHubPullRequest(**dict(update.outcome or {}))  # type: ignore[arg-type]
            old = record.pull_request
            request = update.request_payload
            expected = {
                "pull_request_id": str(record.id),
                "node_id": old.node_id,
                "repository": old.base_repository,
                "pull_request_number": old.number,
                "head_sha": old.head_sha,
                "previous_base_sha": old.base_sha,
                "base_sha": pull.base_sha,
                "base_ref": old.base_ref,
                "policy_version": policy.version,
                "observation_digest": request.get("observation_digest"),
                "remote_attempt": request.get("remote_attempt"),
            }
            if (
                update.run_id != record.run_id
                or update.kind != "update_branch"
                or update.status is not OperationStatus.SUCCEEDED
                or update.request_schema_version != 1
                or request != expected
                or update.request_digest != canonical_digest(request)
                or update.idempotency_key
                != f"{record.run_id}:update-branch:{update.request_digest}"
                or update.remote_resource_id != old.node_id
                or pull != replace(old, head_sha=pull.head_sha, base_sha=pull.base_sha)
                or pull.head_sha == old.head_sha
                or pull.base_sha == old.base_sha
                or _SHA.fullmatch(pull.head_sha) is None
                or _SHA.fullmatch(pull.base_sha) is None
                or pull.state != "open"
                or pull.merged
                or pull.merge_sha is not None
                or worktree.identity.run_id != record.run_id
                or worktree.identity.project_id != policy.id
                or worktree.identity.branch != pull.head_ref
                or policy.github_repository != pull.base_repository
                or policy.default_branch != pull.base_ref
            ):
                raise ReleaseReconciliationRequired()
        except TypeError, ValueError:
            raise ReleaseReconciliationRequired() from None
        self._record, self._pull, self._tree, self._policy, self._adoption = (
            record,
            pull,
            worktree,
            policy,
            adoption,
        )
        payload = dict(request) | {
            "update_intent_id": str(update.id),
            "head_sha": pull.head_sha,
            "previous_head_sha": old.head_sha,
        }
        digest = canonical_digest(payload)
        self.request = OperationRequest(
            run_id=record.run_id,
            kind="adopt_base",
            idempotency_key=f"{record.run_id}:adopt-base:{digest}",
            request_digest=digest,
            request_payload=payload,
        )

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        _validate_intent(intent, self.request)
        await self._adoption.adopt(
            self._tree,
            self._policy,
            self._record.pull_request.head_sha,
            self._pull.head_sha,
            self._pull.base_sha,
        )
        return self._outcome()

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        _validate_intent(intent, self.request)
        await self._adoption.inspect(
            self._tree,
            self._policy,
            self._record.pull_request.head_sha,
            self._pull.head_sha,
            self._pull.base_sha,
        )
        return self._outcome()

    def _outcome(self) -> OperationOutcome:
        return OperationOutcome(remote_resource_id=self._pull.node_id, payload=asdict(self._pull))
