"""Durable ownership proofs for optional retained-branch removal."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from forge.application.handlers.teardown import (
    TeardownCommandRejected,
    _binding,
    _validate_progress,
)
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.command import CommandEnvelope
from forge.domain.event import RunEvent
from forge.domain.operation import canonical_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.domain.teardown import teardown_identity
from forge.tools.branch_removal import BranchRemovalBinding
from forge.tools.worktree import (
    WorktreeProvisionerError,
    _parse_teardown_checkpoint,
    _request,
    _teardown_checkpoint_payload,
    _teardown_request,
    _validate_succeeded_intent,
    _validate_teardown_checkpoint_shape,
)


async def require_branch_admission(
    run: RunSnapshot,
    policy: ProjectPolicy,
    binding: BranchRemovalBinding,
    events: Sequence[RunEvent],
    work: UnitOfWork,
    *,
    own_unresolved_operation: bool,
) -> CommandEnvelope:
    """Bind a branch intent to its immutable operator admission under the run lock.

    The caller proves the supplied intent is the stored operation before excluding
    that one operation from quiescence. Recovery need not hold the old command lease.
    """
    command = await work.commands.get(binding.source_command_id)
    if (
        command.id != binding.source_command_id
        or command.run_id != run.id
        or binding.run_id != run.id
        or binding.project_id != run.project_id
        or binding.policy_version != run.policy_version
        or binding.policy_version != policy.version
        or policy.id != run.project_id
        or binding.branch != run.branch_name
        or binding.base_sha != run.base_sha
        or binding.database_enabled != policy.database.enabled
        or command.command_type != "teardown_run_resources"
        or command.actor_id is None
        or command.payload_schema_version != 1
        or set(command.payload)
        != {"delete_branch", "confirm_branch_name", "confirm_resource_identity"}
        or command.payload.get("delete_branch") is not True
        or command.payload.get("confirm_branch_name") != binding.branch
    ):
        raise TeardownCommandRejected("branch source command differs")
    admissions = [
        event
        for event in events
        if event.event_type == "resource.teardown_admitted"
        and event.payload.get("source_command_id") == str(command.id)
    ]
    if len(admissions) != 1:
        raise TeardownCommandRejected("branch admission is absent or duplicated")
    admission = admissions[0]
    identity = admission.payload.get("identity")
    if not isinstance(identity, Mapping) or set(identity) != set(teardown_identity(run)):
        raise TeardownCommandRejected("branch admission identity differs")
    confirmation = f"teardown:{run.id}:{canonical_digest(identity)}"
    expected = {
        "source_command_id": str(command.id),
        "command_digest": _binding(command),
        "confirmation": confirmation,
        "identity": identity,
        "state": run.state.value,
        "branch_expected_head": binding.expected_head,
    }
    if (
        admission.run_id != run.id
        or admission.run_version != command.expected_run_version
        or admission.payload_schema_version != 1
        or admission.actor_class != "operator"
        or admission.actor_id != command.actor_id
        or type(identity.get("run_version")) is not int
        or identity["run_version"] != command.expected_run_version
        or command.payload.get("confirm_resource_identity") != confirmation
        or canonical_digest(admission.payload) != canonical_digest(expected)
    ):
        raise TeardownCommandRejected("branch admission differs")
    try:
        _validate_progress(run, identity)
    except CommandRecoveryRequired:
        raise TeardownCommandRejected("branch resource identity changed") from None
    quiescence = await work.runs.prove_quiescent(run.id, exclude_command_id=command.id)
    if (
        quiescence.pending_or_leased_commands
        or quiescence.running_steps
        or quiescence.running_executions
        or quiescence.running_tools
        or quiescence.unresolved_operations != int(own_unresolved_operation)
    ):
        raise TeardownCommandRejected("branch removal is blocked by unsettled work")
    return command


async def require_owned_removed_worktree(
    run: RunSnapshot,
    policy: ProjectPolicy,
    events: Sequence[RunEvent],
    work: UnitOfWork,
) -> WorktreeIdentity:
    """Reject foreign branch collisions even when worktree absence was recorded.

    Callers hold the run row lock and separately validate the operator admission.
    A removal receipt alone proves absence, not that Forge created the branch.
    """
    try:
        if (
            run.state not in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
            or run.project_id != policy.id
            or run.policy_version != policy.version
            or run.branch_name is None
            or run.base_ref is None
            or run.branch_name
            in {
                policy.default_branch.removeprefix("refs/heads/"),
                run.base_ref.removeprefix("refs/heads/"),
            }
            or run.worktree_path is not None
            or run.database_state not in {ResourceState.DISABLED, ResourceState.REMOVED}
            or (policy.database.enabled and run.database_state != ResourceState.REMOVED)
            or (not policy.database.enabled and run.database_state != ResourceState.DISABLED)
            or any(
                value is not None for value in (run.database_name, run.database_role, run.secret_id)
            )
        ):
            raise TeardownCommandRejected("branch resource ownership is unproven")
        identity = WorktreeIdentity.for_run(
            run.project_id, run.id, run.branch_name, policy.database.enabled
        )
        creation_request = _request(run, identity, policy)
        removal_request = _teardown_request(run, identity, policy)
        for request in (creation_request, removal_request):
            intent = await work.operations.get_by_idempotency_key(request.idempotency_key)
            if intent is None:
                raise TeardownCommandRejected("branch resource ownership is unproven")
            _validate_succeeded_intent(intent, request, identity)
        # `intent` is the canonical successful worktree-removal operation.
        removed = _parse_teardown_checkpoint(events, run, removal_request)
        _validate_teardown_checkpoint_shape(run, policy, removed)
        if intent is None or removed is None or removed.operation_intent_id != intent.id:
            raise TeardownCommandRejected("branch resource ownership is unproven")
        event = next(event for event in events if event.event_type == "resource.worktree_removed")
        expected_checkpoint = _teardown_checkpoint_payload(
            removal_request,
            intent.id,
            target_state=ResourceState(str(event.payload["database_state"])),
        )
        if (
            event.payload_schema_version != 1
            or event.actor_class != "system"
            or event.actor_id is not None
            or event.run_version > run.version
            or canonical_digest(event.payload) != canonical_digest(expected_checkpoint)
        ):
            raise TeardownCommandRejected("branch resource ownership is unproven")
        return identity
    except WorktreeProvisionerError, ValueError, TypeError:
        raise TeardownCommandRejected("branch resource ownership is unproven") from None
