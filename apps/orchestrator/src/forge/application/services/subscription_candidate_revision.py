"""Reopen verified subscription publication inputs for a bounded primary revision."""

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.clock import Clock, SystemClock
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.subscription_acceptance import PreparedSubscriptionAcceptance
from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.subscription_candidate_revision import AcceptanceRevision
from forge.application.ports.subscription_validation import AcceptanceValidationRepair
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ControlledGitPort
from forge.application.services.approved_plan import ApprovedPlan, ApprovedPlanLoader
from forge.application.services.control_settlement import pending_current_control_stop
from forge.application.services.subscription_publication_evidence import (
    FrozenSubscriptionPublication,
    SubscriptionPublicationEvidence,
)
from forge.application.services.validation import _fence_command
from forge.domain.approval import ApprovalGate, SubscriptionPlanApprovalEvidence
from forge.domain.command import CommandEnvelope
from forge.domain.operation import canonical_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunState

EVENT = "run.subscription_candidate_revision_requested"


@dataclass(frozen=True, slots=True)
class SubscriptionCandidateRevisionDecision:
    run_id: UUID
    state: RunState
    version: int


def _feedback(command: CommandEnvelope) -> str | None:
    if command.command_type == "approve_pr" and set(command.payload) == {"approval_id"}:
        return None
    value = command.payload.get("feedback")
    if (
        command.command_type != "request_candidate_changes"
        or set(command.payload) != {"feedback"}
        or not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > 16_384
    ):
        raise CommandRecoveryRequired("subscription revision command differs")
    return value


def _payload(
    command: CommandEnvelope,
    approved: ApprovedPlan,
    frozen: FrozenSubscriptionPublication,
    revision: AcceptanceRevision,
    receipt: AcceptanceValidationRepair,
    local_count: int,
) -> dict[str, object]:
    return {
        "source_command_id": str(command.id),
        "source_payload_digest": canonical_digest(command.payload),
        "source_expected_version": command.expected_run_version,
        "plan_approval_id": str(approved.approval_id),
        "acceptance_attempt_id": str(frozen.source.attempt_id),
        "acceptance_digest": frozen.evidence.acceptance_digest,
        "revision": revision.payload(),
        "repair": receipt.payload(),
        "local_remediation_count": local_count,
        "target": (
            RunState.REMEDIATING if receipt.repaired else RunState.AWAITING_HUMAN_INTERVENTION
        ).value,
    }


