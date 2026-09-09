"""Immutable monitor authority shared by base-update delivery and startup recovery."""

import hashlib
import json
from dataclasses import asdict, replace
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.release import ReleaseRecord
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.approved_plan import ApprovedPlan
from forge.domain.command import CommandEnvelope, CommandStatus


async def base_update_origin(
    work: UnitOfWork, store: ArtifactStore, command: CommandEnvelope,
    approved: ApprovedPlan, record: ReleaseRecord,
) -> tuple[str, str]:
    run = approved.run
    attempt = command.payload.get("remote_attempt")
    if (
        command.run_id != run.id
        or command.command_type != "update_base"
        or command.payload_schema_version != 1
        or set(command.payload) != {"observation_digest", "pull_request_id", "remote_attempt", "target_base_sha"}
        or type(attempt) is not int
        or not 1 <= attempt <= min(run.remote_remediation_count, approved.policy.remote_remediation_limit)
        or command.idempotency_key != f"{run.id}:remote-remediation:{attempt}"
        or command.actor_id != approved.approval_actor_id
        or command.payload["pull_request_id"] != str(record.id)
        or command.expected_run_version > run.version
    ):
        raise CommandRecoveryRequired("base update historical authority differs")
    events = await work.events.list_after(run.id, 0)
    parents = [
        e
        for e in events
        if e.event_type == "run.pr_observed"
        and e.payload.get("remediation_command_id") == str(command.id)
    ]
    if (
        len(parents) != 1
        or parents[0].run_version != command.expected_run_version
        or parents[0].payload.get("reason") != "base_advanced"
        or parents[0].payload.get("remediation_payload") != command.payload
        or parents[0].payload.get("remediation_key") != command.idempotency_key
        or parents[0].actor_class != "worker"
        or parents[0].payload.get("disposition") != "remediate"
        or parents[0].payload.get("target") != "REMEDIATING"
        or parents[0].payload.get("observation_digest") != command.payload["observation_digest"]
    ):
        raise CommandRecoveryRequired("base update source differs")
    try:
        source_id = UUID(str(parents[0].payload.get("source_command_id")))
    except ValueError:
        raise CommandRecoveryRequired("base update source differs") from None
    source = await work.commands.get(source_id)
    from forge.application.services.monitor_resume import monitor_inputs, monitor_origin

    await monitor_origin(work, source)
    if (
        source.run_id != run.id
        or source.command_type != "monitor_pr"
        or source.status is not CommandStatus.COMPLETED
        or source.payload_schema_version != 1
        or source.expected_run_version + 1 != command.expected_run_version
        or source.actor_id != parents[0].actor_id
        or monitor_inputs(source) != (record.id, parents[0].payload.get("poll"))
    ):
        raise CommandRecoveryRequired("base update source differs")
    digest = str(command.payload["observation_digest"])
    descriptor = await work.artifacts.get_by_digest(digest, run_id=run.id)
    wire = await store.open_bytes(digest, max_bytes=1048576)
    try:
        observation = json.loads(wire)
    except ValueError:
        raise CommandRecoveryRequired("base update observation differs") from None
    target = str(command.payload["target_base_sha"])
    if (
        hashlib.sha256(wire).hexdigest() != digest
        or not isinstance(observation, dict)
        or descriptor.producer_type != "remote_pr_observation"
        or descriptor.producer_id != record.id
        or descriptor.media_type != "application/json"
        or descriptor.byte_count != len(wire)
        or descriptor.truncated
        or observation.get("target_base_sha") != target
        or observation.get("pull_request_id") != str(record.id)
        or observation.get("pull_request")
        not in (
            asdict(record.pull_request),
            asdict(replace(record.pull_request, base_sha=target)),
        )
    ):
        raise CommandRecoveryRequired("base update observation differs")
    return digest, target
