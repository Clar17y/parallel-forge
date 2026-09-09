"""Durable PR publication orchestration; remote effects occur after commit."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import cast
from uuid import UUID, uuid4

from forge.application.ports.clock import Clock, SystemClock
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.git_push import ManagedPushPort
from forge.application.ports.github_write import GitHubWritePort
from forge.application.ports.operations import OperationAdapter
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.control_settlement import pending_current_control_stop
from forge.application.services.pr_evidence import (
    PrEvidenceValidationError,
    PrEvidenceValidator,
    ValidatedPrEvidence,
)
from forge.application.services.publication_replay import verify_publication_replay
from forge.application.services.recovery import OperationExecutor
from forge.application.services.release_resume import resumed_release_origin
from forge.application.services.resume_source import RESUME_FIELDS
from forge.application.services.reviewed_push_replay import verify_reviewed_push_replay
from forge.application.services.validation import _fence_command
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationRequest,
    OperationStatus,
    canonical_digest,
)
from forge.domain.policy import ProjectPolicy
from forge.domain.release import GitHubPullRequest
from forge.domain.run import RunState
from forge.persistence.models import Approval
from forge.release.controller import (
    Publication,
    PullRequestOperation,
    PushOperation,
    ReleaseReconciliationRequired,
    ReviewedPushOperation,
    _validate_intent,
)
from forge.release.git_push import ManagedPushError
from forge.release.github_write import GitHubWriteError


class ReleaseService:
    def __init__(
        self,
        evidence: PrEvidenceValidator,
        github: GitHubWritePort,
        push_factory: Callable[[ProjectPolicy], ManagedPushPort],
        executor: OperationExecutor,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._evidence, self._github = evidence, github
        self._push_factory, self._executor = push_factory, executor
        self._clock = clock or SystemClock()

    async def push_reviewed(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        await _fence_command(command, work)
        await resumed_release_origin(work, command)
        if await self._evidence_rejection_replay(command, work):
            return
        try:
            await self._push_reviewed(command, work)
        except PrEvidenceValidationError as error:
            await self._reject_evidence(command, work, error.category)

    async def _push_reviewed(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        if command.command_type != "push_reviewed_pr" or command.status is not CommandStatus.LEASED:
            raise CommandRecoveryRequired("reviewed push command is invalid")
        await _fence_command(command, work)
        if await self._intervention_replay(command, work):
            return
        run = await work.runs.get_for_update(command.run_id)
        events = await work.events.list_after(run.id, 0)
        record = await work.releases.get_for_run(run.id)
        if record is None:
            raise CommandRecoveryRequired("reviewed push PR is absent")
        if await verify_reviewed_push_replay(command, work):
            await self._rejection_approval(command, work)
            await work.commit()
            return
        if (
            run.state is not RunState.MONITORING_PR
            or run.version != command.expected_run_version
            or await pending_current_control_stop(work, run)
        ):
            raise CommandRecoveryRequired("reviewed push awaits control reconciliation")
        verified = await self._evidence.validate_reviewed_push(work, command)
        approval_id = UUID(str(command.payload["approval_id"]))
        approval = cast(
            Approval | None, await work.auth.get_approval(approval_id=approval_id, for_update=True)
        )
        if approval is None or approval.invalidated_at is not None:
            raise CommandRecoveryRequired("reviewed push approval differs")
        adapter = ReviewedPushOperation(
            record,
            approval_id,
            approval.evidence_digest,
            verified.evidence,
            self._github,
            self._push_factory(verified.approved.policy),
            verified.worktree,
            verified.approved.policy,
        )
        result = await self._effect(command, work, adapter.request, adapter)
        if result is None:
            return
        intent_id, outcome = result
        await _fence_command(command, work)
        current = await work.runs.get_for_update(run.id)
        if current != run or await pending_current_control_stop(work, current):
            raise CommandRecoveryRequired("reviewed push settlement awaits control reconciliation")
        rechecked = await self._evidence.validate_reviewed_push(work, command)
        if rechecked.evidence != verified.evidence:
            raise PrEvidenceValidationError("content_drift")
        pull = GitHubPullRequest(**dict(outcome.payload))  # type: ignore[arg-type]
        updated = await work.releases.record_reviewed_push(run.id, pull, intent_id)
        polls = [
            event.payload.get("poll") for event in events if event.event_type == "run.pr_observed"
        ]
        if not polls or any(type(poll) is not int or poll < 1 for poll in polls):
            raise CommandRecoveryRequired("reviewed push poll history differs")
        poll = max(cast(list[int], polls))
        key = f"{run.id}:monitor-pr:{poll + 1}"
        queued = await work.commands.enqueue(
            run_id=run.id,
            command_type="monitor_pr",
            idempotency_key=key,
            payload={"pull_request_id": str(record.id), "poll": poll + 1},
            expected_run_version=run.version,
            actor_id=command.actor_id,
            available_at=self._clock.now() + timedelta(seconds=15),
        )
        await work.events.append(
            RunEvent(
                run_id=run.id,
                run_version=run.version,
                event_type="run.pr_updated",
                payload={
                    "source_command_id": str(command.id),
                    "pull_request_id": str(record.id),
                    "push_intent_id": str(intent_id),
                    "candidate_evidence_digest": updated.candidate_evidence_digest,
                    "poll": poll,
                    "monitor_command_id": str(queued.id),
                    "monitor_key": key,
                },
                actor_class="worker",
                actor_id=command.actor_id,
                occurred_at=self._clock.now(),
            )
        )
        await work.commit()

    async def publish(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        await _fence_command(command, work)
        await resumed_release_origin(work, command)
        _publication_approval(command)
        await self._rejection_approval(command, work)
        if await self._evidence_rejection_replay(command, work):
            return
        try:
            await self._publish(command, work)
        except PrEvidenceValidationError as error:
            await self._reject_evidence(command, work, error.category)

    async def _publish(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        approval_id = _publication_approval(command)
        await _fence_command(command, work)
        if await self._intervention_replay(command, work):
            return
        if await verify_publication_replay(command, work, approval_id):
            await work.commit()
            return
        verified = await self._current(command, work, approval_id)
        publication = self._publication(verified, approval_id)
        push = PushOperation(
            publication,
            self._github,
            self._push_factory(verified.approved.policy),
            verified.worktree,
            verified.approved.policy,
        )
        pushed = await self._effect(command, work, push.request, push)
        if pushed is None:
            return
        push_id, _ = pushed
        # Recheck authorization/candidate after the first external effect. A
        # queued control or drift prevents admission of the next external write.
        verified = await self._current(command, work, approval_id)
        if self._publication(verified, approval_id) != publication:
            raise PrEvidenceValidationError("content_drift")
        create = PullRequestOperation(publication, self._github, verified.body)
        created = await self._effect(command, work, create.request, create)
        if created is None:
            return
        publication_id, outcome = created
        await _fence_command(command, work)
        run = await work.runs.get_for_update(command.run_id)
        if (
            run.state is not RunState.PUBLISHING_PR
            or run.version != command.expected_run_version
            or await pending_current_control_stop(work, run)
        ):
            raise CommandRecoveryRequired("publication settlement awaits control reconciliation")
        try:
            pull = GitHubPullRequest(**dict(outcome.payload))  # type: ignore[arg-type]
        except TypeError, ValueError:
            raise CommandRecoveryRequired("publication outcome is invalid") from None
        recorded = await work.releases.record_publication(run.id, pull, push_id, publication_id)
        queued = await work.commands.enqueue(
            run_id=run.id,
            command_type="monitor_pr",
            idempotency_key=f"{run.id}:monitor-pr:1",
            payload={"pull_request_id": str(recorded.id), "poll": 1},
            expected_run_version=run.version + 1,
            actor_id=command.actor_id,
            available_at=self._clock.now() + timedelta(seconds=15),
        )
        await work.runs.transition(
            run.id,
            run.version,
            RunState.MONITORING_PR,
            "run.pr_published",
            {
                "source_command_id": str(command.id),
                "approval_id": str(approval_id),
                "pull_request_id": str(recorded.id),
                "node_id": pull.node_id,
                "push_intent_id": str(push_id),
                "publication_intent_id": str(publication_id),
                "monitor_command_id": str(queued.id),
            },
            actor_class="worker",
            actor_id=command.actor_id,
            occurred_at=self._clock.now(),
        )
        await work.commit()

    async def _current(
        self, command: CommandEnvelope, work: UnitOfWork, approval_id: UUID
    ) -> ValidatedPrEvidence:
        await _fence_command(command, work)
        run = await work.runs.get_for_update(command.run_id)
        if (
            run.state is not RunState.PUBLISHING_PR
            or run.version != command.expected_run_version
            or await pending_current_control_stop(work, run)
        ):
            raise CommandRecoveryRequired("publication awaits control reconciliation")
        approval = cast(
            Approval | None, await work.auth.get_approval(approval_id=approval_id, for_update=True)
        )
        if approval is None or approval.authenticated_actor_id != command.actor_id:
            raise CommandRecoveryRequired("publication approval actor differs")
        result = await self._evidence.validate_for_publication(work, run.id, approval_id)
        await _fence_command(command, work)
        return result

    async def _rejection_approval(self, command: CommandEnvelope, work: UnitOfWork) -> Approval:
        """Prove the causal delivery independently of the evidence that failed revalidation."""
        if (
            command.status is not CommandStatus.LEASED
            or command.payload_schema_version != 1
            or command.actor_id is None
        ):
            raise CommandRecoveryRequired("publication rejection command differs")
        try:
            approval_id = UUID(str(command.payload.get("approval_id")))
        except ValueError:
            raise CommandRecoveryRequired("publication rejection approval is invalid") from None
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        if (
            not isinstance(approval, Approval)
            or approval.run_id != command.run_id
            or approval.gate != "pr"
        ):
            raise CommandRecoveryRequired("publication rejection approval differs")
        authority = await resumed_release_origin(work, command)
        events = await work.events.list_for_version(command.run_id, authority.expected_run_version)
        if command.command_type == "publish_pr":
            expected = {
                "id": str(authority.id),
                "key": authority.idempotency_key,
                "command_type": command.command_type,
                "payload": dict(authority.payload),
                "actor_id": str(command.actor_id),
                "version": authority.expected_run_version,
            }
            causal = [
                e
                for e in events
                if e.event_type == "run.pr_approval_consumed"
                and e.actor_class == "worker"
                and e.actor_id == command.actor_id
                and e.payload.get("approval_id") == str(approval.id)
                and e.payload.get("approval_digest") == approval.evidence_digest
                and e.payload.get("target") == RunState.PUBLISHING_PR.value
                and e.payload.get("invalidated") is False
                and e.payload.get("queued") == expected
            ]
            if approval.authenticated_actor_id != command.actor_id:
                raise CommandRecoveryRequired("publication rejection actor differs")
        elif command.command_type == "push_reviewed_pr":
            causal = [
                e
                for e in events
                if e.event_type == "run.review_decided"
                and e.actor_class == "worker"
                and e.actor_id is None
                and e.payload.get("target") == RunState.MONITORING_PR.value
                and e.payload.get("queued_command_id") == str(authority.id)
                and e.payload.get("queued_key") == authority.idempotency_key
                and e.payload.get("queued_payload") == authority.payload
                and e.payload.get("pr_evidence_digest")
                == command.payload.get("candidate_evidence_digest")
            ]
        else:
            raise CommandRecoveryRequired("publication rejection stage differs")
        if len(causal) != 1:
            raise CommandRecoveryRequired("publication rejection has no causal authority")
        return approval

    async def _reject_evidence(
        self, command: CommandEnvelope, work: UnitOfWork, reason: str
    ) -> None:
        await _fence_command(command, work)
        run = await work.runs.get_for_update(command.run_id)
        expected = (
            RunState.PUBLISHING_PR
            if command.command_type == "publish_pr"
            else RunState.MONITORING_PR
        )
        if (
            run.state is not expected
            or run.version != command.expected_run_version
            or await pending_current_control_stop(work, run)
        ):
            raise CommandRecoveryRequired(
                "publication evidence rejection awaits control reconciliation"
            )
        approval = await self._rejection_approval(command, work)
        await work.auth.invalidate_pr_gate(
            run_id=run.id, run_version=approval.run_version, at=self._clock.now()
        )
        reason = (
            reason
            if reason
            in {"content_drift", "remote_base_drift", "remote_read_failed", "authority_drift"}
            else "authority_drift"
        )
        # Publication authority has already been consumed, and an earlier effect
        # may have succeeded. Preserve its receipts for operator reconciliation.
        await work.runs.intervene(
            run.id,
            run.version,
            "run.publication_evidence_rejected",
            _rejection_payload(command, approval, reason),
            actor_class="worker",
            actor_id=command.actor_id,
            occurred_at=self._clock.now(),
        )
        await work.commit()

    async def _evidence_rejection_replay(self, command: CommandEnvelope, work: UnitOfWork) -> bool:
        events = [
            e
            for e in await work.events.list_after(command.run_id, 0)
            if e.event_type == "run.publication_evidence_rejected"
            and e.payload.get("source_command_id") == str(command.id)
        ]
        if not events:
            return False
        approval = await self._rejection_approval(command, work)
        run = await work.runs.get_for_update(command.run_id)
        event = events[0]
        reason = event.payload.get("reason")
        if (
            len(events) != 1
            or run.state is not RunState.AWAITING_HUMAN_INTERVENTION
            or run.version != command.expected_run_version + 1
            or event.run_version != run.version
            or event.actor_class != "worker"
            or event.actor_id != command.actor_id
            or approval.invalidated_at is None
            or reason
            not in {"content_drift", "remote_base_drift", "remote_read_failed", "authority_drift"}
            or event.payload != _rejection_payload(command, approval, str(reason))
        ):
            raise CommandRecoveryRequired("publication evidence rejection replay differs")
        await work.commit()
        return True

    async def _effect(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        request: OperationRequest,
        adapter: OperationAdapter,
    ) -> tuple[UUID, OperationOutcome] | None:
        await _fence_command(command, work)
        intent = await work.operations.begin(
            run_id=request.run_id,
            operation_type=request.kind,
            idempotency_key=request.idempotency_key,
            request_digest=request.request_digest,
            request_payload=request.request_payload,
            execution_owner=f"forge-release-{uuid4().hex}",
            execution_lease_seconds=30,
        )
        await work.commit()
        try:
            outcome = await self._executor.execute_admitted(intent, adapter)
        except ReleaseReconciliationRequired, GitHubWriteError, ManagedPushError:
            await _fence_command(command, work)
            run = await work.runs.get_for_update(command.run_id)
            persisted = await work.operations.get(intent.id)
            _validate_intent(persisted, request)
            if (
                run.version != command.expected_run_version
                or run.state
                is not (
                    RunState.PUBLISHING_PR
                    if command.command_type == "publish_pr"
                    else RunState.MONITORING_PR
                )
                or await pending_current_control_stop(work, run)
            ):
                raise CommandRecoveryRequired(
                    "publication uncertainty awaits control reconciliation"
                ) from None
            approval = await self._intervention_approval(command, work, persisted)
            await work.auth.invalidate_pr_gate(
                run_id=run.id, run_version=approval.run_version, at=self._clock.now()
            )
            await work.runs.intervene(
                run.id,
                run.version,
                "run.publication_intervention",
                _intervention_payload(command, persisted),
                actor_class="worker",
                actor_id=command.actor_id,
                occurred_at=self._clock.now(),
            )
            await work.commit()
            return None
        if outcome.status is not OperationStatus.SUCCEEDED:
            raise CommandRecoveryRequired("publication outcome requires reconciliation")
        return intent.id, outcome

    async def _intervention_approval(
        self, command: CommandEnvelope, work: UnitOfWork, intent: OperationIntent
    ) -> Approval:
        try:
            approval_id = UUID(str(intent.request_payload.get("approval_id")))
        except ValueError:
            raise CommandRecoveryRequired("publication uncertain approval differs") from None
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        prefix = "push-reviewed" if command.command_type == "push_reviewed_pr" else intent.kind
        if (
            not isinstance(approval, Approval)
            or approval.run_id != command.run_id
            or approval.gate != "pr"
            or intent.run_id != command.run_id
            or intent.kind not in {"push_branch", "create_pr"}
            or intent.status is not OperationStatus.NEEDS_RECONCILIATION
            or intent.execution_owner is not None
            or intent.request_schema_version != 1
            or intent.request_digest != canonical_digest(intent.request_payload)
            or intent.idempotency_key != f"{command.run_id}:{prefix}:{intent.request_digest}"
            or intent.request_payload.get("approval_digest") != approval.evidence_digest
            or intent.request_payload.get("policy_version") != approval.policy_version
            or command.payload.get("approval_id") != str(approval_id)
        ):
            raise CommandRecoveryRequired("publication uncertain authority differs")
        return approval

    async def _intervention_replay(self, command: CommandEnvelope, work: UnitOfWork) -> bool:
        events = [
            e
            for e in await work.events.list_after(command.run_id, 0)
            if e.event_type == "run.publication_intervention"
            and e.payload.get("source_command_id") == str(command.id)
        ]
        if not events:
            return False
        event = events[0]
        intent = await work.operations.get_by_idempotency_key(
            str(event.payload.get("operation_key"))
        )
        if intent is None:
            raise CommandRecoveryRequired("publication intervention intent is absent")
        approval = await self._intervention_approval(command, work, intent)
        run = await work.runs.get_for_update(command.run_id)
        if (
            len(events) != 1
            or run.state is not RunState.AWAITING_HUMAN_INTERVENTION
            or run.version != command.expected_run_version + 1
            or event.run_version != run.version
            or event.actor_class != "worker"
            or event.actor_id != command.actor_id
            or approval.invalidated_at is None
            or event.payload != _intervention_payload(command, intent)
        ):
            raise CommandRecoveryRequired("publication intervention replay differs")
        await work.commit()
        return True

    @staticmethod
    def _publication(verified: ValidatedPrEvidence, approval_id: UUID) -> Publication:
        return Publication(
            run_id=verified.approved.run.id,
            approval_id=approval_id,
            policy_version=verified.approved.policy.version,
            branch=verified.worktree.identity.branch,
            evidence=verified.evidence,
        )


def _publication_approval(command: CommandEnvelope) -> UUID:
    if (
        command.command_type != "publish_pr"
        or command.status is not CommandStatus.LEASED
        or command.payload_schema_version != 1
        or set(command.payload) - RESUME_FIELDS != {"approval_id"}
        or command.actor_id is None
    ):
        raise CommandRecoveryRequired("publication command is invalid")
    try:
        return UUID(str(command.payload["approval_id"]))
    except ValueError, TypeError:
        raise CommandRecoveryRequired("publication approval is invalid") from None


def _intervention_payload(command: CommandEnvelope, intent: OperationIntent) -> dict[str, object]:
    return {
        "source_command_id": str(command.id),
        "command_type": command.command_type,
        "command_payload": dict(command.payload),
        "operation_key": intent.idempotency_key,
        "operation_intent_id": str(intent.id),
        "request_digest": intent.request_digest,
        "reason": "publication_outcome_unresolved",
    }


def _rejection_payload(
    command: CommandEnvelope, approval: Approval, reason: str
) -> dict[str, object]:
    return {
        "source_command_id": str(command.id),
        "command_type": command.command_type,
        "command_payload": dict(command.payload),
        "approval_id": str(approval.id),
        "approval_digest": approval.evidence_digest,
        "reason": reason,
    }
