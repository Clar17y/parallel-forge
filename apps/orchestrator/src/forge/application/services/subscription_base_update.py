"""Bind controlled base adoption to the existing primary and fresh acceptance."""

from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.release import ReleaseRecord
from forge.application.ports.subscription_acceptance import (
    PreparedSubscriptionAcceptance,
    RetainedSubscriptionAcceptance,
)
from forge.application.ports.subscription_base_update import BaseUpdateReservation
from forge.application.ports.subscription_validation import AcceptanceValidationRepair
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.approved_plan import ApprovedPlan, ApprovedPlanLoader
from forge.application.services.base_update_authority import base_update_origin
from forge.application.services.pr_evidence import PrEvidenceValidator
from forge.application.services.release_resume import resumed_release_origin
from forge.application.services.remote_remediation import remote_publication_record
from forge.application.services.subscription_delivery_ack import require_acknowledged_delivery
from forge.domain.approval import SubscriptionPrApprovalEvidence, canonical_digest
from forge.domain.command import CommandEnvelope
from forge.domain.event import RunEvent
from forge.domain.evidence import SubscriptionAcceptanceEvidenceManifest, decode_evidence_manifest
from forge.domain.operation import OperationIntent, OperationStatus
from forge.domain.operation import canonical_digest as payload_digest
from forge.domain.release import GitHubPullRequest
from forge.domain.run import RunState
from forge.persistence.models import Approval

if TYPE_CHECKING:
    from forge.application.services.subscription_publication_evidence import (
        FrozenSubscriptionPublication,
    )

ADMISSION_EVENT = "run.subscription_base_update_admitted"
EVENT = "run.subscription_base_adopted"


@dataclass(frozen=True, slots=True)
class _Source:
    origin: CommandEnvelope
    record: ReleaseRecord
    acceptance: RetainedSubscriptionAcceptance
    evidence: SubscriptionPrApprovalEvidence


@dataclass(frozen=True, slots=True)
class SubscriptionBaseAdoption:
    version: int
    record: ReleaseRecord
    acceptance: RetainedSubscriptionAcceptance
    repair: AcceptanceValidationRepair


def _invalid() -> CommandRecoveryRequired:
    return CommandRecoveryRequired("subscription base update authority differs")


def _admission_payload(
    command: CommandEnvelope, source: _Source, reservation: BaseUpdateReservation
) -> dict[str, object]:
    return {
        "origin_command_id": str(source.origin.id),
        "admission_command_id": str(command.id),
        "admission_payload_digest": payload_digest(command.payload),
        "admission_expected_version": command.expected_run_version,
        "acceptance_attempt_id": str(source.acceptance.attempt_id),
        "acceptance_digest": source.evidence.acceptance_digest,
        "pr_evidence_digest": canonical_digest(source.evidence),
        "reservation": reservation.payload(),
    }


def _adopted_payload(
    command: CommandEnvelope,
    source: _Source,
    reservation: BaseUpdateReservation,
    record: ReleaseRecord,
    repair: AcceptanceValidationRepair,
) -> dict[str, object]:
    return {
        "source_command_id": str(command.id),
        "source_payload_digest": payload_digest(command.payload),
        "source_expected_version": command.expected_run_version,
        "origin_command_id": str(source.origin.id),
        "acceptance_attempt_id": str(source.acceptance.attempt_id),
        "pr_evidence_digest": canonical_digest(source.evidence),
        "pull_request_id": str(record.id),
        "update_intent_id": str(record.base_update_intent_id),
        "adoption_intent_id": str(record.base_adoption_intent_id),
        "head_sha": record.pull_request.head_sha,
        "base_sha": record.pull_request.base_sha,
        "reservation": reservation.payload(),
        "repair": repair.payload(),
        "target": RunState.REMEDIATING.value,
    }


