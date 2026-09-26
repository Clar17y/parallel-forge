"""Read-only authority for developer work admitted by a remote observation."""

import hashlib
import json
from dataclasses import asdict, replace
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.release import ReleaseRecord
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.approved_plan import ApprovedPlan
from forge.application.services.base_review import base_review
from forge.application.services.resume_successor import resumed_successor
from forge.domain.agent import UntrustedContent, UntrustedSourceKind
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.github import CheckSnapshot, MergeProtection, ReviewSnapshot
from forge.domain.operation import OperationStatus, canonical_digest
from forge.persistence.models import Approval
from forge.release.monitor import assess_checks


async def reviewed_push_payload(
    work: UnitOfWork, approved: ApprovedPlan, store: ArtifactStore, evidence_digest: str,
    *, allow_invalidated_approval: bool = False,
) -> dict[str, object] | None:
    record = await work.releases.get_for_run(approved.run.id)
    if record is None:
        return None
    command = await work.commands.get_by_idempotency_key(
        f"{approved.run.id}:remote-remediation:{approved.run.remote_remediation_count}"
    )
    if command is not None and command.command_type == "update_base":
        adopted = await base_review(work, approved, current_attempt=True)
        if adopted is None or adopted.remote_attempt != approved.run.remote_remediation_count:
            raise CommandRecoveryRequired("reviewed base repair lineage differs")
        publication = await work.operations.get(record.publication_intent_id)
        return {
            "pull_request_id": str(record.id),
            "node_id": record.pull_request.node_id,
            "approval_id": str(publication.request_payload["approval_id"]),
            "candidate_evidence_digest": evidence_digest,
            "previous_head_sha": record.pull_request.head_sha,
            "remote_attempt": approved.run.remote_remediation_count,
        }
    if (
        command is None
        or command.command_type != "remediate_remote"
    ):
        raise CommandRecoveryRequired("reviewed PR has no completed remote remediation")
    await remote_evidence(
        work, approved, command, store, allow_invalidated_approval=allow_invalidated_approval
    )
    events = await work.events.list_after(approved.run.id, 0)
    command = await resumed_successor(work, events, command)
    if command.status is not CommandStatus.COMPLETED:
        raise CommandRecoveryRequired("reviewed PR has no completed remote remediation")
    completed = [
        event
        for event in await work.events.list_after(approved.run.id, 0)
        if event.event_type == "run.remediation_completed"
        and event.payload.get("command_id") == str(command.id)
        and event.payload.get("command_payload") == command.payload
        and event.run_version == command.expected_run_version + 1
        and event.run_version < approved.run.version
        and event.actor_class == "worker"
        and event.actor_id is None
    ]
    if len(completed) != 1:
        raise CommandRecoveryRequired("reviewed PR remediation completion differs")
    publication = await work.operations.get(record.publication_intent_id)
    return {
        "pull_request_id": str(record.id),
        "node_id": record.pull_request.node_id,
        "approval_id": str(publication.request_payload["approval_id"]),
        "candidate_evidence_digest": evidence_digest,
        "previous_head_sha": record.pull_request.head_sha,
        "remote_attempt": approved.run.remote_remediation_count,
    }


async def remote_evidence(
    work: UnitOfWork, approved: ApprovedPlan, command: CommandEnvelope, store: ArtifactStore,
    *, allow_invalidated_approval: bool = False,
    historical_publication: bool = False,
) -> UntrustedContent:
    # Only historical reconciliation opts in; live developer/push admission keeps the default.
    def invalid() -> CommandRecoveryRequired:
        return CommandRecoveryRequired("remote remediation authority differs")

    events = [
        event
        for event in await work.events.list_after(command.run_id, 0)
        if event.event_type == "run.pr_observed"
        and event.payload.get("remediation_command_id") == str(command.id)
    ]
    record = await work.releases.get_for_run(command.run_id)
    if historical_publication:
        record = await remote_publication_record(work, command)
    count = approved.run.remote_remediation_count
    if (
        len(events) != 1
        or record is None
        or command.actor_id != approved.approval_actor_id
        or command.payload.get("pull_request_id") != str(record.id)
        or command.payload.get("remote_attempt") != count
        or count < 1
        or count > approved.policy.remote_remediation_limit
    ):
        raise invalid()
    event = events[0]
    if (
        event.run_version != command.expected_run_version
        or event.actor_class != "worker"
        or event.payload.get("remediation_payload") != command.payload
        or event.payload.get("remediation_key") != command.idempotency_key
        or event.payload.get("disposition") != "remediate"
        or event.payload.get("target") != "REMEDIATING"
        or event.payload.get("observation_digest") != command.payload.get("observation_digest")
    ):
        raise invalid()
    source = await work.commands.get(UUID(str(event.payload.get("source_command_id"))))
    from forge.application.services.monitor_resume import monitor_inputs, monitor_origin

    await monitor_origin(work, source)
    if (
        source.run_id != command.run_id
        or source.command_type != "monitor_pr"
        or source.status is not CommandStatus.COMPLETED
        or source.expected_run_version + 1 != command.expected_run_version
        or source.actor_id != event.actor_id
        or monitor_inputs(source) != (record.id, event.payload.get("poll"))
    ):
        raise invalid()
    publication = await work.operations.get(record.publication_intent_id)
    if (
        publication.run_id != command.run_id
        or publication.kind != "create_pr"
        or publication.status is not OperationStatus.SUCCEEDED
        or publication.remote_resource_id != record.pull_request.node_id
    ):
        raise invalid()
    approval = await work.auth.get_approval(
        approval_id=UUID(str(publication.request_payload.get("approval_id"))), for_update=True
    )
    if (
        not isinstance(approval, Approval)
        or approval.run_id != command.run_id
        or approval.gate != "pr"
        or (approval.invalidated_at is not None and not allow_invalidated_approval)
        or approval.policy_version != approved.policy.version
        or approval.evidence_digest != publication.request_payload.get("approval_digest")
    ):
        raise invalid()
    digest = str(command.payload.get("observation_digest"))
    descriptor = await work.artifacts.get_by_digest(digest, run_id=command.run_id)
    wire = await store.open_bytes(digest, max_bytes=1048576)
    if (
        descriptor.producer_type != "remote_pr_observation"
        or descriptor.producer_id != record.id
        or descriptor.media_type != "application/json"
        or descriptor.truncated
        or descriptor.byte_count != len(wire)
        or hashlib.sha256(wire).hexdigest() != digest
    ):
        raise invalid()
    try:
        value = json.loads(wire)
        checks = tuple(CheckSnapshot(**item) for item in value["checks"])
        reviews = tuple(ReviewSnapshot(**item) for item in value["reviews"])
        protection = MergeProtection(**value["protection"])
        if (
            value["pull_request_id"] != str(record.id)
            or value["pull_request"] != asdict(record.pull_request)
            or assess_checks(record.pull_request.head_sha, checks, reviews, protection).disposition
            != "remediate"
        ):
            raise invalid()
    except ValueError, TypeError, KeyError:
        raise invalid() from None
    return UntrustedContent.from_text(
        wire.decode("utf-8"), source_kind=UntrustedSourceKind.CHECK, source_reference=digest
    )


