"""Fence and settle one approved worktree preparation command."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import WorktreeProvisionerPort
from forge.application.services.approved_plan import ApprovedPlan, ApprovedPlanLoader
from forge.application.services.control_settlement import pending_current_control_stop
from forge.application.services.preparation_resume import (
    preparation_branch_source,
    preparation_history,
)
from forge.application.services.resume_source import resume_command_ids
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.resource import ResourceState, WorktreeIdentity, database_secret_id
from forge.domain.run import RunSnapshot, RunState


class DeliveryPreparationService:
    def __init__(
        self, approved_plans: ApprovedPlanLoader, provisioner: WorktreeProvisionerPort
    ) -> None:
        self._approved_plans = approved_plans
        self._provisioner = provisioner

    async def execute(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        fenced = await work.commands.assert_current_lease(command)
        if (
            fenced.command_type != command.command_type
            or fenced.idempotency_key != command.idempotency_key
            or fenced.payload != command.payload
            or fenced.payload_schema_version != command.payload_schema_version
            or fenced.expected_run_version != command.expected_run_version
            or fenced.actor_id != command.actor_id
        ):
            raise CommandRecoveryRequired("preparation delivery does not match its lease")
        approved = await self._approved_plans.load(work, command.run_id)
        resumed = resume_command_ids(command.payload) is not None
        history = await preparation_history(work, command, approved) if resumed else (command,)
        origin = history[-1]
        expected = approved.approval_version + 1
        if (
            fenced.command_type != "prepare_worktree"
            or origin.idempotency_key != f"{command.run_id}:prepare-worktree:{expected}"
            or origin.payload != {}
            or origin.payload_schema_version != 1
            or origin.expected_run_version != expected
            or origin.actor_id != approved.approval_actor_id
        ):
            raise CommandRecoveryRequired("preparation command authority is invalid")
        if approved.run.state is RunState.IMPLEMENTING:
            queued = await work.commands.get_by_idempotency_key(f"{command.run_id}:implement:1")
            events = [
                event
                for event in await work.events.list_after(command.run_id, 0)
                if event.event_type == "run.worktree_prepared"
            ]
            if (
                queued is None
                or not _valid_implementation(queued, approved, approved.run.version)
                or len(events) != 1
                or events[0].run_version != approved.run.version
                or events[0].actor_class != "worker"
                or events[0].payload != _prepared_payload(command, approved, queued)
            ):
                raise CommandRecoveryRequired("preparation replay requires recovery")
            return
        if approved.run.state is not RunState.PREPARING_WORKTREE:
            raise CommandRecoveryRequired("preparation command authority is invalid")
        if await pending_current_control_stop(work, approved.run):
            raise CommandRecoveryRequired("preparation is fenced by operator control")
        branch_events = [
            event
            for event in await work.events.list_after(command.run_id, 0)
            if event.event_type == "run.preparation_branch_bound"
        ]
        if approved.run.branch_name is None:
            if branch_events or approved.run.version != command.expected_run_version:
                raise CommandRecoveryRequired("preparation branch binding requires recovery")
            branch_name = f"forge/run/{command.run_id.hex}"
            approval_id = approved.approval_id
            bound = await work.runs.bind_preparation_branch(
                command.run_id,
                command.expected_run_version,
                branch_name=branch_name,
                event_type="run.preparation_branch_bound",
                event_payload={
                    "source_command_id": str(command.id),
                    "approval_id": str(approved.approval_id),
                    "branch_name": branch_name,
                },
                actor_class="worker",
                actor_id=command.actor_id,
            )
            if bound.branch_name != branch_name:
                raise CommandRecoveryRequired("preparation branch binding differs")
            await work.commit()
            approved = await self._approved_plans.load(work, command.run_id)
            if (
                approved.run.state is not RunState.PREPARING_WORKTREE
                or approved.approval_id != approval_id
                or approved.run.branch_name != branch_name
            ):
                raise CommandRecoveryRequired("preparation branch binding requires recovery")
        elif resumed:
            await preparation_branch_source(work, history, approved)
        elif branch_events or approved.run.branch_name == f"forge/run/{command.run_id.hex}":
            if not _valid_branch_binding(command, approved, branch_events):
                raise CommandRecoveryRequired("preparation branch binding requires recovery")
        if await pending_current_control_stop(work, approved.run):
            raise CommandRecoveryRequired("preparation dispatch is fenced by operator control")
        await work.commit()
        try:
            worktree = await self._provisioner.prepare(command.run_id, approved.policy)
        except Exception:  # noqa: BLE001 - an uncertain resource effect needs reconciliation
            raise CommandRecoveryRequired("worktree provisioning requires recovery") from None
        fenced = await work.commands.assert_current_lease(command)
        refreshed = await self._approved_plans.load(work, command.run_id)
        if (
            refreshed.run.state is not RunState.PREPARING_WORKTREE
            or refreshed.approval_id != approved.approval_id
            or fenced.actor_id != command.actor_id
            or fenced.payload != command.payload
            or fenced.idempotency_key != command.idempotency_key
            or fenced.command_type != command.command_type
            or fenced.expected_run_version != command.expected_run_version
            or fenced.payload_schema_version != command.payload_schema_version
        ):
            raise CommandRecoveryRequired("preparation outcome requires recovery")
        if refreshed.run.branch_name is None:
            raise CommandRecoveryRequired("prepared run has no branch identity")
        if await pending_current_control_stop(work, refreshed.run):
            raise CommandRecoveryRequired("preparation publication is fenced by operator control")
        expected_identity = WorktreeIdentity.for_run(
            refreshed.run.project_id,
            refreshed.run.id,
            refreshed.run.branch_name,
            refreshed.policy.database.enabled,
        )
        if (
            worktree.identity != expected_identity
            or refreshed.run.worktree_path != str(worktree.path)
            or refreshed.run.branch_name != worktree.identity.branch
            or refreshed.run.base_sha != worktree.base_sha
        ):
            raise CommandRecoveryRequired("prepared worktree does not match run")
        _resource_digest(refreshed)
        queued = await work.commands.enqueue(
            run_id=command.run_id,
            command_type="implement",
            idempotency_key=f"{command.run_id}:implement:1",
            payload={"semantic_attempt": 1},
            expected_run_version=refreshed.run.version + 1,
            actor_id=refreshed.approval_actor_id,
        )
        if not _valid_implementation(queued, refreshed, refreshed.run.version + 1):
            raise CommandRecoveryRequired("implement command authority differs")
        transitioned = await work.runs.transition(
            command.run_id,
            refreshed.run.version,
            RunState.IMPLEMENTING,
            "run.worktree_prepared",
            _prepared_payload(command, refreshed, queued),
            actor_class="worker",
        )
        if queued.expected_run_version != transitioned.version:
            raise CommandRecoveryRequired("implement command version mismatch")
        await work.commit()


__all__ = ["DeliveryPreparationService"]


def _valid_implementation(queued: CommandEnvelope, approved: ApprovedPlan, version: int) -> bool:
    return (
        queued.run_id == approved.run.id
        and queued.command_type == "implement"
        and queued.status is CommandStatus.PENDING
        and queued.payload_schema_version == 1
        and queued.payload == {"semantic_attempt": 1}
        and type(queued.payload.get("semantic_attempt")) is int
        and queued.actor_id == approved.approval_actor_id
        and queued.expected_run_version == version
    )


def _valid_branch_binding(
    command: CommandEnvelope, approved: ApprovedPlan, events: Sequence[RunEvent]
) -> bool:
    if len(events) != 1:
        return False
    event = events[0]
    branch_name = f"forge/run/{command.run_id.hex}"
    return (
        approved.run.branch_name == branch_name
        and event.run_version == command.expected_run_version + 1
        and event.actor_class == "worker"
        and event.actor_id == command.actor_id
        and event.payload
        == {
            "source_command_id": str(command.id),
            "approval_id": str(approved.approval_id),
            "branch_name": branch_name,
        }
    )


def _prepared_payload(
    command: CommandEnvelope,
    approved: ApprovedPlan,
    queued: CommandEnvelope,
) -> dict[str, object]:
    return {
        "source_command_id": str(command.id),
        "approval_id": str(approved.approval_id),
        "queued_command_id": str(queued.id),
        "queued_payload": {"semantic_attempt": 1},
        "resource_digest": _resource_digest(approved),
    }


def _resource_digest(approved: ApprovedPlan) -> str:
    run = approved.run
    if run.branch_name is None or run.worktree_path is None or run.base_sha is None:
        raise CommandRecoveryRequired("prepared resource identity is incomplete")
    identity = WorktreeIdentity.for_run(
        run.project_id,
        run.id,
        run.branch_name,
        approved.policy.database.enabled,
    )
    _verify_database(run, identity)
    payload = {
        "project_id": str(run.project_id),
        "run_id": str(run.id),
        "base_sha": run.base_sha,
        "worktree_path": run.worktree_path,
        "branch_name": run.branch_name,
        "database_state": run.database_state.value,
        "database_name": run.database_name,
        "database_role": run.database_role,
        "secret_id": run.secret_id,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _verify_database(run: RunSnapshot, identity: WorktreeIdentity) -> None:
    enabled = identity.database_name is not None
    if (
        run.database_state is not (ResourceState.ACTIVE if enabled else ResourceState.DISABLED)
        or run.database_name != identity.database_name
        or run.database_role != identity.database_role
        or run.secret_id != (database_secret_id(identity) if enabled else None)
    ):
        raise CommandRecoveryRequired("prepared database identity differs")
