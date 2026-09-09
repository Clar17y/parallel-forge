"""Budgeted remote base update, local adoption, and fresh validation delivery."""

from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.base_adoption import BaseAdoptionPort
from forge.application.ports.clock import Clock, SystemClock
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.github import GitHubPort
from forge.application.ports.github_write import GitHubWritePort
from forge.application.ports.operations import OperationAdapter
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.base_update_authority import base_update_origin
from forge.application.services.base_update_replay import verify_base_update_replay
from forge.application.services.control_settlement import pending_current_control_stop
from forge.application.services.pr_evidence import PrEvidenceValidationError, PrEvidenceValidator
from forge.application.services.recovery import OperationExecutor
from forge.application.services.release_resume import resumed_release_origin
from forge.application.services.resume_source import RESUME_FIELDS
from forge.application.services.validation import _fence_command
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.operation import OperationIntent, OperationRequest, OperationStatus
from forge.domain.policy import ProjectPolicy
from forge.domain.release import GitHubPullRequest
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.models import Approval
from forge.release.base_adoption import BaseAdoptionOperation
from forge.release.base_update import BaseUpdateOperation
from forge.release.controller import ReleaseReconciliationRequired, _validate_intent
from forge.release.git_adoption import ManagedAdoptionError
from forge.release.github_client import GitHubClientError
from forge.release.github_write import GitHubWriteError


class _DurationExpired(RuntimeError):
    """Stop new base-update work after proving its causal delivery authority."""


