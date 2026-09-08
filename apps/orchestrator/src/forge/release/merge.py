"""Exact-evidence merge preflight and observation-only recovery."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from uuid import UUID

from forge.application.ports.github import GitHubPort
from forge.application.ports.github_write import GitHubWritePort
from forge.application.ports.release import ReleaseRecord
from forge.domain.approval import MergeApprovalEvidence
from forge.domain.approval import canonical_digest as evidence_digest
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationRequest,
    OperationStatus,
    canonical_digest,
)
from forge.domain.release import GitHubPullRequest
from forge.release.controller import ReleaseReconciliationRequired, _validate_intent
from forge.release.github_write import GitHubWriteError
from forge.release.monitor import assess_checks, required_check_results


class StaleMergeEvidence(ReleaseReconciliationRequired):
    pass


class MergeController:
    def __init__(self, github: GitHubPort, writes: GitHubWritePort) -> None:
        self._github = github
        self._writes = writes

    async def preflight(
        self,
        record: ReleaseRecord,
        approved: MergeApprovalEvidence,
        current: MergeApprovalEvidence,
    ) -> GitHubPullRequest:
        if (
            evidence_digest(current) != evidence_digest(approved)
            or approved.merge_method not in {"merge", "squash", "rebase"}
            or approved.unresolved_blocking_findings != 0
            or not approved.required_checks
            or any(value != "success" for value in approved.required_checks.values())
        ):
            raise StaleMergeEvidence()
        repository, number = approved.repository, approved.pull_request_number
        base = approved.base_ref.removeprefix("refs/heads/")
        pull = await self._writes.get_pull_request(repository, number)
        self._identity(record, approved, pull)
        if (
            pull.state != "open"
            or pull.merged
            or pull.merge_sha is not None
            or pull.base_sha != approved.base_sha
            or await self._writes.get_branch_sha(repository, pull.head_ref) != approved.head_sha
            or await self._github.get_base(repository, base) != approved.base_sha
        ):
            raise StaleMergeEvidence()
        checks = await self._github.get_checks(repository, approved.head_sha)
        reviews = await self._github.get_reviews(repository, number)
        if any(review.blocks_merge for review in reviews):
            raise StaleMergeEvidence()
        protection = await self._github.get_merge_protection(repository, base)
        if (
            (
                protection.merge_queue_enabled
                and protection.merge_queue_method != approved.merge_method
            )
            or assess_checks(approved.head_sha, checks, reviews, protection).disposition != "ready"
            or required_check_results(checks, protection) != dict(approved.required_checks)
            or canonical_digest(asdict(protection)) != approved.protection_digest
        ):
            raise StaleMergeEvidence()
        return pull

    async def merge(self, approved: MergeApprovalEvidence) -> GitHubPullRequest:
        # GitHub enforces this SHA atomically. Never retry a stale/uncertain PUT.
        return await self._writes.merge_pull_request(
            approved.repository,
            approved.pull_request_number,
            approved.head_sha,
            approved.merge_method,
        )

    async def reconcile(
        self, record: ReleaseRecord, approved: MergeApprovalEvidence
    ) -> OperationOutcome:
        pull = await self._writes.get_pull_request(
            approved.repository, approved.pull_request_number
        )
        return self.outcome(record, approved, pull)

    def outcome(
        self, record: ReleaseRecord, approved: MergeApprovalEvidence, pull: GitHubPullRequest
    ) -> OperationOutcome:
        self._identity(record, approved, pull)
        if not pull.merged or pull.state != "closed" or pull.merge_sha is None:
            raise ReleaseReconciliationRequired()
        if len(pull.merge_sha) != 40 or any(c not in "0123456789abcdef" for c in pull.merge_sha):
            raise ReleaseReconciliationRequired()
        return OperationOutcome(remote_resource_id=pull.node_id, payload=asdict(pull))

    @staticmethod
    def _identity(
        record: ReleaseRecord, approved: MergeApprovalEvidence, pull: GitHubPullRequest
    ) -> None:
        expected = record.pull_request
        if (
            pull.number != approved.pull_request_number
            or pull.number != expected.number
            or pull.node_id != expected.node_id
            or pull.url != expected.url
            or pull.head_repository != approved.repository
            or pull.head_repository != expected.head_repository
            or pull.base_repository != approved.repository
            or pull.base_repository != expected.base_repository
            or pull.head_ref != expected.head_ref
            or pull.head_sha != approved.head_sha
            or pull.base_ref != approved.base_ref.removeprefix("refs/heads/")
            or pull.base_ref != expected.base_ref
        ):
            raise StaleMergeEvidence()


class MergeOperation:
    def __init__(
        self,
        controller: MergeController,
        record: ReleaseRecord,
        approval_id: UUID,
        approved: MergeApprovalEvidence,
        current_evidence: Callable[[], Awaitable[MergeApprovalEvidence]],
    ) -> None:
        self._controller, self._record = controller, record
        self._approved, self._current = approved, current_evidence
        payload: dict[str, object] = {
            "approval_id": str(approval_id),
            "approval_digest": evidence_digest(approved),
            "policy_version": approved.policy_version,
            "repository": approved.repository,
            "pull_request_number": approved.pull_request_number,
            "node_id": record.pull_request.node_id,
            "head_sha": approved.head_sha,
            "base_ref": approved.base_ref,
            "base_sha": approved.base_sha,
            "merge_method": approved.merge_method,
        }
        digest = canonical_digest(payload)
        self.request = OperationRequest(
            run_id=record.run_id,
            kind="merge_pr",
            idempotency_key=f"{record.run_id}:merge_pr:{digest}",
            request_digest=digest,
            request_payload=payload,
        )

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        _validate_intent(intent, self.request)
        try:
            await self._controller.preflight(self._record, self._approved, await self._current())
        except StaleMergeEvidence:
            return OperationOutcome(status=OperationStatus.FAILED, error="merge_preflight_rejected")
        try:
            pull = await self._controller.merge(self._approved)
        except GitHubWriteError as error:
            if error.category not in {"stale", "rejected"}:
                raise
            return OperationOutcome(status=OperationStatus.FAILED, error="merge_remote_rejected")
        return self._controller.outcome(self._record, self._approved, pull)

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        _validate_intent(intent, self.request)
        return await self._controller.reconcile(self._record, self._approved)
