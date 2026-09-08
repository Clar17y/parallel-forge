"""Exact-candidate release effects behind durable operation admission.

These adapters never authorize a release. The application service must consume
the existing human approval and commit its operation intent before invocation.
Reconciliation observes remote state without repeating uncertain writes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from uuid import UUID

from forge.application.ports.git_push import ManagedPushPort
from forge.application.ports.github_write import GitHubWritePort
from forge.application.ports.release import ReleaseRecord
from forge.application.ports.worktrees import ManagedWorktree
from forge.domain.approval import PrApprovalEvidence
from forge.domain.approval import canonical_digest as evidence_digest
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationRequest,
    canonical_digest,
)
from forge.domain.policy import ProjectPolicy
from forge.domain.release import GitHubPullRequest


class ReleaseReconciliationRequired(RuntimeError):
    def __init__(self) -> None:
        super().__init__("release operation requires authoritative reconciliation")


@dataclass(frozen=True, slots=True, kw_only=True)
class Publication:
    run_id: UUID
    approval_id: UUID
    policy_version: int
    branch: str
    evidence: PrApprovalEvidence

    @property
    def base_branch(self) -> str:
        # Approval evidence uses canonical Git refs; GitHub PRs use branch names.
        return self.evidence.base_ref.removeprefix("refs/heads/")

    def request(self, kind: str) -> OperationRequest:
        if kind not in {"push_branch", "create_pr"}:
            raise ReleaseReconciliationRequired()
        payload: dict[str, object] = {
            "approval_id": str(self.approval_id),
            "approval_digest": evidence_digest(self.evidence),
            "policy_version": self.policy_version,
            "repository": self.evidence.repository,
            "branch": self.branch,
            "head_sha": self.evidence.candidate_commit,
            "base_ref": self.evidence.base_ref,
            "base_sha": self.evidence.base_sha,
        }
        digest = canonical_digest(payload)
        return OperationRequest(
            run_id=self.run_id,
            kind=kind,
            idempotency_key=f"{self.run_id}:{kind}:{digest}",
            request_digest=digest,
            request_payload=payload,
        )


def _validate_intent(intent: OperationIntent, request: OperationRequest) -> None:
    if (
        intent.run_id != request.run_id
        or intent.kind != request.kind
        or intent.idempotency_key != request.idempotency_key
        or intent.request_digest != request.request_digest
        or intent.request_payload != request.request_payload
        or intent.request_schema_version != request.request_schema_version
    ):
        raise ReleaseReconciliationRequired()


class PushOperation:
    def __init__(
        self,
        publication: Publication,
        github: GitHubWritePort,
        push: ManagedPushPort,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
    ) -> None:
        evidence = publication.evidence
        if (
            worktree.identity.run_id != publication.run_id
            or worktree.identity.project_id != policy.id
            or worktree.identity.branch != publication.branch
            or policy.version != publication.policy_version
            or policy.github_repository != evidence.repository
            or policy.default_branch != publication.base_branch
            or policy.runner_mode != evidence.runner_mode
            or policy.remote_remediation_limit != evidence.remote_remediation_limit
        ):
            raise ReleaseReconciliationRequired()
        self._publication = publication
        self._github = github
        self._push = push
        self._worktree = worktree
        self._policy = policy
        self._base_sha = evidence.base_sha
        self.request = publication.request("push_branch")

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        _validate_intent(intent, self.request)
        evidence = self._publication.evidence
        if (
            await self._github.get_branch_sha(evidence.repository, self._publication.base_branch)
            != self._base_sha
        ):
            raise ReleaseReconciliationRequired()
        await self._push.push(self._worktree, self._policy, evidence.candidate_commit)
        return await self.reconcile(intent)

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        _validate_intent(intent, self.request)
        evidence = self._publication.evidence
        observed = await self._github.get_branch_sha(evidence.repository, self._publication.branch)
        if observed != evidence.candidate_commit:
            raise ReleaseReconciliationRequired()
        return OperationOutcome(
            remote_resource_id=f"{evidence.repository}:refs/heads/{self._publication.branch}",
            payload={
                "repository": evidence.repository,
                "branch": self._publication.branch,
                "head_sha": observed,
            },
        )


class ReviewedPushOperation(PushOperation):
    """Update one existing PR, retaining original approval and new review identities."""

    def __init__(
        self,
        record: ReleaseRecord,
        approval_id: UUID,
        approval_digest: str,
        candidate: PrApprovalEvidence,
        github: GitHubWritePort,
        push: ManagedPushPort,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
    ) -> None:
        pull = record.pull_request
        if (
            pull.base_repository != candidate.repository
            or pull.head_repository != candidate.repository
            or pull.base_ref != candidate.base_ref.removeprefix("refs/heads/")
            or candidate.base_sha != worktree.base_sha
            or (
                pull.base_sha != candidate.base_sha
                and (record.base_update_intent_id is None or record.base_adoption_intent_id is None)
            )
            or pull.state != "open"
            or pull.merged
            or pull.merge_sha is not None
            or len(approval_digest) != 64
            or any(c not in "0123456789abcdef" for c in approval_digest)
        ):
            raise ReleaseReconciliationRequired()
        super().__init__(
            Publication(
                run_id=record.run_id,
                approval_id=approval_id,
                policy_version=policy.version,
                branch=pull.head_ref,
                evidence=candidate,
            ),
            github,
            push,
            worktree,
            policy,
        )
        self._record = record
        self._base_sha = pull.base_sha
        payload = dict(self.request.request_payload) | {
            "base_sha": pull.base_sha,
            "approval_digest": approval_digest,
            "candidate_evidence_digest": evidence_digest(candidate),
            "previous_head_sha": pull.head_sha,
            "pull_request_id": str(record.id),
            "node_id": pull.node_id,
            "pull_request_number": pull.number,
        }
        if record.base_update_intent_id is not None:
            payload |= {
                "base_update_intent_id": str(record.base_update_intent_id),
                "base_adoption_intent_id": str(record.base_adoption_intent_id),
            }
        digest = canonical_digest(payload)
        self.request = OperationRequest(
            run_id=record.run_id,
            kind="push_branch",
            idempotency_key=f"{record.run_id}:push-reviewed:{digest}",
            request_digest=digest,
            request_payload=payload,
        )

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        _validate_intent(intent, self.request)
        pull = self._record.pull_request
        if (
            await self._github.get_pull_request(pull.base_repository, pull.number) != pull
            or await self._github.get_branch_sha(pull.head_repository, pull.head_ref)
            != pull.head_sha
        ):
            raise ReleaseReconciliationRequired()
        return await super().invoke(intent)

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        await super().reconcile(intent)
        expected = replace(
            self._record.pull_request, head_sha=self._publication.evidence.candidate_commit
        )
        pull = await self._github.get_pull_request(expected.base_repository, expected.number)
        if pull != expected:
            raise ReleaseReconciliationRequired()
        return OperationOutcome(remote_resource_id=pull.node_id, payload=asdict(pull))


class PullRequestOperation:
    def __init__(self, publication: Publication, github: GitHubWritePort, body: bytes) -> None:
        if sha256(body).hexdigest() != publication.evidence.body_digest:
            raise ReleaseReconciliationRequired()
        try:
            self._body = body.decode("utf-8", "strict")
        except UnicodeError:
            raise ReleaseReconciliationRequired() from None
        self._publication = publication
        self._github = github
        self.request = publication.request("create_pr")

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        _validate_intent(intent, self.request)
        publication = self._publication
        evidence = publication.evidence
        if (
            await self._github.get_branch_sha(evidence.repository, publication.branch)
            != evidence.candidate_commit
            or await self._github.get_branch_sha(evidence.repository, publication.base_branch)
            != evidence.base_sha
        ):
            raise ReleaseReconciliationRequired()
        matches = await self._find()
        if matches:
            return self._adopt(matches)
        created = await self._github.create_pull_request(
            evidence.repository,
            evidence.repository,
            publication.branch,
            publication.base_branch,
            evidence.title,
            self._body,
        )
        return self._adopt((created,))

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        _validate_intent(intent, self.request)
        return self._adopt(await self._find())

    async def _find(self) -> tuple[GitHubPullRequest, ...]:
        evidence = self._publication.evidence
        return await self._github.find_pull_requests(
            evidence.repository,
            evidence.repository,
            self._publication.branch,
            self._publication.base_branch,
        )

    def _adopt(self, matches: tuple[GitHubPullRequest, ...]) -> OperationOutcome:
        if len(matches) != 1:
            raise ReleaseReconciliationRequired()
        pull = matches[0]
        evidence = self._publication.evidence
        if (
            pull.head_repository != evidence.repository
            or pull.base_repository != evidence.repository
            or pull.head_ref != self._publication.branch
            or pull.head_sha != evidence.candidate_commit
            or pull.base_ref != self._publication.base_branch
            or pull.base_sha != evidence.base_sha
            or pull.state != "open"
            or pull.merged
            or pull.merge_sha is not None
            or type(pull.number) is not int
            or pull.number < 1
            or not pull.node_id
            or pull.url != f"https://github.com/{evidence.repository}/pull/{pull.number}"
        ):
            raise ReleaseReconciliationRequired()
        return OperationOutcome(remote_resource_id=pull.node_id, payload=asdict(pull))
