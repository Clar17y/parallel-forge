"""Read-only proof of an atomically settled remote monitoring outcome."""

from collections.abc import Sequence
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.approval import ApprovalGate
from forge.domain.command import CommandEnvelope
from forge.domain.event import RunEvent
from forge.domain.run import RunState


async def verify_monitor_replay(
    store: ArtifactStore,
    command: CommandEnvelope,
    work: UnitOfWork,
    settled: Sequence[RunEvent],
    record_id: UUID,
    poll: int,
) -> None:
    if len(settled) != 1:
        raise CommandRecoveryRequired("PR monitoring replay is ambiguous")
    event = settled[0]
    if (
        event.actor_id != command.actor_id
        or event.actor_class != "worker"
        or event.payload.get("pull_request_id") != str(record_id)
        or event.payload.get("poll") != poll
    ):
        raise CommandRecoveryRequired("PR monitoring replay differs")
    digest = str(event.payload.get("observation_digest"))
    descriptor = await work.artifacts.get_by_digest(digest, run_id=command.run_id)
    if (
        descriptor.producer_id != record_id
        or descriptor.producer_type != "remote_pr_observation"
        or descriptor.truncated
        or not await store.verify(digest)
    ):
        raise CommandRecoveryRequired("PR monitoring replay evidence differs")
    disposition = event.payload.get("disposition")
    run = await work.runs.get_for_update(command.run_id)
    expected_version = command.expected_run_version + (disposition != "pending")
    if (
        event.run_version != expected_version
        or run.version < event.run_version
        or (run.version == event.run_version and run.state.value != event.payload.get("target"))
    ):
        raise CommandRecoveryRequired("PR monitoring replay outcome state differs")
    if disposition == "pending":
        queued = await work.commands.get_by_idempotency_key(
            f"{command.run_id}:monitor-pr:{poll + 1}"
        )
        if (
            queued is None
            or str(queued.id) != event.payload.get("monitor_command_id")
            or queued.command_type != "monitor_pr"
            or queued.actor_id != command.actor_id
            or queued.payload != {"pull_request_id": str(record_id), "poll": poll + 1}
            or queued.expected_run_version != event.run_version
        ):
            raise CommandRecoveryRequired("PR monitoring replay delivery differs")
        return
    outcome_type = {
        "ready": "run.merge_ready",
        "remediate": "run.remote_remediation_requested",
        "intervene": "run.release_intervention",
    }.get(str(disposition))
    outcomes = [
        item
        for item in await work.events.list_after(command.run_id, 0)
        if item.event_type == outcome_type
        and item.payload.get("source_command_id") == str(command.id)
    ]
    if len(outcomes) != 1:
        raise CommandRecoveryRequired("PR monitoring replay outcome receipt is missing")
    outcome = outcomes[0]
    receipt_payload = {
        key: value
        for key, value in event.payload.items()
        if key
        not in {
            "target",
            "remediation_command_id",
            "remediation_payload",
            "remediation_key",
            "remediation_actor_id",
        }
    }
    if (
        outcome.run_version != event.run_version
        or outcome.actor_class != event.actor_class
        or outcome.actor_id != event.actor_id
        or outcome.occurred_at != event.occurred_at
        or outcome.sequence is None
        or event.sequence is None
        or outcome.sequence >= event.sequence
        or outcome.payload != receipt_payload
    ):
        raise CommandRecoveryRequired("PR monitoring replay outcome receipt differs")
    if disposition == "ready":
        merge_digest = str(event.payload.get("merge_evidence_digest"))
        merge = await work.artifacts.get_by_digest(merge_digest, run_id=command.run_id)
        if (
            event.payload.get("target") != RunState.AWAITING_MERGE_APPROVAL.value
            or merge.producer_id != record_id
            or merge.producer_type != "merge_approval_evidence"
            or merge.truncated
            or not await store.verify(merge_digest)
            or (
                run.version == event.run_version
                and (
                    run.pending_gate is not ApprovalGate.MERGE
                    or run.pending_evidence_digest != merge_digest
                )
            )
        ):
            raise CommandRecoveryRequired("PR monitoring replay outcome approval differs")
    elif disposition == "remediate" and event.payload.get("target") == RunState.REMEDIATING.value:
        queued = await work.commands.get_by_idempotency_key(
            str(event.payload.get("remediation_key"))
        )
        if (
            queued is None
            or str(queued.id) != event.payload.get("remediation_command_id")
            or queued.run_id != command.run_id
            or queued.command_type
            != (
                "update_base"
                if event.payload.get("reason") == "base_advanced"
                else "remediate_remote"
            )
            or queued.payload != event.payload.get("remediation_payload")
            or str(queued.actor_id) != event.payload.get("remediation_actor_id")
            or queued.expected_run_version != event.run_version
            or queued.payload_schema_version != 1
        ):
            raise CommandRecoveryRequired("PR monitoring replay outcome delivery differs")
    elif event.payload.get("target") != RunState.AWAITING_HUMAN_INTERVENTION.value:
        raise CommandRecoveryRequired("PR monitoring replay outcome target differs")