async def remote_publication_record(
    work: UnitOfWork, command: CommandEnvelope
) -> ReleaseRecord:
    """Reconstruct the actual published snapshot preceding this observation.

    Current release rows may advance after a repair. Historical verification uses
    the succeeded effect and its causal settlement, never a caller-supplied head.
    """
    def invalid() -> CommandRecoveryRequired:
        return CommandRecoveryRequired("remote observation publication source differs")

    record = await work.releases.get_for_run(command.run_id)
    if record is None:
        raise invalid()
    events = [
        event for event in await work.events.list_after(command.run_id, 0)
        if event.event_type in {"run.pr_published", "run.pr_updated"}
        and event.run_version < command.expected_run_version
    ]
    if not events:
        raise invalid()
    latest = max(event.run_version for event in events)
    events = [event for event in events if event.run_version == latest]
    if len(events) != 1:
        raise invalid()
    event = events[0]
    updated = event.event_type == "run.pr_updated"
    try:
        source = await work.commands.get(UUID(str(event.payload["source_command_id"])))
        intent = await work.operations.get(UUID(str(event.payload[
            "push_intent_id" if updated else "publication_intent_id"
        ])))
        head, base = intent.request_payload.get("head_sha"), intent.request_payload.get("base_sha")
        if (
            not isinstance(head, str) or not isinstance(base, str)
            or any(len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha)
                   for sha in (head, base))
        ):
            raise invalid()
        pull = replace(
            record.pull_request, head_sha=head, base_sha=base,
            state="open", merged=False, merge_sha=None,
        )
        if (
            event.payload_schema_version != 1
            or event.actor_class != "worker"
            or event.actor_id != source.actor_id
            or event.payload.get("pull_request_id") != str(record.id)
            or source.run_id != command.run_id
            or source.command_type != ("push_reviewed_pr" if updated else "publish_pr")
            or source.status is not CommandStatus.COMPLETED
            or source.expected_run_version + int(not updated) != event.run_version
            or intent.run_id != command.run_id
            or intent.status is not OperationStatus.SUCCEEDED
            or intent.kind != ("push_branch" if updated else "create_pr")
            or intent.request_schema_version != 1
            or intent.request_digest != canonical_digest(intent.request_payload)
            or intent.outcome != asdict(pull)
            or intent.remote_resource_id != record.pull_request.node_id
            or pull.node_id != record.pull_request.node_id
            or pull.head_repository != record.pull_request.head_repository
            or pull.base_repository != record.pull_request.base_repository
            or pull.head_ref != record.pull_request.head_ref
            or pull.base_ref != record.pull_request.base_ref
            or pull.number != record.pull_request.number
            or pull.head_sha != intent.request_payload.get("head_sha")
            or pull.base_sha != intent.request_payload.get("base_sha")
            or source.payload.get("approval_id") != intent.request_payload.get("approval_id")
            or (
                updated and (
                    intent.request_payload.get("pull_request_id") != str(record.id)
                    or event.payload.get("candidate_evidence_digest")
                    != intent.request_payload.get("candidate_evidence_digest")
                    or source.payload.get("candidate_evidence_digest")
                    != intent.request_payload.get("candidate_evidence_digest")
                    or source.payload.get("previous_head_sha")
                    != intent.request_payload.get("previous_head_sha")
                )
            )
            or (not updated and intent.id != record.publication_intent_id)
        ):
            raise invalid()
        return replace(
            record,
            pull_request=pull,
            reviewed_push_intent_id=intent.id if updated else None,
            candidate_evidence_digest=(
                str(intent.request_payload["candidate_evidence_digest"]) if updated else None
            ),
            base_update_intent_id=(
                UUID(str(intent.request_payload["base_update_intent_id"]))
                if intent.request_payload.get("base_update_intent_id") else None
            ),
            base_adoption_intent_id=(
                UUID(str(intent.request_payload["base_adoption_intent_id"]))
                if intent.request_payload.get("base_adoption_intent_id") else None
            ),
        )
    except KeyError, ValueError, TypeError:
        raise invalid() from None
