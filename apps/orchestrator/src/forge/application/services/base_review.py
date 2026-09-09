"""Read-only provenance for fresh review of a remotely updated, locally adopted base."""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from uuid import UUID

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.approved_plan import ApprovedPlan
from forge.application.services.resume_successor import resumed_successor
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.operation import OperationStatus, canonical_digest


@dataclass(frozen=True)
class BaseReview:
    update_id: UUID
    adoption_id: UUID
    validation_id: UUID
    review_command_id: UUID
    review_version: int
    poll: int
    remote_attempt: int
    head_sha: str

    @property
    def binding(self) -> dict[str, object]:
        return {
            "base_update_intent_id": str(self.update_id),
            "base_adoption_intent_id": str(self.adoption_id),
        }


async def base_review(
    work: UnitOfWork, approved: ApprovedPlan, *, current_attempt: bool = False
) -> BaseReview | None:
    def invalid() -> CommandRecoveryRequired:
        return CommandRecoveryRequired("base review provenance differs")

    run = approved.run
    record = await work.releases.get_for_run(run.id)
    if record is None or record.base_update_intent_id is None:
        return None
    if current_attempt:
        current = await work.commands.get_by_idempotency_key(
            f"{run.id}:remote-remediation:{run.remote_remediation_count}"
        )
        if current is not None and current.command_type == "remediate_remote":
            return None
    if record.base_adoption_intent_id is None:
        raise invalid()
    remote = await work.operations.get(record.base_update_intent_id)
    local = await work.operations.get(record.base_adoption_intent_id)
    for receipt, kind, prefix in (
        (remote, "update_branch", "update-branch"),
        (local, "adopt_base", "adopt-base"),
    ):
        if (
            receipt.run_id != run.id
            or receipt.kind != kind
            or receipt.status is not OperationStatus.SUCCEEDED
            or receipt.request_schema_version != 1
            or receipt.outcome_schema_version != 1
            or receipt.request_digest != canonical_digest(receipt.request_payload)
            or receipt.idempotency_key != f"{run.id}:{prefix}:{receipt.request_digest}"
            or receipt.remote_resource_id != record.pull_request.node_id
            or receipt.outcome != asdict(record.pull_request)
        ):
            raise invalid()
    request = remote.request_payload
    attempt = request.get("remote_attempt")
    expected = {
        "pull_request_id": str(record.id),
        "node_id": record.pull_request.node_id,
        "repository": record.pull_request.base_repository,
        "pull_request_number": record.pull_request.number,
        "head_sha": request.get("head_sha"),
        "previous_base_sha": request.get("previous_base_sha"),
        "base_sha": record.pull_request.base_sha,
        "base_ref": record.pull_request.base_ref,
        "policy_version": approved.policy.version,
        "observation_digest": request.get("observation_digest"),
        "remote_attempt": attempt,
    }
    if (
        request != expected
        or type(attempt) is not int
        or not 1 <= attempt <= run.remote_remediation_count
        or local.request_payload
        != dict(request)
        | {
            "update_intent_id": str(remote.id),
            "head_sha": record.pull_request.head_sha,
            "previous_head_sha": request.get("head_sha"),
        }
    ):
        raise invalid()
    command = await work.commands.get_by_idempotency_key(f"{run.id}:remote-remediation:{attempt}")
    if (
        command is None
        or command.command_type != "update_base"
        or command.run_id != run.id
        or command.actor_id != approved.approval_actor_id
        or command.payload_schema_version != 1
        or command.payload
        != {
            "pull_request_id": str(record.id),
            "observation_digest": request["observation_digest"],
            "remote_attempt": attempt,
            "target_base_sha": record.pull_request.base_sha,
        }
    ):
        raise invalid()
    origin = command
    events = await work.events.list_after(run.id, 0)
    settled = [
        e
        for e in events
        if e.event_type == "run.base_updated"
        and e.payload.get("update_intent_id") == str(remote.id)
        and e.payload.get("adoption_intent_id") == str(local.id)
    ]
    parents = [
        e
        for e in events
        if e.event_type == "run.pr_observed"
        and e.payload.get("remediation_command_id") == str(command.id)
    ]
    if len(settled) != 1 or len(parents) != 1:
        raise invalid()
    event, parent = settled[0], parents[0]
    from forge.application.services.release_resume import resumed_release_origin

    try:
        command = await work.commands.get(UUID(str(event.payload.get("source_command_id"))))
    except ValueError:
        raise invalid() from None
    if (
        command.status is not CommandStatus.COMPLETED
        or command.run_id != run.id
        or await resumed_release_origin(work, command) != origin
    ):
        raise invalid()
    if (
        event.run_version != command.expected_run_version + 1
        or event.actor_class != "worker"
        or event.actor_id != approved.approval_actor_id
        or event.payload.get("update_intent_id") != str(remote.id)
        or event.payload.get("adoption_intent_id") != str(local.id)
        or event.payload.get("pull_request_id") != str(record.id)
        or event.payload.get("head_sha") != record.pull_request.head_sha
        or event.payload.get("base_sha") != record.pull_request.base_sha
        or parent.run_version != origin.expected_run_version
        or parent.payload.get("remediation_payload") != origin.payload
        or parent.payload.get("reason") != "base_advanced"
        or type(parent.payload.get("poll")) is not int
    ):
        raise invalid()
    validation = await work.commands.get_by_idempotency_key(
        str(event.payload.get("validation_key"))
    )
    if (
        validation is None
        or validation.command_type != "validate"
        or validation.run_id != run.id
        or validation.expected_run_version != event.run_version
        or validation.actor_id != approved.approval_actor_id
        or event.payload.get("validation_command_id") != str(validation.id)
    ):
        raise invalid()
    evidence_id, review = await _review_after_validation(work, approved, events, validation)
    poll = parent.payload["poll"]
    assert isinstance(poll, int)
    return BaseReview(
        remote.id,
        local.id,
        evidence_id,
        review.id,
        review.expected_run_version,
        poll,
        attempt,
        record.pull_request.head_sha,
    )