class BaseUpdateService:
    def __init__(
        self,
        store: ArtifactStore,
        evidence: PrEvidenceValidator,
        reads: GitHubPort,
        writes: GitHubWritePort,
        adoption: Callable[[ProjectPolicy], BaseAdoptionPort],
        executor: OperationExecutor,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._store, self._evidence, self._reads, self._writes = store, evidence, reads, writes
        self._adoption, self._executor = adoption, executor
        self._approved = ApprovedPlanLoader(store)
        self._clock = clock or SystemClock()

    async def execute(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        if (
            command.command_type != "update_base"
            or command.status is not CommandStatus.LEASED
            or command.payload_schema_version != 1
            or set(command.payload) - RESUME_FIELDS
            != {"observation_digest", "pull_request_id", "remote_attempt", "target_base_sha"}
            or type(command.payload["remote_attempt"]) is not int
        ):
            raise CommandRecoveryRequired("base update command is invalid")
        await _fence_command(command, work)
        authority = await resumed_release_origin(work, command)
        approved = await self._approved.load(work, command.run_id)
        run = await work.runs.get_for_update(command.run_id)
        record = await work.releases.get_for_run(run.id)
        if (
            record is None
            or str(record.id) != command.payload["pull_request_id"]
            or command.actor_id != approved.approval_actor_id
            or authority.idempotency_key
            != f"{run.id}:remote-remediation:{command.payload['remote_attempt']}"
        ):
            raise CommandRecoveryRequired("base update authority differs")
        events = await work.events.list_after(run.id, 0)
        interventions = [
            event
            for event in events
            if event.event_type == "run.base_update_intervention"
            and event.payload.get("source_command_id") == str(command.id)
        ]
        if interventions:
            event = interventions[0]
            if event.payload.get("reason") in {
                "base_update_evidence_invalid",
                "base_update_duration_exhausted",
            }:
                approval = await work.auth.get_approval(
                    approval_id=UUID(str(event.payload.get("approval_id"))), for_update=True
                )
                if (
                    len(interventions) != 1
                    or run.state is not RunState.AWAITING_HUMAN_INTERVENTION
                    or run.version != command.expected_run_version + 1
                    or event.run_version != run.version
                    or event.actor_class != "worker"
                    or event.actor_id != command.actor_id
                    or not isinstance(approval, Approval)
                    or approval.invalidated_at is None
                    or event.payload
                    != _evidence_intervention_payload(
                        command, approval, record.id, str(event.payload["reason"])
                    )
                ):
                    raise CommandRecoveryRequired(
                        "base update evidence intervention replay differs"
                    )
                await work.commit()
                return
            intent = await work.operations.get_by_idempotency_key(
                str(event.payload.get("operation_key"))
            )
            approval = await work.auth.get_approval(
                approval_id=UUID(str(event.payload.get("approval_id"))), for_update=True
            )
            if (
                len(interventions) != 1
                or run.state is not RunState.AWAITING_HUMAN_INTERVENTION
                or run.version != command.expected_run_version + 1
                or event.run_version != run.version
                or event.actor_class != "worker"
                or event.actor_id != command.actor_id
                or not isinstance(approval, Approval)
                or approval.invalidated_at is None
                or intent is None
                or intent.status is not OperationStatus.NEEDS_RECONCILIATION
                or event.payload != _intervention_payload(command, approval, record.id, intent)
            ):
                raise CommandRecoveryRequired("base update intervention replay differs")
            await work.commit()
            return
        if await verify_base_update_replay(command, work):
            await work.commit()
            return
        await self._current(command, work, run, check_deadline=False)
        if (
            run.remote_remediation_count != command.payload["remote_attempt"]
            or not 1 <= run.remote_remediation_count <= approved.policy.remote_remediation_limit
        ):
            raise CommandRecoveryRequired("base update budget differs")
        digest, target = await base_update_origin(work, self._store, authority, approved, record)
        publication = await work.operations.get(record.publication_intent_id)
        approval_id = UUID(str(publication.request_payload.get("approval_id")))
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        if (
            not isinstance(approval, Approval)
            or approval.run_id != run.id
            or approval.gate != "pr"
            or approval.invalidated_at is not None
            or approval.policy_version != run.policy_version
            or publication.status is not OperationStatus.SUCCEEDED
            or publication.request_payload.get("approval_digest") != approval.evidence_digest
        ):
            raise CommandRecoveryRequired("base update PR approval differs")
        remote = BaseUpdateOperation(
            record,
            self._reads,
            self._writes,
            target,
            approved.policy.version,
            digest,
            run.remote_remediation_count,
        )
        prior = await work.operations.get_by_idempotency_key(remote.request.idempotency_key)
        if prior is None:
            try:
                await self._evidence.validate_for_base_update(work, run.id, approval_id, target)
            except PrEvidenceValidationError:
                await self._intervene_evidence(command, work, run, approval, record.id)
                return
        try:
            updated = await self._effect(command, work, run, remote.request, remote)
        except _DurationExpired:
            await self._intervene_evidence(
                command, work, run, approval, record.id, "base_update_duration_exhausted"
            )
            return
        except ReleaseReconciliationRequired, GitHubWriteError, GitHubClientError, ManagedAdoptionError:
            await self._intervene_unresolved(command, work, run, approval, remote.request)
            return
        if not run.worktree_path or not run.branch_name or not run.base_sha:
            raise CommandRecoveryRequired("base update worktree differs")
        tree = ManagedWorktree(
            identity=WorktreeIdentity.for_run(
                run.project_id, run.id, run.branch_name, approved.policy.database.enabled
            ),
            path=Path(run.worktree_path),
            base_sha=run.base_sha,
        )
        local = BaseAdoptionOperation(
            record, updated, tree, approved.policy, self._adoption(approved.policy)
        )
        try:
            adopted = await self._effect(command, work, run, local.request, local)
        except _DurationExpired:
            await self._intervene_evidence(
                command, work, run, approval, record.id, "base_update_duration_exhausted"
            )
            return
        except ReleaseReconciliationRequired, GitHubWriteError, GitHubClientError, ManagedAdoptionError:
            await self._intervene_unresolved(command, work, run, approval, local.request)
            return
        await self._current(command, work, run, check_deadline=False)
        pull = GitHubPullRequest(**dict(adopted.outcome or {}))  # type: ignore[arg-type]
        await work.releases.record_base_update(run.id, pull, updated.id, adopted.id)
        attempt = await work.controller_steps.next_attempt(run.id, "validate")
        queued = await work.commands.enqueue(
            run_id=run.id,
            command_type="validate",
            idempotency_key=f"{run.id}:validate:{attempt}",
            payload={"semantic_attempt": attempt},
            expected_run_version=run.version + 1,
            actor_id=approved.approval_actor_id,
        )
        await work.runs.transition(
            run.id,
            run.version,
            RunState.VALIDATING,
            "run.base_updated",
            {
                "source_command_id": str(command.id),
                "pull_request_id": str(record.id),
                "update_intent_id": str(updated.id),
                "adoption_intent_id": str(adopted.id),
                "head_sha": pull.head_sha,
                "base_sha": pull.base_sha,
                "validation_key": queued.idempotency_key,
                "validation_command_id": str(queued.id),
            },
            actor_class="worker",
            actor_id=command.actor_id,
            occurred_at=self._clock.now(),
        )
        await work.commit()

    async def _current(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        run: RunSnapshot,
        *,
        check_deadline: bool = True,
    ) -> None:
        await _fence_command(command, work)
        current = await work.runs.get_for_update(run.id)
        if (
            current != run
            or current.state is not RunState.REMEDIATING
            or current.version != command.expected_run_version
            or await pending_current_control_stop(work, current)
        ):
            raise CommandRecoveryRequired("base update awaits control reconciliation")
        if check_deadline and self._clock.now() >= await work.runs.duration_deadline(run.id):
            raise _DurationExpired()

    async def _effect(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        run: RunSnapshot,
        request: OperationRequest,
        adapter: OperationAdapter,
    ) -> OperationIntent:
        await self._current(command, work, run)
        intent = await work.operations.begin(
            run_id=run.id,
            operation_type=request.kind,
            idempotency_key=request.idempotency_key,
            request_digest=request.request_digest,
            request_payload=request.request_payload,
            execution_owner=f"forge-base-{uuid4().hex}",
            execution_lease_seconds=30,
        )
        await work.commit()
        outcome = await self._executor.execute_admitted(intent, adapter)
        if outcome.status is not OperationStatus.SUCCEEDED:
            raise CommandRecoveryRequired("base update outcome is unresolved")
        await self._current(command, work, run)
        return await work.operations.get(intent.id)

    async def _intervene_unresolved(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        run: RunSnapshot,
        approval: Approval,
        request: OperationRequest,
    ) -> None:
        await _fence_command(command, work)
        current = await work.runs.get_for_update(run.id)
        if (
            current != run
            or current.state is not RunState.REMEDIATING
            or current.version != command.expected_run_version
            or await pending_current_control_stop(work, current)
        ):
            raise CommandRecoveryRequired("base update uncertainty awaits control reconciliation")
        intent = await work.operations.get_by_idempotency_key(request.idempotency_key)
        if (
            intent is None
            or intent.status is not OperationStatus.NEEDS_RECONCILIATION
            or intent.execution_owner is not None
        ):
            raise CommandRecoveryRequired("base update uncertainty awaits reconciliation")
        _validate_intent(intent, request)
        await work.auth.invalidate_pr_gate(
            run_id=run.id, run_version=approval.run_version, at=self._clock.now()
        )
        record = await work.releases.get_for_run(run.id)
        if record is None:
            raise CommandRecoveryRequired("base update record is absent")
        await work.runs.intervene(
            run.id,
            run.version,
            "run.base_update_intervention",
            _intervention_payload(command, approval, record.id, intent),
            actor_class="worker",
            actor_id=command.actor_id,
            occurred_at=self._clock.now(),
        )
        await work.commit()

    async def _intervene_evidence(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        run: RunSnapshot,
        approval: Approval,
        record_id: UUID,
        reason: str = "base_update_evidence_invalid",
    ) -> None:
        await _fence_command(command, work)
        current = await work.runs.get_for_update(run.id)
        if (
            current != run
            or current.state is not RunState.REMEDIATING
            or current.version != command.expected_run_version
            or await pending_current_control_stop(work, current)
        ):
            raise CommandRecoveryRequired("base update evidence awaits control reconciliation")
        await work.auth.invalidate_pr_gate(
            run_id=run.id, run_version=approval.run_version, at=self._clock.now()
        )
        await work.runs.intervene(
            run.id,
            run.version,
            "run.base_update_intervention",
            _evidence_intervention_payload(command, approval, record_id, reason),
            actor_class="worker",
            actor_id=command.actor_id,
            occurred_at=self._clock.now(),
        )
        await work.commit()


def _intervention_payload(
    command: CommandEnvelope, approval: Approval, record_id: UUID, intent: OperationIntent
) -> dict[str, object]:
    return {
        "source_command_id": str(command.id),
        "approval_id": str(approval.id),
        "evidence_digest": approval.evidence_digest,
        "pull_request_id": str(record_id),
        "operation_key": intent.idempotency_key,
        "operation_intent_id": str(intent.id),
        "request_digest": intent.request_digest,
        "reason": "base_update_outcome_unresolved",
    }


def _evidence_intervention_payload(
    command: CommandEnvelope,
    approval: Approval,
    record_id: UUID,
    reason: str = "base_update_evidence_invalid",
) -> dict[str, object]:
    return {
        "source_command_id": str(command.id),
        "approval_id": str(approval.id),
        "evidence_digest": approval.evidence_digest,
        "pull_request_id": str(record_id),
        "reason": reason,
    }
