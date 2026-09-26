"""Advance verified primary acceptance to the existing human publication gate."""

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.subscription_acceptance import PreparedSubscriptionAcceptance
from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.subscription_validation import AcceptanceValidationRepair
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ControlledGitPort
from forge.application.services.approved_plan import ApprovedPlan, ApprovedPlanLoader
from forge.application.services.control_settlement import pending_current_control_stop
from forge.application.services.subscription_publication_evidence import (
    PUBLICATION_EVENT,
    FrozenSubscriptionPublication,
    SubscriptionPublicationEvidence,
    VerifiedSubscriptionValidation,
    publication_decision_payload,
)
from forge.application.services.validation import (
    ValidationService,
    _fence_command,
    validation_acceptance_attempt,
)
from forge.domain.approval import (
    ApprovalGate,
    SubscriptionPrApprovalEvidence,
    decode_pr_approval_evidence,
)
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.evidence import (
    EvidenceStatus,
    ValidationEvidenceManifest,
    decode_evidence_manifest,
)
from forge.domain.operation import canonical_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunState

if TYPE_CHECKING:
    from forge.application.services.subscription_remote_remediation import (
        SubscriptionRemoteRemediationController,
    )

EVENT = PUBLICATION_EVENT
REJECTION_EVENT = "run.subscription_validation_rejected"


@dataclass(frozen=True, slots=True)
class SubscriptionPublicationDecision:
    run_id: UUID
    state: RunState
    version: int
    validation_evidence_set_id: UUID
    acceptance_evidence_set_id: UUID
    pr_evidence_digest: str


@dataclass(frozen=True, slots=True)
class SubscriptionValidationRepairDecision:
    run_id: UUID
    state: RunState
    version: int
    validation_evidence_set_id: UUID
    validation_digest: str


def _failure_payload(
    command: CommandEnvelope,
    verified: VerifiedSubscriptionValidation,
    receipt: AcceptanceValidationRepair,
    local_count: int,
) -> dict[str, object]:
    return {
        "source_command_id": str(command.id),
        "source_payload_digest": canonical_digest(command.payload),
        "source_expected_version": command.expected_run_version,
        "dispatch": verified.binding.payload(),
        "validation_evidence_set_id": str(verified.validation.evidence_set_id),
        "validation_digest": verified.validation.manifest_digest,
        "failed_checks": _failed_checks(verified),
        "repair": receipt.payload(),
        "local_remediation_count": local_count,
        "target": (
            RunState.REMEDIATING if receipt.repaired else RunState.AWAITING_HUMAN_INTERVENTION
        ).value,
    }


def _failed_checks(verified: VerifiedSubscriptionValidation) -> tuple[str, ...]:
    return tuple(
        sorted(
            m.command_name for m in verified.manifest.members if m.status is EvidenceStatus.FAILED
        )
    )


