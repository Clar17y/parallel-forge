"""Reopen an accepted primary using the monitor's bounded remote repair authority."""

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.subscription_acceptance import (
    PreparedSubscriptionAcceptance,
    RetainedSubscriptionAcceptance,
)
from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.subscription_validation import AcceptanceValidationRepair
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ControlledGitPort
from forge.application.services.approved_plan import ApprovedPlan, ApprovedPlanLoader
from forge.application.services.control_settlement import pending_current_control_stop
from forge.application.services.pr_evidence import PrEvidenceValidator
from forge.application.services.remote_remediation import remote_evidence, remote_publication_record
from forge.application.services.resume_source import (
    RESUME_FIELDS,
    resume_command_ids,
    resume_origin,
)
from forge.application.services.resume_successor import resumed_successor
from forge.application.services.subscription_delivery_ack import require_acknowledged_delivery
from forge.application.services.validation import _fence_command
from forge.domain.agent import UntrustedContent
from forge.domain.approval import SubscriptionPrApprovalEvidence, canonical_digest
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.evidence import SubscriptionAcceptanceEvidenceManifest, decode_evidence_manifest
from forge.domain.operation import canonical_digest as payload_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunState

if TYPE_CHECKING:
    from forge.application.services.subscription_publication_evidence import (
        FrozenSubscriptionPublication,
    )

EVENT = "run.subscription_remote_repair_requested"


@dataclass(frozen=True, slots=True)
class SubscriptionRemoteRemediationDecision:
    run_id: UUID
    state: RunState
    version: int


def _command(command: CommandEnvelope) -> int:
    remote = command.payload.get("remote_attempt")
    semantic = command.payload.get("semantic_attempt")
    identities = resume_command_ids(command.payload)
    if (
        command.command_type != "remediate_remote"
        or command.payload_schema_version != 1
        or set(command.payload)
        != {"observation_digest", "pull_request_id", "remote_attempt", "semantic_attempt"}
        | (RESUME_FIELDS if identities is not None else frozenset())
        or type(remote) is not int
        or remote < 1
        or type(semantic) is not int
        or semantic < 1
        or command.idempotency_key
        != (
            f"{command.run_id}:remote-remediation:{remote}"
            if identities is None
            else f"{command.run_id}:resume:{identities[0]}:remediate_remote:{semantic}"
        )
    ):
        raise CommandRecoveryRequired("subscription remote repair command differs")
    return remote


async def _origin(work: UnitOfWork, command: CommandEnvelope) -> CommandEnvelope:
    _command(command)
    original = (
        await resume_origin(work, command, historical=command.status is not CommandStatus.LEASED)
        or command
    )
    _command(original)
    return original


def _payload(
    command: CommandEnvelope,
    approved: ApprovedPlan,
    source: RetainedSubscriptionAcceptance,
    evidence: SubscriptionPrApprovalEvidence,
    repair: AcceptanceValidationRepair,
) -> dict[str, object]:
    return {
        "source_command_id": str(command.id),
        "source_payload_digest": payload_digest(command.payload),
        "source_expected_version": command.expected_run_version,
        "plan_approval_id": str(approved.approval_id),
        "acceptance_attempt_id": str(source.attempt_id),
        "acceptance_digest": evidence.acceptance_digest,
        "pr_evidence_digest": canonical_digest(evidence),
        "observation_digest": command.payload["observation_digest"],
        "remote_attempt": _command(command),
        "repair": repair.payload(),
        "target": (
            RunState.REMEDIATING if repair.repaired else RunState.AWAITING_HUMAN_INTERVENTION
        ).value,
    }


