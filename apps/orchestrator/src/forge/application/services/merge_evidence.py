"""Recompute the complete local/remote evidence at a pending merge gate."""

import hashlib
import json
from collections.abc import Mapping
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.paused_approvals import approval_gate_origin
from forge.application.services.pr_evidence import PrEvidenceValidationError, PrEvidenceValidator
from forge.application.services.release_resume import resumed_release_origin
from forge.artifacts._errors import ArtifactIntegrityError, ArtifactStoreError
from forge.domain.approval import ApprovalGate, MergeApprovalEvidence, canonical_digest
from forge.domain.operation import canonical_digest as payload_digest
from forge.domain.run import RunState
from forge.persistence.models import Approval
from forge.persistence.repositories.artifacts import ArtifactNotFound
from forge.persistence.repositories.commands import CommandNotFound
from forge.release.merge import MergeController, StaleMergeEvidence


class MergeEvidenceValidator:
    def __init__(
        self, store: ArtifactStore, publication: PrEvidenceValidator, controller: MergeController
    ) -> None:
        self._store, self._publication, self._controller = store, publication, controller

    async def queue_required(
        self, work: UnitOfWork, run_id: UUID, approval_id: UUID, approved: MergeApprovalEvidence
    ) -> bool:
        """Choose the effect from the frozen observation, never from mutable remote state."""
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        record = await work.releases.get_for_run(run_id)
        if (
            not isinstance(approval, Approval) or record is None
            or approval.run_id != run_id or approval.gate != "merge"
            or approval.evidence_digest != canonical_digest(approved)
        ):
            raise StaleMergeEvidence()
        try:
            version = await approval_gate_origin(
                work, run_id, approval.run_version, "merge", approval.evidence_digest
            )
            events = [
                event for event in await work.events.list_for_version(run_id, version)
                if event.event_type == "run.merge_ready" and event.actor_class == "worker"
                and event.payload.get("merge_evidence_digest") == approval.evidence_digest
                and event.payload.get("pull_request_id") == str(record.id)
            ]
            if len(events) != 1:
                raise StaleMergeEvidence()
            digest = str(events[0].payload.get("observation_digest"))
            descriptor = await work.artifacts.get_by_digest(digest, run_id=run_id)
            wire = await self._store.open_bytes(digest, max_bytes=1_000_000)
            if (
                hashlib.sha256(wire).hexdigest() != digest or descriptor.digest != digest
                or descriptor.producer_type != "remote_pr_observation"
                or descriptor.producer_id != record.id or descriptor.truncated
                or descriptor.media_type != "application/json" or descriptor.byte_count != len(wire)
            ):
                raise StaleMergeEvidence()
            raw = json.loads(wire)
            protection = raw.get("protection") if isinstance(raw, dict) else None
            if (
                not isinstance(protection, dict)
                or payload_digest(protection) != approved.protection_digest
                or type(protection.get("merge_queue_enabled")) is not bool
                or protection.get("verified") is not True
                or protection.get("actor_can_bypass") is not False
            ):
                raise StaleMergeEvidence()
            queue = protection["merge_queue_enabled"]
            if (
                (queue and protection.get("merge_queue_method") != approved.merge_method)
                or (not queue and protection.get("strict_required_checks") is not True)
            ):
                raise StaleMergeEvidence()
            return bool(queue)
        except (
            ValueError, OSError, ArtifactNotFound, ArtifactIntegrityError,
            ArtifactStoreError, CommandRecoveryRequired,
        ):
            raise StaleMergeEvidence() from None

    async def validate(self, work: UnitOfWork, run_id: UUID) -> MergeApprovalEvidence:
        run = await work.runs.get_for_update(run_id)
        if (
            run.state is not RunState.AWAITING_MERGE_APPROVAL
            or run.pending_gate is not ApprovalGate.MERGE
            or run.pending_evidence_digest is None
        ):
            raise StaleMergeEvidence()
        return await self._validate_gate(
            work, run_id, run.pending_evidence_digest, run.version, recheck=True
        )

    async def for_recovery(
        self, work: UnitOfWork, run_id: UUID, approval_id: UUID
    ) -> MergeApprovalEvidence:
        """Load historical admitted authority without authorizing another merge."""
        from forge.application.services.merge_authority import verify_merge_delivery

        run = await work.runs.get_for_update(run_id)
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        if (
            not isinstance(approval, Approval)
            or approval.run_id != run_id
            or approval.gate != "merge"
            or approval.policy_version != run.policy_version
            or run.version < approval.run_version + 1
        ):
            raise StaleMergeEvidence()
        events = [
            e
            for e in await work.events.list_for_version(run_id, approval.run_version + 1)
            if e.event_type == "run.merge_approved"
            and e.payload.get("approval_id") == str(approval_id)
        ]
        if len(events) != 1:
            raise StaleMergeEvidence()
        try:
            source = await work.commands.get(UUID(str(events[0].payload.get("queued_command_id"))))
        except ValueError, CommandNotFound:
            raise StaleMergeEvidence() from None
        if (
            source.run_id != run_id
            or source.command_type != "merge_pr"
            or source.expected_run_version != approval.run_version + 1
            or source.actor_id != approval.authenticated_actor_id
            or source.payload != {"approval_id": str(approval_id)}
        ):
            raise StaleMergeEvidence()
        await verify_merge_delivery(source, work, approval)
        return await self._validate_gate(
            work, run_id, approval.evidence_digest, approval.run_version, recheck=False
        )

    async def consumed(
        self, work: UnitOfWork, run_id: UUID, approval_id: UUID, *, recheck: bool
    ) -> MergeApprovalEvidence:
        run = await work.runs.get_for_update(run_id)
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        if (
            not isinstance(approval, Approval)
            or approval.run_id != run_id
            or approval.gate != "merge"
            or approval.invalidated_at is not None
            or approval.policy_version != run.policy_version
            or run.state is not RunState.MERGING
        ):
            raise StaleMergeEvidence()
        if run.version != approval.run_version + 1:
            resumed = [
                e
                for e in await work.events.list_for_version(run_id, run.version)
                if e.event_type == "run.resumed"
            ]
            binding = resumed[0].payload.get("continuation") if len(resumed) == 1 else None
            if not isinstance(binding, Mapping):
                raise StaleMergeEvidence()
            try:
                source = await work.commands.get(UUID(str(binding.get("command_id"))))
                origin = await resumed_release_origin(work, source)
                if (
                    source.run_id != run_id
                    or source.command_type != "merge_pr"
                    or source.expected_run_version != run.version
                    or origin.expected_run_version != approval.run_version + 1
                    or origin.payload.get("approval_id") != str(approval_id)
                    or origin.actor_id != approval.authenticated_actor_id
                ):
                    raise StaleMergeEvidence()
            except ValueError, CommandNotFound, CommandRecoveryRequired:
                raise StaleMergeEvidence() from None
        events = [
            event
            for event in await work.events.list_for_version(run_id, approval.run_version + 1)
            if event.event_type == "run.merge_approved"
            and event.payload.get("approval_id") == str(approval_id)
            and event.payload.get("evidence_digest") == approval.evidence_digest
            and event.actor_class == "operator"
            and event.actor_id == approval.authenticated_actor_id
        ]
        if len(events) != 1:
            raise StaleMergeEvidence()
        return await self._validate_gate(
            work, run_id, approval.evidence_digest, approval.run_version, recheck=recheck
        )

    async def _validate_gate(
        self, work: UnitOfWork, run_id: UUID, digest: str, gate_version: int, *, recheck: bool
    ) -> MergeApprovalEvidence:
        record = await work.releases.get_for_run(run_id)
        if record is None:
            raise StaleMergeEvidence()
        try:
            descriptor = await work.artifacts.get_by_digest(digest, run_id=run_id)
            wire = await self._store.open_bytes(digest, max_bytes=1_000_000)
            frozen = MergeApprovalEvidence.model_validate_json(wire)
        except ValueError, OSError, ArtifactNotFound, ArtifactIntegrityError, ArtifactStoreError:
            raise StaleMergeEvidence() from None
        try:
            gate_version = await approval_gate_origin(work, run_id, gate_version, "merge", digest)
        except CommandRecoveryRequired:
            raise StaleMergeEvidence() from None
        events = [
            event
            for event in await work.events.list_for_version(run_id, gate_version)
            if event.event_type == "run.merge_ready"
            and event.payload.get("merge_evidence_digest") == digest
        ]
        if (
            hashlib.sha256(wire).hexdigest() != digest
            or canonical_digest(frozen) != digest
            or descriptor.producer_type != "merge_approval_evidence"
            or descriptor.producer_id != record.id
            or descriptor.truncated
            or descriptor.media_type != "application/json"
            or descriptor.byte_count != len(wire)
            or len(events) != 1
            or events[0].actor_class != "worker"
            or descriptor.parent_digests
            != tuple(
                sorted(
                    {frozen.validation_digest, frozen.review_digest, frozen.runner_evidence_digest}
                )
            )
        ):
            raise StaleMergeEvidence()
        if not recheck:
            return frozen
        publication = await work.operations.get(record.publication_intent_id)
        try:
            local = await self._publication.validate_published(
                work, run_id, UUID(str(publication.request_payload["approval_id"]))
            )
        except PrEvidenceValidationError:
            raise StaleMergeEvidence() from None
        if frozen.merge_method not in local.approved.policy.allowed_merge_methods:
            raise StaleMergeEvidence()
        current = frozen.model_copy(
            update={
                "repository": local.evidence.repository,
                "pull_request_number": record.pull_request.number,
                "head_sha": local.candidate_head,
                "base_ref": local.evidence.base_ref,
                "base_sha": local.remote_base_sha,
                "validation_digest": local.evidence.validation_digest,
                "review_digest": local.evidence.review_digest,
                "runner_mode": local.evidence.runner_mode,
                "runner_evidence_digest": local.evidence.runner_evidence_digest,
                "policy_version": local.approved.policy.version,
            }
        )
        await self._controller.preflight(record, frozen, current)
        return frozen