class SubscriptionPublicationController:
    def __init__(
        self,
        store: ArtifactStore,
        *,
        validation: ValidationService,
        git_factory: Callable[[ProjectPolicy], ControlledGitPort],
        remote_repairs: SubscriptionRemoteRemediationController | None = None,
    ) -> None:
        self._store, self._validation, self._git = store, validation, git_factory
        self._approved = ApprovedPlanLoader(store)
        self._publication = SubscriptionPublicationEvidence(store)
        self._remote_repairs = remote_repairs

    async def validate(
        self, command: CommandEnvelope, work: UnitOfWork
    ) -> SubscriptionPublicationDecision | SubscriptionValidationRepairDecision:
        await _fence_command(command, work)
        approved = await self._approved.load(work, command.run_id)
        if command.actor_id != approved.approval_actor_id:
            raise CommandRecoveryRequired("subscription publication actor differs")
        events = [
            event
            for event in await work.events.list_after(command.run_id, 0)
            if event.event_type == EVENT
            and event.payload.get("source_command_id") == str(command.id)
        ]
        if events:
            return await self._replay(work, command, approved, events)
        rejected = [
            event
            for event in await work.events.list_after(command.run_id, 0)
            if event.event_type == REJECTION_EVENT
            and event.payload.get("source_command_id") == str(command.id)
        ]
        if rejected:
            return await self._rejected_replay(work, command, approved, rejected)
        attempt_id = validation_acceptance_attempt(command)
        if (
            attempt_id is None
            or approved.run.state is not RunState.VALIDATING
            or approved.run.version != command.expected_run_version
        ):
            raise CommandRecoveryRequired("subscription publication is not current")
        await work.commit()
        produced = await self._validation.execute(command, work)
        await _fence_command(command, work)
        current = await self._approved.load(work, command.run_id)
        if current.run != approved.run or current.approval_id != approved.approval_id:
            raise CommandRecoveryRequired("subscription publication authority changed")
        proposal, proof = await work.subscription_decisions.acceptance_validation_source(attempt_id)
        diff = await self._candidate(proposal)
        manifest = decode_evidence_manifest(await self._store.open_bytes(produced.manifest_digest))
        if isinstance(manifest, ValidationEvidenceManifest) and any(
            member.status is not EvidenceStatus.PASSED for member in manifest.members
        ):
            verified = await self._publication.verify_validation(work, current, command)
            await _fence_command(command, work)
            latest = await self._approved.load(work, command.run_id)
            source, receipts = await work.subscription_decisions.acceptance_validation_source(
                attempt_id
            )
            if (
                verified.passed
                or not _failed_checks(verified)
                or produced != verified.validation
                or latest.run != current.run
                or latest.approval_id != current.approval_id
                or source != proposal
                or receipts != proof
                or await self._candidate(source) != diff
                or await pending_current_control_stop(work, latest.run)
            ):
                raise CommandRecoveryRequired("subscription validation failure authority differs")
            await _fence_command(command, work)
            repair = await work.subscription_decisions.reject_acceptance_validation(
                source,
                command.id,
                produced.manifest_digest,
                _failed_checks(verified),
                current.evidence.local_remediation_limit,
            )
            payload = _failure_payload(
                command,
                verified,
                repair,
                current.run.local_remediation_count + int(repair.repaired),
            )
            await _fence_command(command, work)
            if repair.repaired:
                changed = await work.runs.begin_local_remediation(
                    current.run.id,
                    current.run.version,
                    automatic=True,
                    limit=current.evidence.local_remediation_limit,
                    event_type=REJECTION_EVENT,
                    event_payload=payload,
                    actor_class="worker",
                    actor_id=command.actor_id,
                )
            else:
                changed = await work.runs.intervene(
                    current.run.id,
                    current.run.version,
                    REJECTION_EVENT,
                    payload,
                    actor_class="worker",
                    actor_id=command.actor_id,
                )
            await work.commit()
            return SubscriptionValidationRepairDecision(
                changed.id,
                changed.state,
                changed.version,
                produced.evidence_set_id,
                produced.manifest_digest,
            )
        frozen = await self._publication.freeze(work, current, command, diff_digest=diff)
        if produced != frozen.validation:
            raise CommandRecoveryRequired("subscription publication validation changed")
        await _fence_command(command, work)
        latest = await self._approved.load(work, command.run_id)
        source, receipts = await work.subscription_decisions.acceptance_validation_source(
            attempt_id
        )
        if (
            latest.run != current.run
            or latest.approval_id != current.approval_id
            or source != proposal
            or receipts != proof
            or await self._candidate(source) != diff
            or await pending_current_control_stop(work, latest.run)
        ):
            raise CommandRecoveryRequired("subscription publication changed during storage")
        await _fence_command(command, work)
        if await work.releases.get_for_run(command.run_id) is not None:
            if self._remote_repairs is None:
                raise CommandRecoveryRequired("subscription remote publication is not configured")
            payload = await self._remote_repairs.push_payload(work, current, frozen)
            push = await work.commands.enqueue(
                run_id=command.run_id,
                command_type="push_reviewed_pr",
                idempotency_key=f"{command.run_id}:push-reviewed:{current.run.version + 1}",
                payload=payload,
                expected_run_version=current.run.version + 1,
                actor_id=current.approval_actor_id,
            )
            await self._verify_push(work, current, frozen, push, current.run.version + 1)
            await _fence_command(command, work)
            run = await work.runs.transition(
                command.run_id, current.run.version, RunState.MONITORING_PR,
                EVENT, publication_decision_payload(command, current, frozen, push=push),
                actor_class="worker", actor_id=current.approval_actor_id,
            )
        else:
            run = await work.runs.await_approval(
                command.run_id,
                current.run.version,
                ApprovalGate.PR,
                frozen.digest,
                EVENT,
                publication_decision_payload(command, current, frozen),
                actor_class="worker",
                actor_id=current.approval_actor_id,
            )
        await _fence_command(command, work)
        await work.commit()
        return self._decision(command.run_id, run.version, frozen, run.state)

    async def _rejected_replay(
        self,
        work: UnitOfWork,
        command: CommandEnvelope,
        approved: ApprovedPlan,
        events: list[RunEvent],
    ) -> SubscriptionValidationRepairDecision:
        if len(events) != 1:
            raise CommandRecoveryRequired("subscription validation rejection is ambiguous")
        event = events[0]
        verified = await self._publication.verify_validation(work, approved, command)
        receipt = AcceptanceValidationRepair.from_payload(event.payload.get("repair"))
        local_count = event.payload.get("local_remediation_count")
        if (
            verified.passed
            or not _failed_checks(verified)
            or type(local_count) is not int
            or local_count < int(receipt.repaired)
            or local_count > approved.run.local_remediation_count
            or (receipt.repaired and local_count > approved.evidence.local_remediation_limit)
            or event.actor_class != "worker"
            or event.actor_id != command.actor_id
            or event.payload_schema_version != 1
            or event.run_version != command.expected_run_version + 1
            or event.run_version > approved.run.version
            or canonical_digest(event.payload)
            != canonical_digest(_failure_payload(command, verified, receipt, local_count))
        ):
            raise CommandRecoveryRequired("subscription validation rejection differs")
        await work.subscription_decisions.verify_acceptance_validation_rejection(
            verified.source,
            command.id,
            verified.validation.manifest_digest,
            _failed_checks(verified),
            receipt,
        )
        await work.commit()
        return SubscriptionValidationRepairDecision(
            command.run_id,
            RunState.REMEDIATING if receipt.repaired else RunState.AWAITING_HUMAN_INTERVENTION,
            event.run_version,
            verified.validation.evidence_set_id,
            verified.validation.manifest_digest,
        )

    async def _replay(
        self,
        work: UnitOfWork,
        command: CommandEnvelope,
        approved: ApprovedPlan,
        events: list[RunEvent],
    ) -> SubscriptionPublicationDecision:
        if len(events) != 1:
            raise CommandRecoveryRequired("subscription publication replay is ambiguous")
        event = events[0]
        digest = str(event.payload.get("pr_evidence_digest"))
        evidence = decode_pr_approval_evidence(await self._store.open_bytes(digest))
        if not isinstance(evidence, SubscriptionPrApprovalEvidence):
            raise CommandRecoveryRequired("subscription publication evidence kind differs")
        frozen = await self._publication.freeze(
            work, approved, command, diff_digest=evidence.diff_digest, read_only=True
        )
        push = None
        if event.payload.get("target") == RunState.MONITORING_PR.value:
            push = await work.commands.get(UUID(str(event.payload.get("queued_command_id"))))
            await self._verify_push(work, approved, frozen, push, event.run_version)
        if (
            frozen.digest != digest
            or event.actor_class != "worker"
            or event.actor_id != approved.approval_actor_id
            or event.payload_schema_version != 1
            or event.run_version != command.expected_run_version + 1
            or event.run_version > approved.run.version
            or canonical_digest(event.payload)
            != canonical_digest(publication_decision_payload(command, approved, frozen, push=push))
        ):
            raise CommandRecoveryRequired("subscription publication decision differs")
        await work.commit()
        return self._decision(
            command.run_id, event.run_version, frozen,
            RunState.MONITORING_PR if push is not None else RunState.AWAITING_PR_APPROVAL,
        )

    async def _verify_push(
        self, work: UnitOfWork, approved: ApprovedPlan, frozen: FrozenSubscriptionPublication,
        push: CommandEnvelope, version: int,
    ) -> None:
        if self._remote_repairs is None:
            raise CommandRecoveryRequired("subscription remote publication is not configured")
        expected = await self._remote_repairs.push_payload(work, approved, frozen)
        if (
            push.command_type != "push_reviewed_pr"
            or push.run_id != approved.run.id
            or push.payload_schema_version != 1
            or push.payload != expected
            or push.expected_run_version != version
            or push.idempotency_key != f"{approved.run.id}:push-reviewed:{version}"
            or push.actor_id != approved.approval_actor_id
            or (version > approved.run.version and push.status is not CommandStatus.PENDING)
        ):
            raise CommandRecoveryRequired("subscription reviewed push queue differs")

    async def _candidate(self, source: PreparedSubscriptionAcceptance) -> str:
        def capture() -> str:
            git = self._git(source.policy)
            candidate = git.candidate_diff(source.worktree)
            snapshot = git.working_tree_snapshot(
                source.worktree, secret_paths=source.policy.effective_secret_paths
            )
            if (
                git.inspect_worktree(source.worktree.identity, source.worktree.base_sha)
                != source.worktree
                or not git.is_ancestor(source.worktree)
                or candidate.diff.truncated
                or candidate.head_sha != source.review.candidate.head_sha
                or CandidateInspection.from_snapshot(snapshot) != source.review.candidate
            ):
                raise CommandRecoveryRequired("subscription publication candidate differs")
            return hashlib.sha256(candidate.diff.text.encode()).hexdigest()

        return await asyncio.to_thread(capture)

    @staticmethod
    def _decision(
        run_id: UUID, version: int, frozen: FrozenSubscriptionPublication,
        state: RunState = RunState.AWAITING_PR_APPROVAL,
    ) -> SubscriptionPublicationDecision:
        return SubscriptionPublicationDecision(
            run_id,
            state,
            version,
            frozen.validation.evidence_set_id,
            frozen.acceptance.evidence_set_id,
            frozen.digest,
        )
