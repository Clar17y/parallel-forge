"""Authorize an operator-requested revision of the frozen local candidate."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.clock import Clock, SystemClock
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.application.services.approvals import ApprovalCommandValidationError
from forge.application.services.approved_plan import ApprovedPlan, ApprovedPlanLoader
from forge.domain.approval import ApprovalGate, PrApprovalEvidence, canonical_digest
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunState


class CandidateRevisionError(ApprovalCommandValidationError):
    """Candidate revision command is not bound to current PR evidence."""


class CandidateRevisionService:
    def __init__(
        self,
        artifact_store: ArtifactStore,
        approved_plans: ApprovedPlanLoader,
        git_factory: Callable[[ProjectPolicy], ControlledGitPort],
        *,
        clock: Clock | None = None,
    ) -> None:
        self._store = artifact_store
        self._approved = approved_plans
        self._git_factory = git_factory
        self._clock = clock or SystemClock()

    async def execute(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        await self._fence(command, work)
        if (
            command.command_type != "request_candidate_changes"
            or command.status is not CommandStatus.LEASED
            or command.payload_schema_version != 1
            or command.actor_id is None
            or set(command.payload) != {"feedback"}
            or not isinstance(command.payload.get("feedback"), str)
            or not cast(str, command.payload.get("feedback")).strip()
            or len(cast(str, command.payload.get("feedback")).encode("utf-8")) > 16_384
        ):
            raise CandidateRevisionError("candidate revision is not admissible")
        feedback = cast(str, command.payload["feedback"])
        if "\x00" in feedback:
            raise CandidateRevisionError("candidate feedback contains an invalid character")
        feedback_bytes = json.dumps(
            {"schema_version": 1, "command_id": str(command.id), "feedback": feedback},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        feedback_digest = hashlib.sha256(feedback_bytes).hexdigest()
        run = await work.runs.get_for_update(command.run_id)
        if (
            run.state is RunState.REMEDIATING
            and run.version == command.expected_run_version + 1
            and await self._replay(command, work, feedback_digest)
        ):
            await self._fence(command, work)
            await work.commit()
            return
        if (
            run.state is not RunState.AWAITING_PR_APPROVAL
            or run.version != command.expected_run_version
        ):
            raise CandidateRevisionError("candidate revision is not admissible")
        approved = await self._approved.load(work, command.run_id)
        if (
            approved.run != run
            or run.pending_gate is not ApprovalGate.PR
            or not run.pending_evidence_digest
        ):
            raise CandidateRevisionError("PR approval evidence is not current")
        descriptor = await self._store.put_bytes(
            feedback_bytes,
            media_type="application/json",
            max_bytes=131_072,
            bounding_policy="head_tail",
        )
        persisted = await work.artifacts.record(
            descriptor,
            run_id=run.id,
            producer_type="candidate_revision_feedback",
            producer_id=command.id,
        )
        evidence_descriptor = await work.artifacts.get_by_digest(
            run.pending_evidence_digest, run_id=run.id
        )
        evidence_bytes = await self._store.open_bytes(run.pending_evidence_digest)
        evidence = PrApprovalEvidence.model_validate_json(evidence_bytes)
        candidate_head, candidate_digest = self._candidate(approved)
        if (
            evidence_descriptor.digest != run.pending_evidence_digest
            or evidence_descriptor.media_type != "application/json"
            or evidence_descriptor.byte_count != len(evidence_bytes)
            or evidence_descriptor.truncated
            or hashlib.sha256(evidence_bytes).hexdigest() != evidence_descriptor.digest
            or canonical_digest(evidence) != evidence_descriptor.digest
            or evidence_descriptor.producer_type != "pr_approval_evidence"
            or evidence.candidate_commit != candidate_head
            or evidence.diff_digest != candidate_digest
            or evidence.base_sha != approved.evidence.base_sha
            or evidence.repository != approved.policy.github_repository
            or evidence.base_ref != run.base_ref
            or evidence.runner_mode != approved.policy.runner_mode
            or evidence.remote_remediation_limit != approved.policy.remote_remediation_limit
        ):
            raise CandidateRevisionError("PR approval evidence is stale")
        frozen = [
            event
            for event in await work.events.list_for_version(run.id, run.version)
            if event.event_type == "run.review_decided"
        ]
        if (
            len(frozen) != 1
            or frozen[0].actor_class != "worker"
            or frozen[0].actor_id is not None
            or frozen[0].payload.get("target") != RunState.AWAITING_PR_APPROVAL.value
            or frozen[0].payload.get("approval_id") != str(approved.approval_id)
            or frozen[0].payload.get("pr_evidence_digest") != run.pending_evidence_digest
            or frozen[0].payload.get("validation_digest") != evidence.validation_digest
            or frozen[0].payload.get("review_digest") != evidence.review_digest
        ):
            raise CandidateRevisionError("candidate has no matching freeze event")
        try:
            validation_id = UUID(str(frozen[0].payload["validation_evidence_set_id"]))
            review_id = UUID(str(frozen[0].payload["review_evidence_set_id"]))
        except ValueError, KeyError:
            raise CandidateRevisionError("candidate evidence identifiers are invalid") from None
        validation = await work.evidence.get_by_id(validation_id, run_id=run.id)
        review = await work.evidence.get_by_id(review_id, run_id=run.id)
        if (
            validation.manifest_digest != evidence.validation_digest
            or review.manifest_digest != evidence.review_digest
            or review.validation_evidence_set_id != validation_id
            or validation.head_sha != candidate_head
            or review.head_sha != candidate_head
            or validation.policy_version != approved.policy.version
            or review.policy_version != approved.policy.version
            or evidence_descriptor.producer_id != review_id
        ):
            raise CandidateRevisionError("candidate evidence binding differs")
        await work.auth.invalidate_pr_gate(
            run_id=run.id, run_version=run.version, at=self._clock.now()
        )
        attempt = await work.executions.next_attempt(run.id, "implement")
        payload = {
            "semantic_attempt": attempt,
            "automatic": False,
            "feedback_digest": persisted.digest,
            "feedback_command_id": str(command.id),
            "pr_evidence_digest": run.pending_evidence_digest,
            "candidate_commit": evidence.candidate_commit,
            "validation_evidence_set_id": str(validation_id),
            "prior_review_evidence_set_id": str(review_id),
        }
        queued = await work.commands.enqueue(
            run_id=run.id,
            command_type="remediate",
            idempotency_key=f"{run.id}:human-remediate:{attempt}",
            payload=payload,
            expected_run_version=run.version + 1,
            actor_id=command.actor_id,
        )
        await work.runs.begin_local_remediation(
            run.id,
            run.version,
            automatic=False,
            limit=approved.evidence.local_remediation_limit,
            event_type="run.candidate_revision_requested",
            event_payload={
                "source_command_id": str(command.id),
                "approval_id": str(approved.approval_id),
                "feedback_digest": persisted.digest,
                "pr_evidence_digest": run.pending_evidence_digest,
                "candidate_commit": evidence.candidate_commit,
                "queued_command_id": str(queued.id),
                "queued_key": queued.idempotency_key,
                "queued_payload": dict(payload),
                "local_remediation_count": run.local_remediation_count,
            },
            actor_class="operator",
            actor_id=command.actor_id,
            occurred_at=self._clock.now(),
        )
        await self._fence(command, work)
        if self._candidate(approved) != (candidate_head, candidate_digest):
            raise CandidateRevisionError("candidate changed during revision authorization")
        await work.commit()

    async def _fence(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        current = await work.commands.assert_current_lease(command)
        if replace(current, lease_expires_at=command.lease_expires_at) != command:
            raise CandidateRevisionError("candidate revision lease changed")

    def _candidate(self, plan: ApprovedPlan) -> tuple[str, str]:
        run = plan.run
        if not run.worktree_path or not run.branch_name or not run.base_sha:
            raise CandidateRevisionError("candidate worktree is unavailable")
        identity = WorktreeIdentity.for_run(
            run.project_id, run.id, run.branch_name, plan.policy.database.enabled
        )
        worktree = ManagedWorktree(
            identity=identity, path=Path(run.worktree_path), base_sha=run.base_sha
        )
        git = self._git_factory(plan.policy)
        candidate = git.candidate_diff(worktree)
        return candidate.head_sha, hashlib.sha256(candidate.diff.text.encode("utf-8")).hexdigest()

    async def _replay(self, command: CommandEnvelope, work: UnitOfWork, digest: str) -> bool:
        artifacts = await work.artifacts.get_by_producer(
            run_id=command.run_id,
            producer_type="candidate_revision_feedback",
            producer_id=command.id,
        )
        events = [
            event
            for event in await work.events.list_after(command.run_id, 0)
            if event.event_type == "run.candidate_revision_requested"
            and event.payload.get("source_command_id") == str(command.id)
        ]
        if (
            len(artifacts) != 1
            or artifacts[0].digest != digest
            or artifacts[0].media_type != "application/json"
            or artifacts[0].truncated
            or not await self._store.verify(digest)
            or len(events) != 1
        ):
            return False
        event = events[0]
        queued_id = event.payload.get("queued_command_id")
        queued_key = event.payload.get("queued_key")
        queued = (
            await work.commands.get_by_idempotency_key(queued_key)
            if isinstance(queued_key, str)
            else None
        )
        return (
            event.actor_class == "operator"
            and event.actor_id == command.actor_id
            and event.run_version == command.expected_run_version + 1
            and event.payload.get("feedback_digest") == digest
            and isinstance(queued_id, str)
            and isinstance(queued_key, str)
            and event.payload.get("queued_payload") is not None
            and queued is not None
            and str(queued.id) == queued_id
            and queued.idempotency_key == queued_key
            and queued.actor_id == command.actor_id
            and queued.command_type == "remediate"
            and queued.run_id == command.run_id
            and queued.payload_schema_version == 1
            and queued.status is CommandStatus.PENDING
            and queued.expected_run_version == command.expected_run_version + 1
            and queued.payload == event.payload.get("queued_payload")
        )


__all__ = ["CandidateRevisionError", "CandidateRevisionService"]