class SubscriptionCandidateRevisionController:
    def __init__(
        self,
        store: ArtifactStore,
        git_factory: Callable[[ProjectPolicy], ControlledGitPort],
        *,
        clock: Clock | None = None,
    ) -> None:
        self._store, self._git = store, git_factory
        self._clock = clock or SystemClock()
        self._approved = ApprovedPlanLoader(store)
        self._publication = SubscriptionPublicationEvidence(store)

    async def reopen(
        self, command: CommandEnvelope, work: UnitOfWork, approved: ApprovedPlan
    ) -> SubscriptionCandidateRevisionDecision:
        """Leave commit to the caller so approval consumption is in the same transaction."""
        await _fence_command(command, work)
        feedback = _feedback(command)
        automatic = feedback is None
        if (
            command.payload_schema_version != 1
            or command.actor_id is None
            or not isinstance(approved.evidence, SubscriptionPlanApprovalEvidence)
            or approved.run.version != command.expected_run_version
            or approved.run.state is not RunState.AWAITING_PR_APPROVAL
            or await self.replay(command, work, approved) is not None
        ):
            raise CommandRecoveryRequired("subscription revision is not current")
        frozen = await self._publication.at_pr_gate(work, approved)
        source = await self._source(work, frozen)
        observed, diff = await self._observe(source)
        changed = observed != source.review.candidate or diff != frozen.evidence.diff_digest
        if changed != automatic:
            raise CommandRecoveryRequired("subscription revision candidate differs")
        feedback_digest = await self._feedback_artifact(work, command, feedback, persist=True)
        revision = AcceptanceRevision(
            "operator_feedback" if feedback is not None else "content_drift",
            frozen.digest,
            observed,
            diff,
            feedback_digest,
            feedback,
        )
        await _fence_command(command, work)
        current = await self._approved.load(work, command.run_id)
        latest = await self._source(work, frozen)
        if (
            current != approved
            or latest != source
            or await self._observe(latest) != (observed, diff)
            or await pending_current_control_stop(work, current.run)
        ):
            raise CommandRecoveryRequired("subscription revision authority changed")
        await _fence_command(command, work)
        receipt = await work.subscription_decisions.reopen_acceptance_revision(
            source, command.id, revision, approved.evidence.local_remediation_limit
        )
        await work.auth.invalidate_pr_gate(
            run_id=approved.run.id, run_version=approved.run.version, at=self._clock.now()
        )
        payload = _payload(
            command,
            approved,
            frozen,
            revision,
            receipt,
            approved.run.local_remediation_count + int(automatic and receipt.repaired),
        )
        await _fence_command(command, work)
        if receipt.repaired:
            run = await work.runs.begin_local_remediation(
                command.run_id,
                approved.run.version,
                automatic=automatic,
                limit=approved.evidence.local_remediation_limit,
                event_type=EVENT,
                event_payload=payload,
                actor_class="worker" if automatic else "operator",
                actor_id=command.actor_id,
                occurred_at=self._clock.now(),
            )
        else:
            run = await work.runs.intervene(
                command.run_id,
                approved.run.version,
                EVENT,
                payload,
                actor_class="worker" if automatic else "operator",
                actor_id=command.actor_id,
                occurred_at=self._clock.now(),
            )
        await _fence_command(command, work)
        return SubscriptionCandidateRevisionDecision(run.id, run.state, run.version)

    async def replay(
        self, command: CommandEnvelope, work: UnitOfWork, approved: ApprovedPlan
    ) -> SubscriptionCandidateRevisionDecision | None:
        events = [
            event
            for event in await work.events.list_after(command.run_id, 0)
            if event.event_type == EVENT
            and event.payload.get("source_command_id") == str(command.id)
        ]
        if not events:
            return None
        if len(events) != 1:
            raise CommandRecoveryRequired("subscription revision replay is ambiguous")
        event = events[0]
        feedback = _feedback(command)
        revision = AcceptanceRevision.from_payload(event.payload.get("revision"), feedback=feedback)
        receipt = AcceptanceValidationRepair.from_payload(event.payload.get("repair"))
        historical = replace(
            approved,
            run=replace(
                approved.run,
                state=RunState.AWAITING_PR_APPROVAL,
                version=command.expected_run_version,
                pending_gate=ApprovalGate.PR,
                pending_evidence_digest=revision.pr_evidence_digest,
            ),
        )
        frozen = await self._publication.at_pr_gate(work, historical)
        feedback_digest = await self._feedback_artifact(work, command, feedback, persist=False)
        local_count = event.payload.get("local_remediation_count")
        automatic = feedback is None
        changed = (
            revision.observation != frozen.source.review.candidate
            or revision.diff_digest != frozen.evidence.diff_digest
        )
        if (
            command.actor_id is None
            or command.payload_schema_version != 1
            or revision.reason != ("content_drift" if automatic else "operator_feedback")
            or revision.feedback_digest != feedback_digest
            or revision.observation.base_sha != frozen.source.worktree.base_sha
            or changed != automatic
            or type(local_count) is not int
            or local_count < int(automatic and receipt.repaired)
            or local_count > approved.run.local_remediation_count
            or (
                automatic
                and receipt.repaired
                and local_count > approved.evidence.local_remediation_limit
            )
            or event.run_version != command.expected_run_version + 1
            or event.run_version > approved.run.version
            or event.actor_class != ("worker" if automatic else "operator")
            or event.actor_id != command.actor_id
            or event.payload_schema_version != 1
            or canonical_digest(event.payload)
            != canonical_digest(_payload(command, approved, frozen, revision, receipt, local_count))
        ):
            raise CommandRecoveryRequired("subscription revision replay differs")
        await work.subscription_decisions.verify_acceptance_revision(
            frozen.source, command.id, revision, receipt
        )
        return SubscriptionCandidateRevisionDecision(
            command.run_id,
            RunState.REMEDIATING if receipt.repaired else RunState.AWAITING_HUMAN_INTERVENTION,
            event.run_version,
        )

    async def _source(
        self, work: UnitOfWork, frozen: FrozenSubscriptionPublication
    ) -> PreparedSubscriptionAcceptance:
        source, proof = await work.subscription_decisions.acceptance_revision_source(
            frozen.source.attempt_id
        )
        if proof != frozen.source.receipts or any(
            getattr(source, field) != getattr(frozen.source, field)
            for field in ("attempt_id", "decision", "result_digest", "review", "policy", "worktree")
        ):
            raise CommandRecoveryRequired("subscription revision acceptance source differs")
        return source

    async def _observe(
        self, source: PreparedSubscriptionAcceptance
    ) -> tuple[CandidateInspection, str]:
        def capture() -> tuple[CandidateInspection, str]:
            git = self._git(source.policy)
            candidate = git.candidate_diff(source.worktree)
            snapshot = git.working_tree_snapshot(
                source.worktree, secret_paths=source.policy.effective_secret_paths
            )
            observed = CandidateInspection.from_snapshot(snapshot)
            if (
                git.inspect_worktree(source.worktree.identity, source.worktree.base_sha)
                != source.worktree
                or not git.is_ancestor(source.worktree)
                or candidate.diff.truncated
                or candidate.head_sha != observed.head_sha
            ):
                raise CommandRecoveryRequired("subscription revision worktree differs")
            return observed, hashlib.sha256(candidate.diff.text.encode()).hexdigest()

        return await asyncio.to_thread(capture)

    async def _feedback_artifact(
        self, work: UnitOfWork, command: CommandEnvelope, feedback: str | None, *, persist: bool
    ) -> str | None:
        if feedback is None:
            return None
        wire = json.dumps(
            {"schema_version": 1, "command_id": str(command.id), "feedback": feedback},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        digest = hashlib.sha256(wire).hexdigest()
        if persist:
            descriptor = await self._store.put_bytes(wire, media_type="application/json")
            await work.artifacts.record(
                descriptor,
                run_id=command.run_id,
                producer_type="candidate_revision_feedback",
                producer_id=command.id,
            )
        artifacts = await work.artifacts.get_by_producer(
            run_id=command.run_id,
            producer_type="candidate_revision_feedback",
            producer_id=command.id,
        )
        if (
            len(artifacts) != 1
            or artifacts[0].digest != digest
            or artifacts[0].byte_count != len(wire)
            or artifacts[0].media_type != "application/json"
            or artifacts[0].schema_version != 1
            or artifacts[0].truncated
            or artifacts[0].parent_digests
            or await self._store.open_bytes(digest) != wire
        ):
            raise CommandRecoveryRequired("subscription revision feedback artifact differs")
        return digest
