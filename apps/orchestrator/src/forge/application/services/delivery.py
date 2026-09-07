"""Advance local delivery using completed, current controller evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.evidence import EvidenceKind, EvidenceSetDescriptor
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.application.services.approved_plan import ApprovedPlan, ApprovedPlanLoader
from forge.application.services.validation import ValidationService, _fence_command
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.evidence import (
    EvidenceStatus,
    ValidationEvidenceManifest,
    decode_evidence_manifest,
)
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunState
from forge.domain.validation import command_spec_digest
from forge.persistence.repositories.commands import IdempotencyConflict


@dataclass(frozen=True, slots=True)
class DeliveryDecision:
    run_id: UUID
    state: RunState
    version: int
    validation_evidence_set_id: UUID


class DeliveryService:
    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        validation: ValidationService,
        git_factory: Callable[[ProjectPolicy], ControlledGitPort],
    ) -> None:
        self._store = artifact_store
        self._validation = validation
        self._git_factory = git_factory
        self._approved_plans = ApprovedPlanLoader(artifact_store)

    async def validate(self, command: CommandEnvelope, work: UnitOfWork) -> DeliveryDecision:
        """Run checks, then atomically choose review, remediation, or intervention."""
        await _fence_command(command, work)
        attempt = command.payload.get("semantic_attempt")
        if (
            command.command_type != "validate"
            or command.payload_schema_version != 1
            or type(attempt) is not int
            or attempt < 1
            or command.payload != {"semantic_attempt": attempt}
            or command.idempotency_key != f"{command.run_id}:validate:{attempt}"
        ):
            raise CommandRecoveryRequired("validation decision command is invalid")
        approved = await self._approved_plans.load(work, command.run_id)
        if command.actor_id != approved.approval_actor_id:
            raise CommandRecoveryRequired("validation decision actor differs")
        prior = [
            event
            for event in await work.events.list_after(command.run_id, 0)
            if event.event_type == "run.validation_decided"
            and event.payload.get("source_command_id") == str(command.id)
        ]
        step_id = uuid5(NAMESPACE_URL, f"forge:validate:{command.id}")
        evidence_id = uuid5(step_id, "validation-evidence")
        if prior:
            descriptor, manifest = await self._evidence(work, approved, evidence_id, step_id)
            event = prior[0]
            run = approved.run
            if (
                len(prior) != 1
                or event.actor_class != "worker"
                or event.actor_id is not None
                or event.run_version != run.version
                or run.version != command.expected_run_version + 1
                or event.payload.get("approval_id") != str(approved.approval_id)
                or event.payload.get("validation_evidence_set_id") != str(evidence_id)
                or event.payload.get("validation_digest") != descriptor.manifest_digest
                or event.payload.get("target") != run.state.value
                or event.payload.get("local_remediation_count") != run.local_remediation_count
            ):
                raise CommandRecoveryRequired("validation decision replay differs")
            passed = all(member.status is EvidenceStatus.PASSED for member in manifest.members)
            allowed = (
                {RunState.REVIEWING}
                if passed
                else {RunState.REMEDIATING, RunState.AWAITING_HUMAN_INTERVENTION}
            )
            if run.state not in allowed:
                raise CommandRecoveryRequired("validation decision state differs")
            queued_key = event.payload.get("queued_key")
            if run.state is RunState.AWAITING_HUMAN_INTERVENTION:
                if (
                    queued_key is not None
                    or run.local_remediation_count < approved.evidence.local_remediation_limit
                ):
                    raise CommandRecoveryRequired("validation intervention differs")
            else:
                if not isinstance(queued_key, str):
                    raise CommandRecoveryRequired("validation decision queue is missing")
                queued = await work.commands.get_by_idempotency_key(queued_key)
                kind = "review" if passed else "remediate"
                if queued is None:
                    raise CommandRecoveryRequired("validation decision queue is missing")
                next_attempt = queued.payload.get("semantic_attempt")
                if (
                    type(next_attempt) is not int
                    or next_attempt < 1
                    or queued.idempotency_key != f"{run.id}:{kind}:{next_attempt}"
                    or str(queued.id) != event.payload.get("queued_command_id")
                    or queued.payload != self._payload(next_attempt, descriptor, passed)
                    or queued.payload != event.payload.get("queued_payload")
                    or queued.command_type != kind
                    or queued.status is not CommandStatus.PENDING
                    or queued.expected_run_version != run.version
                    or queued.actor_id != approved.approval_actor_id
                    or queued.payload_schema_version != 1
                ):
                    raise CommandRecoveryRequired("validation decision queue differs")
            await work.commit()
            return DeliveryDecision(run.id, run.state, run.version, evidence_id)
        if (
            approved.run.state is not RunState.VALIDATING
            or approved.run.version != command.expected_run_version
        ):
            raise CommandRecoveryRequired("validation decision requires current validating run")
        await work.commit()
        produced = await self._validation.execute(command, work)
        await _fence_command(command, work)
        refreshed = await self._approved_plans.load(work, command.run_id)
        if refreshed.run != approved.run or refreshed.approval_id != approved.approval_id:
            raise CommandRecoveryRequired("validation decision authority changed")
        descriptor, manifest = await self._evidence(work, refreshed, evidence_id, step_id)
        if produced != descriptor:
            raise CommandRecoveryRequired("validation returned different evidence")
        passed = all(member.status is EvidenceStatus.PASSED for member in manifest.members)
        exhausted = (
            not passed
            and refreshed.run.local_remediation_count >= refreshed.evidence.local_remediation_limit
        )
        target = (
            RunState.REVIEWING
            if passed
            else RunState.AWAITING_HUMAN_INTERVENTION
            if exhausted
            else RunState.REMEDIATING
        )
        count = refreshed.run.local_remediation_count + (0 if passed or exhausted else 1)
        payload: dict[str, object] = {
            "source_command_id": str(command.id),
            "approval_id": str(refreshed.approval_id),
            "validation_evidence_set_id": str(evidence_id),
            "validation_digest": descriptor.manifest_digest,
            "target": target.value,
            "local_remediation_count": count,
            "queued_key": None,
        }
        if not exhausted:
            kind = "review" if passed else "remediate"
            next_attempt = await work.executions.next_attempt(
                command.run_id, "review" if passed else "implement"
            )
            queued_payload = self._payload(next_attempt, descriptor, passed)
            key = f"{command.run_id}:{kind}:{next_attempt}"
            try:
                queued = await work.commands.enqueue(
                    run_id=command.run_id,
                    command_type=kind,
                    idempotency_key=key,
                    payload=queued_payload,
                    expected_run_version=refreshed.run.version + 1,
                    actor_id=refreshed.approval_actor_id,
                )
            except IdempotencyConflict:
                raise CommandRecoveryRequired("next delivery command authority differs") from None
            if (
                queued.command_type != kind
                or queued.payload != queued_payload
                or queued.payload_schema_version != 1
                or queued.actor_id != refreshed.approval_actor_id
                or queued.expected_run_version != refreshed.run.version + 1
                or queued.status is not CommandStatus.PENDING
            ):
                raise CommandRecoveryRequired("next delivery command authority differs")
            payload.update(
                queued_key=key, queued_command_id=str(queued.id), queued_payload=queued_payload
            )
        await _fence_command(command, work)
        if passed:
            run = await work.runs.transition(
                command.run_id,
                refreshed.run.version,
                target,
                "run.validation_decided",
                payload,
                actor_class="worker",
            )
        else:
            run = await work.runs.begin_local_remediation(
                command.run_id,
                refreshed.run.version,
                automatic=True,
                limit=refreshed.evidence.local_remediation_limit,
                event_type="run.validation_decided",
                event_payload=payload,
                actor_class="worker",
            )
        await work.commit()
        return DeliveryDecision(run.id, run.state, run.version, evidence_id)

    @staticmethod
    def _payload(
        attempt: int, descriptor: EvidenceSetDescriptor, passed: bool
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "semantic_attempt": attempt,
            "validation_evidence_set_id": str(descriptor.evidence_set_id),
        }
        if descriptor.prior_review_evidence_set_id is not None:
            payload["prior_review_evidence_set_id"] = str(descriptor.prior_review_evidence_set_id)
        if not passed:
            payload["automatic"] = True
        return payload

    async def _evidence(
        self, work: UnitOfWork, approved: ApprovedPlan, evidence_id: UUID, step_id: UUID
    ) -> tuple[EvidenceSetDescriptor, ValidationEvidenceManifest]:
        descriptor = await work.evidence.get_by_id(evidence_id, run_id=approved.run.id)
        wire = await self._store.open_bytes(descriptor.manifest_digest)
        manifest = decode_evidence_manifest(wire)
        if (
            not isinstance(manifest, ValidationEvidenceManifest)
            or descriptor.kind is not EvidenceKind.VALIDATION
            or descriptor.step_id != step_id
            or descriptor.producer_execution_id is not None
            or descriptor.policy_version != approved.policy.version
            or descriptor.manifest_byte_count != len(wire)
            or descriptor.manifest_digest != hashlib.sha256(wire).hexdigest()
            or manifest.evidence_set_id != evidence_id
            or manifest.run_id != approved.run.id
            or manifest.step_id != step_id
            or manifest.policy_version != descriptor.policy_version
            or manifest.head_sha != descriptor.head_sha
            or {member.command_name: member.command_digest for member in manifest.members}
            != {spec.name: command_spec_digest(spec) for spec in approved.policy.required_checks}
            or len(manifest.members) != len(approved.policy.required_checks)
        ):
            raise CommandRecoveryRequired("validation decision evidence differs")
        run = approved.run
        if run.branch_name is None or run.worktree_path is None or run.base_sha is None:
            raise CommandRecoveryRequired("validation decision worktree is absent")
        worktree = ManagedWorktree(
            identity=WorktreeIdentity.for_run(
                run.project_id, run.id, run.branch_name, approved.policy.database.enabled
            ),
            path=Path(run.worktree_path),
            base_sha=run.base_sha,
        )
        git = self._git_factory(approved.policy)
        if (
            git.inspect_worktree(worktree.identity, worktree.base_sha) != worktree
            or git.head_sha(worktree) != descriptor.head_sha
            or not git.is_ancestor(worktree)
        ):
            raise CommandRecoveryRequired("validation decision HEAD differs")
        return descriptor, manifest


__all__ = ["DeliveryDecision", "DeliveryService"]