async def _review_after_validation(
    work: UnitOfWork,
    approved: ApprovedPlan,
    events: Sequence[RunEvent],
    validation: CommandEnvelope,
) -> tuple[UUID, CommandEnvelope]:
    """Follow bounded, completed local repairs; never select an unrelated latest review."""

    def invalid() -> CommandRecoveryRequired:
        return CommandRecoveryRequired("base review local repair lineage differs")

    async def decision_for(command: CommandEnvelope, kind: str) -> RunEvent:
        matches = [
            e
            for e in events
            if e.event_type == kind and e.payload.get("source_command_id") == str(command.id)
        ]
        if len(matches) != 1:
            raise invalid()
        event = matches[0]
        if (
            event.run_version != command.expected_run_version + 1
            or event.actor_class != "worker"
            or event.actor_id is not None
            or event.payload.get("approval_id") != str(approved.approval_id)
        ):
            raise invalid()
        return event

    async def delivery(event: RunEvent, kind: str) -> CommandEnvelope:
        try:
            command = await work.commands.get(UUID(str(event.payload["queued_command_id"])))
        except KeyError, ValueError:
            raise invalid() from None
        payload = event.payload.get("queued_payload")
        if event.event_type == "run.review_decided" and kind == "remediate":
            payload = {
                "semantic_attempt": event.payload.get("semantic_attempt"),
                "automatic": True,
                "validation_evidence_set_id": event.payload.get("validation_evidence_set_id"),
                "prior_review_evidence_set_id": event.payload.get("review_evidence_set_id"),
            }
        if (
            command.run_id != approved.run.id
            or command.command_type != kind
            or command.expected_run_version != event.run_version
            or command.actor_id != approved.approval_actor_id
            or command.payload_schema_version != 1
            or command.idempotency_key != event.payload.get("queued_key")
            or command.payload != payload
        ):
            raise invalid()
        return command

    for _ in range(approved.policy.local_remediation_limit + 1):
        validation = await resumed_successor(work, events, validation)
        if validation.status is not CommandStatus.COMPLETED:
            raise invalid()
        decision = await decision_for(validation, "run.validation_decided")
        try:
            evidence_id = UUID(str(decision.payload["validation_evidence_set_id"]))
        except KeyError, ValueError:
            raise invalid() from None
        if decision.payload.get("target") == "REVIEWING":
            review = await resumed_successor(work, events, await delivery(decision, "review"))
            if review.payload.get("validation_evidence_set_id") != str(evidence_id):
                raise invalid()
            completed = [
                e
                for e in events
                if e.event_type == "run.review_decided"
                and e.payload.get("source_command_id") == str(review.id)
            ]
            if not completed or (
                len(completed) == 1 and completed[0].payload.get("target") == "MONITORING_PR"
            ):
                return evidence_id, review
            if review.status is not CommandStatus.COMPLETED:
                raise invalid()
            decision = await decision_for(review, "run.review_decided")
            if decision.payload.get("validation_evidence_set_id") != str(evidence_id):
                raise invalid()
        if decision.payload.get("target") != "REMEDIATING":
            raise invalid()
        repair = await resumed_successor(work, events, await delivery(decision, "remediate"))
        if repair.status is not CommandStatus.COMPLETED:
            raise invalid()
        settled = [
            e
            for e in events
            if e.event_type == "run.remediation_completed"
            and e.payload.get("command_id") == str(repair.id)
        ]
        if len(settled) != 1:
            raise invalid()
        event = settled[0]
        if (
            event.run_version != repair.expected_run_version + 1
            or event.actor_class != "worker"
            or event.actor_id is not None
            or event.payload.get("command_payload") != repair.payload
            or event.payload.get("approval_id") != str(approved.approval_id)
        ):
            raise invalid()
        try:
            validation = await work.commands.get(UUID(str(event.payload["queued_command_id"])))
        except KeyError, ValueError:
            raise invalid() from None
        if (
            validation.run_id != approved.run.id
            or validation.command_type != "validate"
            or validation.expected_run_version != event.run_version
            or validation.actor_id != approved.approval_actor_id
            or validation.payload_schema_version != 1
        ):
            raise invalid()
    raise invalid()

