"""Read-only validation of the frozen PR approval evidence boundary."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import cast
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.evidence import EvidenceKind
from forge.application.ports.github import GitHubPort
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.application.services.approved_plan import (
    ApprovedPlan,
    ApprovedPlanError,
    ApprovedPlanLoader,
)
from forge.application.services.base_review import base_review
from forge.application.services.paused_approvals import approval_gate_origin
from forge.application.services.release_resume import resumed_release_origin
from forge.application.services.remote_remediation import reviewed_push_payload
from forge.application.services.review_decision import ReviewDecisionService
from forge.domain.approval import ApprovalGate, PrApprovalEvidence, canonical_digest
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.command import CommandEnvelope
from forge.domain.evidence import (
    EvidenceStatus,
    ReviewEvidenceManifest,
    ValidationEvidenceManifest,
    decode_evidence_manifest,
)
from forge.domain.operation import OperationStatus
from forge.domain.operation import canonical_digest as operation_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunState
from forge.domain.validation import command_spec_digest
from forge.persistence.models import Approval
from forge.persistence.repositories.commands import CommandNotFound
from forge.release.github_client import GitHubClientError


class PrEvidenceValidationError(RuntimeError):
    """A stable, redacted reason why frozen publication inputs are invalid."""

    def __init__(self, category: str = "authority_drift") -> None:
        self.category = category
        super().__init__(f"PR evidence validation failed: {category}")


@dataclass(frozen=True, slots=True)
class FrozenPrEvidence:
    """Historical inputs only; no current worktree or remote preflight authority."""

    approved: ApprovedPlan
    evidence: PrApprovalEvidence
    body: bytes


@dataclass(frozen=True, slots=True)
class ValidatedPrEvidence:
    approved: ApprovedPlan
    evidence: PrApprovalEvidence
    body: bytes
    worktree: ManagedWorktree
    candidate_head: str
    candidate_diff_digest: str
    remote_base_sha: str


class PrEvidenceValidator:
    def __init__(
        self,
        artifact_store: ArtifactStore,
        approved_plans: ApprovedPlanLoader,
        git_factory: Callable[[ProjectPolicy], ControlledGitPort],
        github: GitHubPort,
    ) -> None:
        self._store = artifact_store
        self._approved = approved_plans
        self._git_factory = git_factory
        self._github = github

    async def validate(self, work: UnitOfWork, run_id: UUID) -> ValidatedPrEvidence:
        try:
            approved = await self._approved.load(work, run_id)
        except ApprovedPlanError:
            raise PrEvidenceValidationError() from None
        return await self._validate(work, approved)

    async def validate_for_publication(
        self, work: UnitOfWork, run_id: UUID, approval_id: UUID
    ) -> ValidatedPrEvidence:
        """Recheck frozen evidence after the gate was consumed, without new authority."""
        return await self._validate_consumed(work, run_id, approval_id, published=False)

    async def for_recovery(
        self, work: UnitOfWork, run_id: UUID, approval_id: UUID
    ) -> FrozenPrEvidence:
        """Prove admitted publication inputs without authorizing another effect."""
        try:
            approved = await self._approved.load(work, run_id)
            approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
            if (
                not isinstance(approval, Approval)
                or approval.run_id != run_id
                or approval.gate != "pr"
                or approval.policy_version != approved.run.policy_version
                or approved.run.version < approval.run_version + 1
            ):
                raise PrEvidenceValidationError()
            events = await work.events.list_for_version(run_id, approval.run_version + 1)
            accepted = [e for e in events if e.event_type == "run.pr_approved"]
            consumed = [e for e in events if e.event_type == "run.pr_approval_consumed"]
            if (
                len(accepted) != 1
                or accepted[0].actor_class != "operator"
                or accepted[0].actor_id != approval.authenticated_actor_id
                or accepted[0].payload.get("approval_id") != str(approval_id)
                or len(consumed) != 1
            ):
                raise PrEvidenceValidationError()
            receipt = consumed[0]
            queued = receipt.payload.get("queued")
            if not isinstance(queued, Mapping):
                raise PrEvidenceValidationError()
            source = await work.commands.get(UUID(str(queued.get("id"))))
            if (
                source.run_id != run_id
                or source.command_type != "publish_pr"
                or source.payload_schema_version != 1
                or source.payload != {"approval_id": str(approval_id)}
                or source.actor_id != approval.authenticated_actor_id
                or source.expected_run_version != approval.run_version + 1
                or receipt.actor_class != "worker"
                or receipt.actor_id != source.actor_id
                or receipt.payload.get("approval_id") != str(approval_id)
                or receipt.payload.get("approval_digest") != approval.evidence_digest
                or receipt.payload.get("target") != RunState.PUBLISHING_PR.value
                or receipt.payload.get("invalidated") is not False
                or queued != {
                    "id": str(source.id),
                    "key": source.idempotency_key,
                    "command_type": source.command_type,
                    "payload": dict(source.payload),
                    "actor_id": str(source.actor_id),
                    "version": source.expected_run_version,
                }
            ):
                raise PrEvidenceValidationError()
            historical = replace(
                approved,
                run=replace(
                    approved.run,
                    state=RunState.AWAITING_PR_APPROVAL,
                    version=approval.run_version,
                    pending_gate=ApprovalGate.PR,
                    pending_evidence_digest=approval.evidence_digest,
                ),
            )
            evidence, body, review_id, validation_id = await self._frozen(
                work, historical, RunState.AWAITING_PR_APPROVAL
            )
            rebuilt = await ReviewDecisionService(
                self._store, git_factory=self._git_factory, approved_plans=self._approved
            ).verify_frozen_publication(
                work,
                historical,
                review_id=review_id,
                validation_id=validation_id,
                head_sha=evidence.candidate_commit,
                diff_digest=evidence.diff_digest,
            )
            if rebuilt != approval.evidence_digest:
                raise PrEvidenceValidationError()
            return FrozenPrEvidence(approved, evidence, body)
        except PrEvidenceValidationError:
            raise
        except Exception:  # noqa: BLE001 - persisted authority is untrusted
            raise PrEvidenceValidationError() from None

    async def validate_published(
        self, work: UnitOfWork, run_id: UUID, approval_id: UUID
    ) -> ValidatedPrEvidence:
        """Recheck the published candidate against its original human authority."""
        return await self._validate_consumed(work, run_id, approval_id, published=True)

    async def for_reviewed_recovery(
        self, work: UnitOfWork, run_id: UUID, approval_id: UUID, digest: str
    ) -> tuple[FrozenPrEvidence, str]:
        """Prove a newer reviewed candidate without granting another push."""
        original = await self.for_recovery(work, run_id, approval_id)
        approved = original.approved
        events = [
            e for e in await work.events.list_after(run_id, 0)
            if e.event_type == "run.review_decided"
            and e.payload.get("pr_evidence_digest") == digest
            and e.payload.get("target") == RunState.MONITORING_PR.value
        ]
        if (
            len(events) != 1 or events[0].run_version > approved.run.version
            or events[0].actor_class != "worker" or events[0].actor_id is not None
        ):
            raise PrEvidenceValidationError()
        event = events[0]
        try:
            command = await work.commands.get(UUID(str(event.payload.get("queued_command_id"))))
        except ValueError, CommandNotFound:
            raise PrEvidenceValidationError() from None
        historical = replace(approved, run=replace(approved.run, version=event.run_version))
        expected = await reviewed_push_payload(
            work, historical, self._store, digest, allow_invalidated_approval=True
        )
        if (
            expected is None or expected.get("approval_id") != str(approval_id)
            or command.run_id != run_id or command.command_type != "push_reviewed_pr"
            or command.payload_schema_version != 1
            or command.expected_run_version != event.run_version
            or command.actor_id != approved.approval_actor_id
            or command.payload != expected or event.payload.get("queued_payload") != expected
            or command.idempotency_key != f"{run_id}:push-reviewed:{event.run_version}"
            or event.payload.get("queued_key") != command.idempotency_key
        ):
            raise PrEvidenceValidationError()
        frozen = replace(historical, run=replace(
            historical.run, state=RunState.AWAITING_PR_APPROVAL,
            pending_gate=ApprovalGate.PR, pending_evidence_digest=digest,
        ))
        evidence, body, review_id, validation_id = await self._frozen(
            work, frozen, RunState.MONITORING_PR
        )
        rebuilt = await ReviewDecisionService(
            self._store, git_factory=self._git_factory, approved_plans=self._approved
        ).verify_frozen_publication(
            work, frozen, review_id=review_id, validation_id=validation_id,
            head_sha=evidence.candidate_commit, diff_digest=evidence.diff_digest,
        )
        if rebuilt != digest:
            raise PrEvidenceValidationError()
        return FrozenPrEvidence(approved, evidence, body), canonical_digest(original.evidence)

    async def validate_for_base_update(
        self, work: UnitOfWork, run_id: UUID, approval_id: UUID, target_base: str
    ) -> ValidatedPrEvidence:
        """Validate the unchanged published candidate against an exact newer base.

        This read-only path does not authorize publication or merge and never
        rewrites the original frozen evidence or approved run baseline.
        """
        run = await work.runs.get_for_update(run_id)
        if (
            run.state not in {RunState.MONITORING_PR, RunState.REMEDIATING}
            or not isinstance(target_base, str)
            or len(target_base) != 40
            or any(c not in "0123456789abcdef" for c in target_base)
            or target_base == run.base_sha
        ):
            raise PrEvidenceValidationError()
        return await self._validate_consumed(
            work, run_id, approval_id, published=True, expected_remote_base=target_base
        )

    async def validate_reviewed_push(
        self, work: UnitOfWork, command: CommandEnvelope
    ) -> ValidatedPrEvidence:
        approved = await self._approved.load(work, command.run_id)
        origin = await resumed_release_origin(work, command)
        digest = str(origin.payload.get("candidate_evidence_digest"))
        expected = await reviewed_push_payload(work, approved, self._store, digest)
        events = [
            event
            for event in await work.events.list_for_version(
                command.run_id, origin.expected_run_version
            )
            if event.event_type == "run.review_decided"
            and event.payload.get("queued_command_id") == str(origin.id)
            and event.payload.get("queued_payload") == origin.payload
        ]
        if (
            origin.command_type != "push_reviewed_pr"
            or origin.payload_schema_version != 1
            or approved.run.state is not RunState.MONITORING_PR
            or approved.run.version != command.expected_run_version
            or command.actor_id != approved.approval_actor_id
            or expected != origin.payload
            or len(events) != 1
            or origin.idempotency_key
            != f"{command.run_id}:push-reviewed:{origin.expected_run_version}"
        ):
            raise PrEvidenceValidationError()
        record = await work.releases.get_for_run(command.run_id)
        if record is None:
            raise PrEvidenceValidationError()
        return await self._reviewed_candidate(
            work, approved, digest, expected_remote_base=record.pull_request.base_sha
        )

    async def _reviewed_candidate(
        self,
        work: UnitOfWork,
        approved: ApprovedPlan,
        digest: str,
        *,
        expected_remote_base: str | None = None,
    ) -> ValidatedPrEvidence:
        events = [
            event
            for event in await work.events.list_after(approved.run.id, 0)
            if event.event_type == "run.review_decided"
            and event.payload.get("pr_evidence_digest") == digest
            and event.payload.get("target") == RunState.MONITORING_PR.value
        ]
        if len(events) != 1 or events[0].run_version > approved.run.version:
            raise PrEvidenceValidationError()
        frozen = replace(
            approved,
            run=replace(
                approved.run,
                state=RunState.AWAITING_PR_APPROVAL,
                version=events[0].run_version,
                pending_gate=ApprovalGate.PR,
                pending_evidence_digest=digest,
            ),
        )
        verified = await self._validate(
            work,
            frozen,
            freeze_target=RunState.MONITORING_PR,
            expected_remote_base=expected_remote_base,
        )
        return replace(verified, approved=approved)

    async def _validate_consumed(
        self,
        work: UnitOfWork,
        run_id: UUID,
        approval_id: UUID,
        *,
        published: bool,
        expected_remote_base: str | None = None,
    ) -> ValidatedPrEvidence:
        try:
            approved = await self._approved.load(work, run_id)
        except ApprovedPlanError:
            raise PrEvidenceValidationError() from None
        run = approved.run
        approval = cast(
            Approval | None, await work.auth.get_approval(approval_id=approval_id, for_update=True)
        )
        if (
            approval is None
            or approval.run_id != run_id
            or approval.gate != "pr"
            or approval.invalidated_at is not None
            or approval.policy_version != run.policy_version
        ):
            raise PrEvidenceValidationError()
        record = None
        if published:
            record = await work.releases.get_for_run(run_id)
            allowed_states = {
                RunState.MONITORING_PR,
                RunState.AWAITING_MERGE_APPROVAL,
                RunState.MERGING,
            }
            if expected_remote_base is not None:
                allowed_states.add(RunState.REMEDIATING)
            if record is None or run.state not in allowed_states:
                raise PrEvidenceValidationError()
            publication = await work.operations.get(record.publication_intent_id)
            if (
                publication.request_payload.get("approval_id") != str(approval_id)
                or publication.request_payload.get("approval_digest") != approval.evidence_digest
                or publication.kind != "create_pr"
                or publication.run_id != run_id
                or publication.status is not OperationStatus.SUCCEEDED
                or publication.outcome
                != asdict(
                    replace(
                        record.pull_request,
                        head_sha=str(publication.request_payload.get("head_sha")),
                        base_sha=str(publication.request_payload.get("base_sha")),
                    )
                )
                or publication.remote_resource_id != record.pull_request.node_id
            ):
                raise PrEvidenceValidationError()
            published_events = [
                event
                for event in await work.events.list_after(run_id, 0)
                if event.event_type == "run.pr_published"
                and event.payload.get("approval_id") == str(approval_id)
                and event.payload.get("publication_intent_id") == str(publication.id)
                and event.payload.get("pull_request_id") == str(record.id)
            ]
            if len(published_events) != 1 or run.version < approval.run_version + 2:
                raise PrEvidenceValidationError()
            await _publication_delivery_authority(
                work,
                run_id,
                published_events[0].payload.get("source_command_id"),
                published_events[0].run_version - 1,
                approval,
            )
            if published_events[0].run_version > run.version:
                raise PrEvidenceValidationError()

        else:
            if run.state is not RunState.PUBLISHING_PR:
                raise PrEvidenceValidationError()
            if run.version != approval.run_version + 1:
                resumed = [
                    e
                    for e in await work.events.list_for_version(run_id, run.version)
                    if e.event_type == "run.resumed"
                ]
                binding = resumed[0].payload.get("continuation") if len(resumed) == 1 else None
                if not isinstance(binding, Mapping):
                    raise PrEvidenceValidationError()
                await _publication_delivery_authority(
                    work,
                    run_id,
                    binding.get("command_id"),
                    run.version,
                    approval,
                )

        events = [
            event
            for event in await work.events.list_for_version(run_id, approval.run_version + 1)
            if event.event_type == "run.pr_approved"
        ]
        if (
            len(events) != 1
            or events[0].payload.get("approval_id") != str(approval_id)
            or events[0].actor_class != "operator"
            or events[0].actor_id != approval.authenticated_actor_id
        ):
            raise PrEvidenceValidationError()
        if record is not None and record.candidate_evidence_digest is not None:
            if record.reviewed_push_intent_id is None:
                raise PrEvidenceValidationError()
            pushed = await work.operations.get(record.reviewed_push_intent_id)
            settled = [
                event
                for event in await work.events.list_after(run_id, 0)
                if event.event_type == "run.pr_updated"
                and event.payload.get("push_intent_id") == str(pushed.id)
                and event.payload.get("candidate_evidence_digest")
                == record.candidate_evidence_digest
            ]
            if (
                pushed.status is not OperationStatus.SUCCEEDED
                or pushed.run_id != run_id
                or pushed.kind != "push_branch"
                or pushed.request_digest != operation_digest(pushed.request_payload)
                or pushed.remote_resource_id != record.pull_request.node_id
                or pushed.request_payload.get("head_sha") != record.pull_request.head_sha
                or pushed.request_payload.get("pull_request_id") != str(record.id)
                or pushed.outcome != asdict(record.pull_request)
                or pushed.request_payload.get("approval_id") != str(approval_id)
                or pushed.request_payload.get("approval_digest") != approval.evidence_digest
                or pushed.request_payload.get("candidate_evidence_digest")
                != record.candidate_evidence_digest
                or pushed.request_payload.get("base_update_intent_id")
                != (str(record.base_update_intent_id) if record.base_update_intent_id else None)
                or pushed.request_payload.get("base_adoption_intent_id")
                != (str(record.base_adoption_intent_id) if record.base_adoption_intent_id else None)
                or len(settled) != 1
            ):
                raise PrEvidenceValidationError()
            event = settled[0]
            if (
                event.actor_class != "worker"
                or event.actor_id != approved.approval_actor_id
                or event.payload.get("pull_request_id") != str(record.id)
                or event.run_version > run.version
            ):
                raise PrEvidenceValidationError()
            return await self._reviewed_candidate(
                work,
                approved,
                record.candidate_evidence_digest,
                expected_remote_base=expected_remote_base or record.pull_request.base_sha,
            )
        if record is not None and record.base_update_intent_id is not None:
            try:
                adopted = await base_review(work, approved)
            except CommandRecoveryRequired:
                raise PrEvidenceValidationError() from None
            if adopted is None:
                raise PrEvidenceValidationError()
            reviews = [
                e
                for e in await work.events.list_after(run.id, 0)
                if e.event_type == "run.review_decided"
                and e.payload.get("source_command_id") == str(adopted.review_command_id)
                and e.payload.get("target") == RunState.MONITORING_PR.value
            ]
            if (
                len(reviews) != 1
                or reviews[0].run_version != adopted.review_version + 1
                or reviews[0].run_version > run.version
                or reviews[0].actor_class != "worker"
                or reviews[0].actor_id is not None
                or reviews[0].payload.get("validation_evidence_set_id")
                != str(adopted.validation_id)
                or any(reviews[0].payload.get(k) != v for k, v in adopted.binding.items())
            ):
                raise PrEvidenceValidationError()
            return await self._reviewed_candidate(
                work,
                approved,
                str(reviews[0].payload.get("pr_evidence_digest")),
                expected_remote_base=expected_remote_base or record.pull_request.base_sha,
            )
        # This historical view selects the original freeze event and digest;
        # it is never written back or returned as the current run state.
        frozen = replace(
            approved,
            run=replace(
                run,
                state=RunState.AWAITING_PR_APPROVAL,
                version=approval.run_version,
                pending_gate=ApprovalGate.PR,
                pending_evidence_digest=approval.evidence_digest,
            ),
        )
        verified = await self._validate(work, frozen, expected_remote_base=expected_remote_base)
        return replace(verified, approved=approved)

    async def _validate(
        self,
        work: UnitOfWork,
        approved: ApprovedPlan,
        *,
        freeze_target: RunState = RunState.AWAITING_PR_APPROVAL,
        expected_remote_base: str | None = None,
    ) -> ValidatedPrEvidence:
        run = approved.run
        try:
            evidence, body, review_id, validation_id = await self._frozen(
                work, approved, freeze_target
            )
            worktree, head, diff_digest = self._candidate(approved)
            if head != evidence.candidate_commit or diff_digest != evidence.diff_digest:
                raise PrEvidenceValidationError("content_drift")
            rebuilt = await ReviewDecisionService(
                self._store, git_factory=self._git_factory, approved_plans=self._approved
            ).verify_frozen_publication(
                work,
                approved,
                review_id=review_id,
                validation_id=validation_id,
                head_sha=head,
                diff_digest=diff_digest,
            )
            if rebuilt != run.pending_evidence_digest:
                raise PrEvidenceValidationError()
            remote_base = await self._github.get_base(
                evidence.repository, _branch(evidence.base_ref)
            )
            if remote_base != (expected_remote_base or evidence.base_sha):
                raise PrEvidenceValidationError("remote_base_drift")
            return ValidatedPrEvidence(
                approved, evidence, body, worktree, head, diff_digest, remote_base
            )
        except PrEvidenceValidationError:
            raise
        except GitHubClientError as error:
            raise PrEvidenceValidationError("remote_read_failed") from error
        except Exception:  # noqa: BLE001 - persisted evidence and remote reads are untrusted
            raise PrEvidenceValidationError() from None

    async def _frozen(
        self, work: UnitOfWork, approved: ApprovedPlan, freeze_target: RunState
    ) -> tuple[PrApprovalEvidence, bytes, UUID, UUID]:
        """Shared immutable evidence checks, independent of live preflight."""
        run = approved.run
        if (
            run.state is not RunState.AWAITING_PR_APPROVAL
            or run.pending_gate is not ApprovalGate.PR
            or not run.pending_evidence_digest
            or run.base_ref is None
            or run.base_sha is None
        ):
            raise PrEvidenceValidationError()
        evidence_descriptor, wire = await self._artifact(
            work, run.id, run.pending_evidence_digest
        )
        evidence = PrApprovalEvidence.model_validate_json(wire)
        if (
            canonical_digest(evidence) != run.pending_evidence_digest
            or evidence_descriptor.producer_type != "pr_approval_evidence"
            or evidence_descriptor.media_type != "application/json"
            or evidence_descriptor.truncated
            or evidence.repository != approved.policy.github_repository
            or evidence.base_ref != run.base_ref
            or evidence.base_sha != run.base_sha
            or evidence.title != approved.task.title
            or evidence.runner_mode is not approved.policy.runner_mode
            or evidence.remote_remediation_limit != approved.policy.remote_remediation_limit
        ):
            raise PrEvidenceValidationError()
        review_id, validation_id = await self._freeze_ids(
            work, approved, evidence_descriptor, freeze_target
        )
        validation, _review, validation_digest, review_digest = await self._manifests(
            work, approved, validation_id, review_id
        )
        if (
            validation_digest != evidence.validation_digest
            or review_digest != evidence.review_digest
        ):
            raise PrEvidenceValidationError()
        body_descriptor, body = await self._artifact(work, run.id, evidence.body_digest)
        if (
            body_descriptor.producer_type != "pr_approval_body"
            or body_descriptor.producer_id != review_id
            or body_descriptor.media_type != "text/markdown"
            or body_descriptor.truncated
            or body_descriptor.parent_digests
            != tuple(sorted({evidence.validation_digest, evidence.review_digest}))
        ):
            raise PrEvidenceValidationError()
        if (
            evidence_descriptor.producer_id != review_id
            or evidence_descriptor.parent_digests
            != tuple(
                sorted(
                    {
                        evidence.body_digest,
                        evidence.runner_evidence_digest,
                        evidence.validation_digest,
                        evidence.review_digest,
                    }
                )
            )
        ):
            raise PrEvidenceValidationError()
        if validation.head_sha != evidence.candidate_commit:
            raise PrEvidenceValidationError()
        return evidence, body, review_id, validation_id

    async def _freeze_ids(
        self,
        work: UnitOfWork,
        approved: ApprovedPlan,
        descriptor: ArtifactDescriptor,
        target: RunState = RunState.AWAITING_PR_APPROVAL,
    ) -> tuple[UUID, UUID]:
        gate_version = await approval_gate_origin(
            work, approved.run.id, approved.run.version, "pr", approved.run.pending_evidence_digest
        )
        events = [
            e
            for e in await work.events.list_for_version(approved.run.id, gate_version)
            if e.event_type == "run.review_decided"
        ]
        if len(events) != 1:
            raise PrEvidenceValidationError()
        event = events[0]
        try:
            review_id = UUID(str(event.payload["review_evidence_set_id"]))
            validation_id = UUID(str(event.payload["validation_evidence_set_id"]))
        except KeyError, ValueError:
            raise PrEvidenceValidationError() from None
        if (
            event.actor_class != "worker"
            or event.actor_id is not None
            or event.payload.get("target") != target.value
            or event.payload.get("approval_id") != str(approved.approval_id)
            or event.payload.get("pr_evidence_digest") != approved.run.pending_evidence_digest
            or descriptor.producer_id != review_id
        ):
            raise PrEvidenceValidationError()
        return review_id, validation_id

    async def _manifests(
        self, work: UnitOfWork, approved: ApprovedPlan, validation_id: UUID, review_id: UUID
    ) -> tuple[ValidationEvidenceManifest, ReviewEvidenceManifest, str, str]:
        validation = await work.evidence.get_by_id(validation_id, run_id=approved.run.id)
        review = await work.evidence.get_by_id(review_id, run_id=approved.run.id)
        _vd, validation_wire = await self._artifact(
            work, approved.run.id, validation.manifest_digest
        )
        _rd, review_wire = await self._artifact(work, approved.run.id, review.manifest_digest)
        vm, rm = decode_evidence_manifest(validation_wire), decode_evidence_manifest(review_wire)
        if (
            not isinstance(vm, ValidationEvidenceManifest)
            or not isinstance(rm, ReviewEvidenceManifest)
            or validation.kind is not EvidenceKind.VALIDATION
            or review.kind is not EvidenceKind.REVIEW
            or validation.manifest_digest != hashlib.sha256(validation_wire).hexdigest()
            or review.manifest_digest != hashlib.sha256(review_wire).hexdigest()
            or validation.manifest_byte_count != len(validation_wire)
            or review.manifest_byte_count != len(review_wire)
            or validation.policy_version != approved.policy.version
            or review.policy_version != approved.policy.version
            or validation.head_sha != vm.head_sha
            or review.head_sha != rm.head_sha
            or rm.head_sha != vm.head_sha
            or rm.producer_execution_id != review.producer_execution_id
            or vm.step_id != validation.step_id
            or rm.step_id != review.step_id
            or bool(rm.review.missing_evidence)
            or approved.policy.blocks_publication(rm.review.findings)
            or review.validation_evidence_set_id != validation_id
            or rm.validation_evidence_set_id != validation_id
            or vm.evidence_set_id != validation_id
            or rm.evidence_set_id != review_id
            or vm.run_id != approved.run.id
            or rm.run_id != approved.run.id
            or vm.policy_version != approved.policy.version
            or rm.policy_version != approved.policy.version
            or len(vm.members) != len(approved.policy.required_checks)
            or {m.command_name: m.command_digest for m in vm.members}
            != {s.name: command_spec_digest(s) for s in approved.policy.required_checks}
            or any(m.status is not EvidenceStatus.PASSED or m.exit_code != 0 for m in vm.members)
        ):
            raise PrEvidenceValidationError()
        return vm, rm, validation.manifest_digest, review.manifest_digest

    def _candidate(self, approved: ApprovedPlan) -> tuple[ManagedWorktree, str, str]:
        run = approved.run
        if not run.worktree_path or not run.branch_name or not run.base_sha:
            raise PrEvidenceValidationError("content_drift")
        worktree = ManagedWorktree(
            identity=WorktreeIdentity.for_run(
                run.project_id, run.id, run.branch_name, approved.policy.database.enabled
            ),
            path=Path(run.worktree_path),
            base_sha=run.base_sha,
        )
        git = self._git_factory(approved.policy)
        candidate = git.candidate_diff(worktree)
        if (
            git.inspect_worktree(worktree.identity, worktree.base_sha) != worktree
            or not git.is_ancestor(worktree)
            or candidate.diff.truncated
        ):
            raise PrEvidenceValidationError("content_drift")
        return (
            worktree,
            candidate.head_sha,
            hashlib.sha256(candidate.diff.text.encode("utf-8")).hexdigest(),
        )

    async def _artifact(
        self, work: UnitOfWork, run_id: UUID, digest: str
    ) -> tuple[ArtifactDescriptor, bytes]:
        descriptor = await work.artifacts.get_by_digest(digest, run_id=run_id)
        data = await self._store.open_bytes(digest)
        if (
            descriptor.byte_count != len(data)
            or descriptor.truncated
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise PrEvidenceValidationError()
        return descriptor, data


async def _publication_delivery_authority(
    work: UnitOfWork, run_id: UUID, source_id: object, version: int, approval: Approval
) -> None:
    try:
        source = await work.commands.get(UUID(str(source_id)))
        origin = await resumed_release_origin(work, source)
        if (
            source.run_id != run_id
            or source.command_type != "publish_pr"
            or source.expected_run_version != version
            or origin.expected_run_version != approval.run_version + 1
            or origin.payload.get("approval_id") != str(approval.id)
            or origin.actor_id != approval.authenticated_actor_id
        ):
            raise PrEvidenceValidationError()
    except ValueError, CommandNotFound, CommandRecoveryRequired:
        raise PrEvidenceValidationError() from None


def _branch(base_ref: str) -> str:
    prefix = "refs/heads/"
    if not base_ref.startswith(prefix) or not base_ref[len(prefix) :]:
        raise PrEvidenceValidationError()
    return base_ref[len(prefix) :]


__all__ = ["PrEvidenceValidationError", "PrEvidenceValidator", "ValidatedPrEvidence"]