class SubscriptionBaseUpdateController:
    def __init__(self, store: ArtifactStore, publications: PrEvidenceValidator) -> None:
        self._store, self._publications = store, publications

    async def _source(
        self,
        work: UnitOfWork,
        command: CommandEnvelope,
        approved: ApprovedPlan,
        *,
        allow_invalidated_approval: bool = False,
    ) -> _Source:
        persisted = await work.commands.get(command.id)
        if any(
            getattr(persisted, key) != getattr(command, key)
            for key in (
                "run_id",
                "command_type",
                "idempotency_key",
                "payload",
                "payload_schema_version",
                "expected_run_version",
                "actor_id",
            )
        ):
            raise _invalid()
        origin = await resumed_release_origin(work, persisted)
        record = await remote_publication_record(work, origin)
        await base_update_origin(work, self._store, origin, approved, record)
        publication = await work.operations.get(record.publication_intent_id)
        approval_id = UUID(str(publication.request_payload.get("approval_id")))
        approval = await work.auth.get_approval(approval_id=approval_id, for_update=True)
        if (
            not isinstance(approval, Approval)
            or approval.run_id != command.run_id
            or approval.gate != "pr"
            or approval.policy_version != approved.policy.version
            or (approval.invalidated_at is not None and not allow_invalidated_approval)
            or approval.evidence_digest != publication.request_payload.get("approval_digest")
        ):
            raise _invalid()
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
        ):
            raise _invalid()
        manifest = decode_evidence_manifest(
            await self._store.open_bytes(evidence.acceptance_digest)
        )
        if not isinstance(manifest, SubscriptionAcceptanceEvidenceManifest):
            raise _invalid()
        source = await work.subscription_decisions.retained_acceptance_source(
            manifest.producer_attempt_id
        )
        return _Source(origin, record, source, evidence)

    async def _current(self, work: UnitOfWork, source: _Source) -> PreparedSubscriptionAcceptance:
        current, proof = await work.subscription_decisions.acceptance_remote_source(
            source.acceptance.attempt_id
        )
        if proof != source.acceptance.receipts or any(
            getattr(current, field) != getattr(source.acceptance, field)
            for field in ("attempt_id", "decision", "result_digest", "review", "policy", "worktree")
        ):
            raise _invalid()
        return current

    async def _admission(
        self, work: UnitOfWork, source: _Source, approved: ApprovedPlan
    ) -> BaseUpdateReservation | None:
        events = [
            event
            for event in await work.events.list_after(source.origin.run_id, 0)
            if event.event_type == ADMISSION_EVENT
            and event.payload.get("origin_command_id") == str(source.origin.id)
        ]
        if not events:
            return None
        if len(events) != 1:
            raise _invalid()
        event = events[0]
        command = await work.commands.get(UUID(str(event.payload.get("admission_command_id"))))
        reservation = BaseUpdateReservation.from_payload(event.payload.get("reservation"))
        if (
            await resumed_release_origin(work, command) != source.origin
            or event.actor_class != "worker"
            or event.actor_id != command.actor_id
            or event.payload_schema_version != 1
            or event.run_version != command.expected_run_version
            or event.run_version > approved.run.version
            or payload_digest(event.payload)
            != payload_digest(_admission_payload(command, source, reservation))
        ):
            raise _invalid()
        await work.subscription_decisions.verify_base_reservation(source.acceptance, reservation)
        return reservation

    async def prepare(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        approved: ApprovedPlan,
        *,
        prior_operation: OperationIntent | None,
    ) -> BaseUpdateReservation | None:
        source = await self._source(work, command, approved)
        if source.record != await work.releases.get_for_run(command.run_id):
            raise _invalid()
        current = await self._current(work, source)
        reservation = await self._admission(work, source, approved)
        if reservation is not None:
            await work.subscription_decisions.verify_base_reservation(
                current, reservation, live=True
            )
            return reservation
        if prior_operation is not None:
            raise _invalid()
        reservation = await work.subscription_decisions.reserve_acceptance_base(current)
        if reservation is not None:
            await work.events.append(
                RunEvent(
                    run_id=command.run_id,
                    run_version=command.expected_run_version,
                    event_type=ADMISSION_EVENT,
                    payload=_admission_payload(command, source, reservation),
                    actor_class="worker",
                    actor_id=command.actor_id,
                )
            )
        return reservation

    async def finish(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        approved: ApprovedPlan,
        updated: OperationIntent,
        adopted: OperationIntent,
    ) -> SubscriptionBaseAdoption:
        source = await self._source(work, command, approved)
        reservation = await self._admission(work, source, approved)
        if reservation is None:
            raise _invalid()
        record = await self._effects(work, source, updated.id, adopted.id)
        if record != await work.releases.get_for_run(command.run_id):
            raise _invalid()
        proposal = await self._current(work, source)
        repair = await work.subscription_decisions.reopen_acceptance_base(
            proposal,
            command.id,
            canonical_digest(source.evidence),
            record.pull_request.base_sha,
            updated.id,
            adopted.id,
            reservation,
        )
        await work.events.append(
            RunEvent(
                run_id=command.run_id,
                run_version=command.expected_run_version,
                event_type=EVENT,
                payload=_adopted_payload(command, source, reservation, record, repair),
                actor_class="worker",
                actor_id=command.actor_id,
            )
        )
        return SubscriptionBaseAdoption(
            command.expected_run_version, record, source.acceptance, repair
        )

    async def replay(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        approved: ApprovedPlan | None = None,
        *,
        allow_invalidated_approval: bool = False,
    ) -> SubscriptionBaseAdoption | None:
        events = [
            event
            for event in await work.events.list_after(command.run_id, 0)
            if event.event_type == EVENT
            and event.payload.get("source_command_id") == str(command.id)
        ]
        if not events:
            return None
        if len(events) != 1:
            raise _invalid()
        approved = approved or await ApprovedPlanLoader(self._store).load(work, command.run_id)
        source = await self._source(
            work, command, approved, allow_invalidated_approval=allow_invalidated_approval
        )
        reservation = await self._admission(work, source, approved)
        event = events[0]
        record = await self._effects(
            work,
            source,
            UUID(str(event.payload.get("update_intent_id"))),
            UUID(str(event.payload.get("adoption_intent_id"))),
        )
        repair = AcceptanceValidationRepair.from_payload(event.payload.get("repair"))
        if (
            reservation is None
            or event.actor_class != "worker"
            or event.actor_id != command.actor_id
            or event.payload_schema_version != 1
            or event.run_version != command.expected_run_version
            or event.run_version > approved.run.version
            or payload_digest(event.payload)
            != payload_digest(_adopted_payload(command, source, reservation, record, repair))
        ):
            raise _invalid()
        assert (
            record.base_update_intent_id is not None and record.base_adoption_intent_id is not None
        )
        await work.subscription_decisions.verify_acceptance_base(
            source.acceptance,
            command.id,
            canonical_digest(source.evidence),
            record.pull_request.base_sha,
            record.base_update_intent_id,
            record.base_adoption_intent_id,
            reservation,
            repair,
        )
        return SubscriptionBaseAdoption(event.run_version, record, source.acceptance, repair)

    async def push_payload(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        approved: ApprovedPlan,
        frozen: FrozenSubscriptionPublication,
        *,
        allow_invalidated_approval: bool = False,
    ) -> dict[str, object]:
        proof = await self.replay(
            command, work, approved, allow_invalidated_approval=allow_invalidated_approval
        )
        if (
            proof is None
            or proof.version >= approved.run.version
            or frozen.source.attempt_id == proof.acceptance.attempt_id
            or frozen.source.decision.task_id != proof.acceptance.decision.task_id
            or frozen.source.review.candidate_epoch <= proof.repair.candidate_epoch
            or frozen.evidence.base_sha != proof.acceptance.worktree.base_sha
        ):
            raise _invalid()
        await require_acknowledged_delivery(work, command)
        publication = await work.operations.get(proof.record.publication_intent_id)
        return {
            "pull_request_id": str(proof.record.id),
            "node_id": proof.record.pull_request.node_id,
            "approval_id": str(publication.request_payload["approval_id"]),
            "candidate_evidence_digest": frozen.digest,
            "previous_head_sha": proof.record.pull_request.head_sha,
            "remote_attempt": command.payload["remote_attempt"],
            "base_update_intent_id": str(proof.record.base_update_intent_id),
            "base_adoption_intent_id": str(proof.record.base_adoption_intent_id),
        }

    async def _effects(
        self, work: UnitOfWork, source: _Source, update_id: UUID, adoption_id: UUID
    ) -> ReleaseRecord:
        updated, adopted = (
            await work.operations.get(update_id),
            await work.operations.get(adoption_id),
        )
        pull = GitHubPullRequest(**dict(adopted.outcome or {}))  # type: ignore[arg-type]
        previous, origin = source.record.pull_request, source.origin
        target = origin.payload["target_base_sha"]
        request = {
            "pull_request_id": str(source.record.id),
            "node_id": previous.node_id,
            "repository": previous.base_repository,
            "pull_request_number": previous.number,
            "head_sha": previous.head_sha,
            "previous_base_sha": previous.base_sha,
            "base_sha": target,
            "base_ref": previous.base_ref,
            "policy_version": source.acceptance.policy.version,
            "observation_digest": origin.payload["observation_digest"],
            "remote_attempt": origin.payload["remote_attempt"],
        }
        local = dict(request) | {
            "update_intent_id": str(updated.id),
            "head_sha": pull.head_sha,
            "previous_head_sha": previous.head_sha,
        }
        if (
            not isinstance(target, str)
            or target == previous.base_sha
            or pull != replace(previous, head_sha=pull.head_sha, base_sha=target)
            or pull.head_sha == previous.head_sha
            or any(
                len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha)
                for sha in (pull.head_sha, pull.base_sha)
            )
            or pull.state != "open"
            or pull.merged
            or pull.merge_sha is not None
        ):
            raise _invalid()
        for operation, kind, prefix, expected in (
            (updated, "update_branch", "update-branch", request),
            (adopted, "adopt_base", "adopt-base", local),
        ):
            if (
                operation.run_id != origin.run_id
                or operation.kind != kind
                or operation.status is not OperationStatus.SUCCEEDED
                or operation.request_schema_version != 1
                or operation.outcome_schema_version != 1
                or payload_digest(operation.request_payload) != payload_digest(expected)
                or operation.request_digest != payload_digest(expected)
                or operation.idempotency_key
                != f"{origin.run_id}:{prefix}:{operation.request_digest}"
                or operation.remote_resource_id != previous.node_id
                or operation.outcome != asdict(pull)
            ):
                raise _invalid()
        return replace(
            source.record,
            pull_request=pull,
            base_update_intent_id=updated.id,
            base_adoption_intent_id=adopted.id,
            reviewed_push_intent_id=None,
            candidate_evidence_digest=None,
        )