class SubscriptionRemoteRemediationController:
    def __init__(
        self,
        store: ArtifactStore,
        publications: PrEvidenceValidator,
        git_factory: Callable[[ProjectPolicy], ControlledGitPort],
    ) -> None:
        self._store, self._publications, self._git = store, publications, git_factory
        self._approved = ApprovedPlanLoader(store)
        from forge.application.services.subscription_base_update import (
            SubscriptionBaseUpdateController,
        )

        self.base_updates = SubscriptionBaseUpdateController(store, publications)

    async def execute(
        self, command: CommandEnvelope, work: UnitOfWork
    ) -> SubscriptionRemoteRemediationDecision:
        await _fence_command(command, work)
        approved = await self._approved.load(work, command.run_id)
        replay = await self.replay(command, work, approved)
        if replay is not None:
            return replay
        if (
            approved.run.state is not RunState.REMEDIATING
            or approved.run.pending_gate is not None
            or approved.run.version != command.expected_run_version
            or approved.run.remote_remediation_count != _command(command)
            or command.actor_id != approved.approval_actor_id
        ):
            raise CommandRecoveryRequired("subscription remote repair is not current")
        source, evidence, feedback = await self._source(command, work, approved)
        proposal = await self._current_source(work, source)
        await self._candidate(proposal, evidence)
        await _fence_command(command, work)
        current = await self._approved.load(work, command.run_id)
        latest = await self._current_source(work, source)
        if (
            current != approved
            or latest != proposal
            or await self._source(command, work, current) != (source, evidence, feedback)
            or await pending_current_control_stop(work, current.run)
        ):
            raise CommandRecoveryRequired("subscription remote repair authority changed")
        await self._candidate(latest, evidence)
        await _fence_command(command, work)
        repair = await work.subscription_decisions.reopen_acceptance_remote(
            proposal, command.id, canonical_digest(evidence), feedback
        )
        values = _payload(command, approved, source, evidence, repair)
        await _fence_command(command, work)
        if repair.repaired:
            run = approved.run
            await work.events.append(
                RunEvent(
                    run_id=run.id,
                    run_version=run.version,
                    event_type=EVENT,
                    payload=values,
                    actor_class="worker",
                    actor_id=command.actor_id,
                )
            )
        else:
            run = await work.runs.intervene(
                command.run_id,
                approved.run.version,
                EVENT,
                values,
                actor_class="worker",
                actor_id=command.actor_id,
            )
        await _fence_command(command, work)
        await work.commit()
        return SubscriptionRemoteRemediationDecision(run.id, run.state, run.version)

    async def replay(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        approved: ApprovedPlan,
        *,
        allow_invalidated_approval: bool = False,
    ) -> SubscriptionRemoteRemediationDecision | None:
        events = [
            event
            for event in await work.events.list_after(command.run_id, 0)
            if event.event_type == EVENT
            and event.payload.get("source_command_id") == str(command.id)
        ]
        if not events:
            return None
        if len(events) != 1:
            raise CommandRecoveryRequired("subscription remote repair is ambiguous")
        event = events[0]
        historical = replace(
            approved, run=replace(approved.run, remote_remediation_count=_command(command))
        )
        source, evidence, feedback = await self._source(
            command,
            work,
            historical,
            historical=True,
            allow_invalidated_approval=allow_invalidated_approval,
        )
        repair = AcceptanceValidationRepair.from_payload(event.payload.get("repair"))
        if (
            command.actor_id != approved.approval_actor_id
            or approved.run.remote_remediation_count < _command(command)
            or event.actor_class != "worker"
            or event.actor_id != command.actor_id
            or event.payload_schema_version != 1
            or event.run_version != command.expected_run_version + int(not repair.repaired)
            or event.run_version > approved.run.version
            or payload_digest(event.payload)
            != payload_digest(_payload(command, approved, source, evidence, repair))
        ):
            raise CommandRecoveryRequired("subscription remote repair decision differs")
        await work.subscription_decisions.verify_acceptance_remote(
            source, command.id, canonical_digest(evidence), feedback, repair
        )
        return SubscriptionRemoteRemediationDecision(
            command.run_id,
            RunState.REMEDIATING if repair.repaired else RunState.AWAITING_HUMAN_INTERVENTION,
            event.run_version,
        )

    async def _source(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        approved: ApprovedPlan,
        *,
        historical: bool = False,
        allow_invalidated_approval: bool = False,
    ) -> tuple[RetainedSubscriptionAcceptance, SubscriptionPrApprovalEvidence, UntrustedContent]:
        origin = await _origin(work, command)
        feedback = await remote_evidence(
            work,
            approved,
            origin,
            self._store,
            historical_publication=True,
            allow_invalidated_approval=allow_invalidated_approval,
        )
        record = await remote_publication_record(work, origin)
        if not historical and record != await work.releases.get_for_run(command.run_id):
            raise CommandRecoveryRequired("subscription remote publication changed")
        publication = await work.operations.get(record.publication_intent_id)
        approval_id = UUID(str(publication.request_payload.get("approval_id")))
        if record.candidate_evidence_digest is None:
            frozen = await self._publications.for_recovery(work, command.run_id, approval_id)
        else:
            frozen, _ = await self._publications.for_reviewed_recovery(
                work, command.run_id, approval_id, record.candidate_evidence_digest
            )
        evidence = frozen.evidence
        if (
            not isinstance(evidence, SubscriptionPrApprovalEvidence)
            or evidence.candidate_commit != record.pull_request.head_sha
            or evidence.base_sha != approved.evidence.base_sha
            or _command(command) > evidence.remote_remediation_limit
        ):
            raise CommandRecoveryRequired("subscription remote publication source differs")
        manifest = decode_evidence_manifest(
            await self._store.open_bytes(evidence.acceptance_digest)
        )
        if not isinstance(manifest, SubscriptionAcceptanceEvidenceManifest):
            raise CommandRecoveryRequired("subscription remote acceptance kind differs")
        source = await work.subscription_decisions.retained_acceptance_source(
            manifest.producer_attempt_id
        )
        return source, evidence, feedback

    async def push_payload(
        self,
        work: UnitOfWork,
        approved: ApprovedPlan,
        frozen: FrozenSubscriptionPublication,
        *,
        allow_invalidated_approval: bool = False,
    ) -> dict[str, object]:
        command = await work.commands.get_by_idempotency_key(
            f"{approved.run.id}:remote-remediation:{approved.run.remote_remediation_count}"
        )
        if command is None:
            raise CommandRecoveryRequired("subscription remote repair is incomplete")
        command = await resumed_successor(
            work, await work.events.list_after(approved.run.id, 0), command
        )
        if command.command_type == "update_base":
            return await self.base_updates.push_payload(
                command,
                work,
                approved,
                frozen,
                allow_invalidated_approval=allow_invalidated_approval,
            )
        decision = await self.replay(
            command, work, approved, allow_invalidated_approval=allow_invalidated_approval
        )
        if decision is None or decision.state is not RunState.REMEDIATING:
            raise CommandRecoveryRequired("subscription remote repair decision is absent")
        await require_acknowledged_delivery(work, command)
        events = [
            event
            for event in await work.events.list_after(approved.run.id, 0)
            if event.event_type == EVENT
            and event.payload.get("source_command_id") == str(command.id)
        ]
        event = events[0]
        source = await work.subscription_decisions.retained_acceptance_source(
            UUID(str(event.payload.get("acceptance_attempt_id")))
        )
        receipt = AcceptanceValidationRepair.from_payload(event.payload.get("repair"))
        snapshot = await remote_publication_record(work, await _origin(work, command))
        if (
            frozen.source.attempt_id == source.attempt_id
            or frozen.source.decision.task_id != source.decision.task_id
            or frozen.source.review.candidate_epoch <= receipt.candidate_epoch
            or frozen.evidence.base_sha != source.worktree.base_sha
            or decision.version >= approved.run.version
        ):
            raise CommandRecoveryRequired("subscription remote repair needs fresh acceptance")
        publication = await work.operations.get(snapshot.publication_intent_id)
        payload: dict[str, object] = {
            "pull_request_id": str(snapshot.id),
            "node_id": snapshot.pull_request.node_id,
            "approval_id": str(publication.request_payload["approval_id"]),
            "candidate_evidence_digest": frozen.digest,
            "previous_head_sha": snapshot.pull_request.head_sha,
            "remote_attempt": _command(command),
        }
        if snapshot.base_update_intent_id is not None:
            payload |= {
                "base_update_intent_id": str(snapshot.base_update_intent_id),
                "base_adoption_intent_id": str(snapshot.base_adoption_intent_id),
            }
        return payload

    async def verify_unadmitted(
        self, command: CommandEnvelope, work: UnitOfWork, approved: ApprovedPlan
    ) -> None:
        """A missing receipt permits retry only while its original candidate is untouched."""
        source, _, _ = await self._source(command, work, approved)
        await self._current_source(work, source, allow_paused=True)

    async def _current_source(
        self,
        work: UnitOfWork,
        retained: RetainedSubscriptionAcceptance,
        *,
        allow_paused: bool = False,
    ) -> PreparedSubscriptionAcceptance:
        source, proof = await work.subscription_decisions.acceptance_remote_source(
            retained.attempt_id, allow_paused=allow_paused
        )
        if proof != retained.receipts or any(
            getattr(source, field) != getattr(retained, field)
            for field in ("attempt_id", "decision", "result_digest", "review", "policy", "worktree")
        ):
            raise CommandRecoveryRequired("subscription remote acceptance source differs")
        return source

    async def _candidate(
        self, source: PreparedSubscriptionAcceptance, evidence: SubscriptionPrApprovalEvidence
    ) -> None:
        def capture() -> None:
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
                or hashlib.sha256(candidate.diff.text.encode()).hexdigest() != evidence.diff_digest
            ):
                raise CommandRecoveryRequired("subscription remote repair candidate differs")

        await asyncio.to_thread(capture)
