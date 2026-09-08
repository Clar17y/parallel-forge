"""Operator-bound execution of durable managed-resource removal."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from uuid import UUID

from pydantic import ValidationError

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.command import CommandEnvelope
from forge.domain.event import RunEvent
from forge.domain.operation import canonical_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import ResourceState
from forge.domain.run import RunSnapshot, RunState
from forge.domain.teardown import teardown_confirmation, teardown_identity

_ADMITTED = "resource.teardown_admitted"
_COMPLETED = "resource.teardown_completed"
_TERMINAL = {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}


class TeardownCommandRejected(RuntimeError):
    """A teardown command conclusively lacks authority before any effect."""


class TeardownRunResourcesHandler:
    def __init__(self, teardown: Callable[[UUID, ProjectPolicy], Awaitable[RunSnapshot]]) -> None:
        self._teardown = teardown

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        await _fence(command, work)
        if (
            command.command_type != "teardown_run_resources"
            or command.actor_id is None
            or command.payload_schema_version != 1
            or set(command.payload) != {"delete_branch", "confirm_resource_identity"}
            or command.payload.get("delete_branch") is not False
            or not isinstance(command.payload.get("confirm_resource_identity"), str)
        ):
            raise TeardownCommandRejected("teardown command authority is invalid")
        run = await work.runs.get_for_update(command.run_id)
        if run.state not in _TERMINAL:
            raise TeardownCommandRejected("resource teardown requires a terminal run")
        events = await work.events.list_after(run.id, 0)
        admissions = [
            event
            for event in events
            if event.event_type == _ADMITTED
            and event.payload.get("source_command_id") == str(command.id)
        ]
        completions = [
            event
            for event in events
            if event.event_type == _COMPLETED
            and event.payload.get("source_command_id") == str(command.id)
        ]
        binding = _binding(command)
        if len(admissions) > 1 or len(completions) > 1 or (completions and not admissions):
            raise CommandRecoveryRequired("teardown receipt lineage is invalid")
        if admissions:
            admission = admissions[0]
            identity = admission.payload.get("identity")
            if (
                admission.payload_schema_version != 1
                or set(admission.payload)
                != {"source_command_id", "command_digest", "confirmation", "identity", "state"}
                or admission.actor_id != command.actor_id
                or admission.actor_class != "operator"
                or admission.payload.get("command_digest") != binding
                or admission.payload.get("confirmation")
                != command.payload["confirm_resource_identity"]
                or admission.payload.get("state") != run.state.value
                or not isinstance(identity, Mapping)
                or set(identity) != set(teardown_identity(run))
                or identity.get("run_version") != command.expected_run_version
                or admission.run_version != command.expected_run_version
                or f"teardown:{run.id}:{canonical_digest(identity)}"
                != command.payload["confirm_resource_identity"]
            ):
                raise CommandRecoveryRequired("teardown admission does not match its command")
            _validate_progress(run, identity)
        else:
            if (
                run.version != command.expected_run_version
                or teardown_confirmation(run) != command.payload["confirm_resource_identity"]
            ):
                raise TeardownCommandRejected("resource confirmation is stale")
            identity = teardown_identity(run)
        quiescence = await work.runs.prove_quiescent(run.id, exclude_command_id=command.id)
        if not quiescence.is_quiescent:
            raise CommandRecoveryRequired("resource teardown is blocked by unsettled work")
        if completions:
            completion = completions[0]
            if (
                completion.payload_schema_version != 1
                or completion.actor_class != "worker"
                or completion.actor_id is not None
                or completion.run_version != run.version
                or completion.payload != _completion(command, run, binding)
            ):
                raise CommandRecoveryRequired("teardown completion does not match resources")
            _require_removed(run)
            await work.commit()
            return
        policy = await _policy(run, work)
        if not admissions:
            await work.events.append(
                RunEvent(
                    run_id=run.id,
                    run_version=run.version,
                    event_type=_ADMITTED,
                    actor_class="operator",
                    actor_id=command.actor_id,
                    payload={
                        "source_command_id": str(command.id),
                        "command_digest": binding,
                        "confirmation": command.payload["confirm_resource_identity"],
                        "identity": identity,
                        "state": run.state.value,
                    },
                )
            )
        # Resource operations use their own durable transactions; do not hold
        # this run lock across them. The current leased command blocks new teardown admission.
        await work.commit()
        try:
            await self._teardown(run.id, policy)
        except Exception:  # noqa: BLE001 - admitted effects need durable reconciliation
            raise CommandRecoveryRequired("resource teardown requires recovery") from None
        await _fence(command, work)
        current = await work.runs.get_for_update(run.id)
        if current.state != run.state:
            raise CommandRecoveryRequired("run changed during resource teardown")
        _validate_progress(current, identity)
        _require_removed(current)
        await work.events.append(
            RunEvent(
                run_id=current.id,
                run_version=current.version,
                event_type=_COMPLETED,
                actor_class="worker",
                payload=_completion(command, current, binding),
            )
        )
        await work.commit()


def _completion(command: CommandEnvelope, run: RunSnapshot, binding: str) -> dict[str, object]:
    return {
        "source_command_id": str(command.id),
        "command_digest": binding,
        "resource_identity": teardown_identity(run),
    }


def _binding(command: CommandEnvelope) -> str:
    return canonical_digest(
        {
            "id": str(command.id),
            "run_id": str(command.run_id),
            "command_type": command.command_type,
            "idempotency_key": command.idempotency_key,
            "payload": dict(command.payload),
            "payload_schema_version": command.payload_schema_version,
            "expected_run_version": command.expected_run_version,
            "actor_id": str(command.actor_id),
        }
    )


async def _fence(command: CommandEnvelope, work: UnitOfWork) -> None:
    fenced = await work.commands.assert_current_lease(command)
    if _binding(fenced) != _binding(command):
        raise CommandRecoveryRequired("teardown delivery does not match its lease")


def _validate_progress(run: RunSnapshot, original: Mapping[str, object]) -> None:
    current = teardown_identity(run)
    changing = {"run_version", "worktree_path", "database_state", "database_name", "database_role"}
    if any(current[key] != original[key] for key in current.keys() - changing):
        raise CommandRecoveryRequired("teardown resource ownership changed")
    if run.worktree_path not in (original["worktree_path"], None):
        raise CommandRecoveryRequired("teardown worktree identity changed")
    database_keys = ("database_state", "database_name", "database_role")
    if tuple(current[key] for key in database_keys) not in (
        tuple(original[key] for key in database_keys),
        (ResourceState.REMOVED.value, None, None),
    ):
        raise CommandRecoveryRequired("teardown database identity changed")


def _require_removed(run: RunSnapshot) -> None:
    if run.worktree_path is not None or run.database_state not in {
        ResourceState.DISABLED,
        ResourceState.REMOVED,
    }:
        raise CommandRecoveryRequired("resource teardown has not completed")


async def _policy(run: RunSnapshot, work: UnitOfWork) -> ProjectPolicy:
    if run.policy_version is None:
        raise TeardownCommandRejected("run has no frozen resource policy")
    record = await work.projects.get_policy(run.project_id, run.policy_version)
    try:
        policy = ProjectPolicy.model_validate(record.document)
    except ValidationError:
        raise TeardownCommandRejected("frozen resource policy is invalid") from None
    if (
        record.project_id != run.project_id
        or record.version != run.policy_version
        or record.document_schema_version != 1
        or policy.id != run.project_id
        or policy.version != run.policy_version
        or canonical_digest(record.document) != record.policy_digest
    ):
        raise TeardownCommandRejected("frozen resource policy identity is invalid")
    return policy
