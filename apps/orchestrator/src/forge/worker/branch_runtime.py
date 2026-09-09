"""Durable ownership proofs for optional retained-branch removal."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager

from forge.application.handlers.teardown import (
    TeardownCommandRejected,
    _binding,
    _fence,
    _policy,
    _validate_progress,
)
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.operations import OperationAdapter
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.application.services.projects import _digest as policy_digest
from forge.application.services.recovery import OperationExecutor, RecoveryError
from forge.domain.branch_removal import BranchSourceRejected
from forge.domain.command import CommandEnvelope
from forge.domain.event import RunEvent
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationRequest,
    OperationStatus,
    canonical_digest,
)
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.domain.run import RunSnapshot
from forge.domain.teardown import TEARDOWN_STATES, teardown_identity
from forge.tools.branch_removal import (
    BranchRemovalAdapter,
    BranchRemovalBinding,
    branch_removal_request,
)
from forge.tools.database import DatabaseProvisioner
from forge.tools.database import _request as database_request
from forge.tools.runner import await_deferred_cancellation
from forge.tools.worktree import (
    WorktreeProvisionerError,
    _parse_teardown_checkpoint,
    _request,
    _teardown_checkpoint_payload,
    _teardown_request,
    _validate_intent,
    _validate_succeeded_intent,
    _validate_teardown_checkpoint_shape,
)


class BranchRemovalRuntime:
    """Compose frozen command authority, controlled Git and durable receipts."""

    def __init__(
        self,
        factory: Callable[[], AbstractAsyncContextManager[UnitOfWork]],
        git: Callable[[ProjectPolicy], ControlledGitPort],
        executor: OperationExecutor,
    ) -> None:
        self._factory, self._git, self._executor = factory, git, executor

    def recovery_adapter(self) -> OperationAdapter:
        return _BranchRecovery(self)

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        async with self._factory() as work:
            run = await work.runs.get_for_update(intent.run_id)
            policy = await _policy(run, work)
            await work.commit()
        request = OperationRequest(
            run_id=intent.run_id,
            kind=intent.kind,
            idempotency_key=intent.idempotency_key,
            request_digest=intent.request_digest,
            request_payload=intent.request_payload,
            request_schema_version=intent.request_schema_version,
        )
        return await self.adapter(request, policy).reconcile(intent)

    def _handle(self, run: RunSnapshot, policy: ProjectPolicy) -> ManagedWorktree:
        if run.branch_name is None or run.base_sha is None:
            raise TeardownCommandRejected("branch identity is absent")
        identity = WorktreeIdentity.for_run(
            run.project_id, run.id, run.branch_name, policy.database.enabled
        )
        return self._git(policy).expected_worktree(identity, run.base_sha)

    async def observe_head(
        self, run: RunSnapshot, policy: ProjectPolicy, work: UnitOfWork
    ) -> str | None:
        # The handler holds the run lock. Prove creation authority before it
        # admits teardown or removes the worktree and database.
        try:
            if (
                run.branch_name is None
                or run.project_id != policy.id
                or run.policy_version != policy.version
            ):
                raise TeardownCommandRejected("branch resource ownership is unproven")
            identity = WorktreeIdentity.for_run(
                run.project_id, run.id, run.branch_name, policy.database.enabled
            )
            request = _request(run, identity, policy)
            intent = await work.operations.get_by_idempotency_key(request.idempotency_key)
            if intent is None:
                raise TeardownCommandRejected("branch resource ownership is unproven")
            _validate_succeeded_intent(intent, request, identity)
        except WorktreeProvisionerError, ValueError, TypeError:
            raise TeardownCommandRejected("branch resource ownership is unproven") from None
        project = await work.projects.get(run.project_id, for_update=True)
        if (
            project.canonical_path != policy.repository_path
            or project.github_repository != policy.github_repository
            or project.default_branch != policy.default_branch
        ):
            raise TeardownCommandRejected("branch frozen project policy differs")
        handle = self._handle(run, policy)
        if run.branch_name in {
            policy.default_branch.removeprefix("refs/heads/"),
            (run.base_ref or "").removeprefix("refs/heads/"),
        }:
            raise TeardownCommandRejected("protected branch cannot be removed")
        if run.worktree_path is not None and run.worktree_path != str(handle.path):
            raise TeardownCommandRejected("branch worktree identity differs")
        git = self._git(policy)
        operation = git.head_sha if run.worktree_path is not None else git.retained_branch_head
        head, cancelled = await await_deferred_cancellation(asyncio.to_thread(operation, handle))
        if cancelled:
            raise asyncio.CancelledError()
        return head

    def _request(
        self,
        run: RunSnapshot,
        policy: ProjectPolicy,
        command: CommandEnvelope,
        expected_head: str | None,
    ) -> OperationRequest:
        return branch_removal_request(
            self._handle(run, policy),
            policy_version=policy.version,
            source_command_id=command.id,
            expected_head=expected_head,
        )

    async def _source(self, intent: OperationIntent, policy: ProjectPolicy) -> ManagedWorktree:
        binding = BranchRemovalBinding.model_validate(dict(intent.request_payload))
        async with self._factory() as work:
            stored = await work.operations.get(intent.id)
            if (
                stored.run_id != intent.run_id
                or stored.kind != intent.kind
                or stored.idempotency_key != intent.idempotency_key
                or stored.request_digest != intent.request_digest
                or stored.request_schema_version != intent.request_schema_version
                or canonical_digest(stored.request_payload)
                != canonical_digest(intent.request_payload)
            ):
                raise TeardownCommandRejected("branch stored operation differs")
            run = await work.runs.get_for_update(intent.run_id)
            frozen = await _policy(run, work)
            project = await work.projects.get(run.project_id, for_update=True)
            if (
                policy_digest(frozen.model_dump(mode="json"))
                != policy_digest(policy.model_dump(mode="json"))
                or project.canonical_path != policy.repository_path
                or project.github_repository != policy.github_repository
                or project.default_branch != policy.default_branch
            ):
                raise TeardownCommandRejected("branch frozen project policy differs")
            events = await work.events.list_after(run.id, 0)
            command = await require_branch_admission(
                run,
                policy,
                binding,
                events,
                work,
                own_unresolved_operation=stored.status
                in {OperationStatus.PENDING, OperationStatus.NEEDS_RECONCILIATION},
            )
            if intent.is_new:
                await _fence(command, work)
            identity = await require_owned_removed_worktree(run, policy, events, work)
            if identity != binding.identity():
                raise TeardownCommandRejected("branch owned identity differs")
            handle = self._handle(run, policy)
            await work.commit()
            return handle

    def adapter(self, request: OperationRequest, policy: ProjectPolicy) -> BranchRemovalAdapter:
        async def source(intent: OperationIntent) -> ManagedWorktree:
            try:
                return await self._source(intent, policy)
            except TeardownCommandRejected:
                raise BranchSourceRejected("branch source authority rejected") from None

        return BranchRemovalAdapter(request, self._git(policy), source)

    async def remove(
        self, command: CommandEnvelope, policy: ProjectPolicy, expected_head: str | None
    ) -> RunSnapshot:
        async with self._factory() as work:
            run = await work.runs.get_for_update(command.run_id)
            await _fence(command, work)
            request = self._request(run, policy, command, expected_head)
            prior = await work.operations.get_by_idempotency_key(request.idempotency_key)
            if prior is not None and prior.status is OperationStatus.FAILED:
                raise TeardownCommandRejected("branch remains; fresh confirmation required")
            await work.commit()
        outcome = await self._executor.execute(request, self.adapter(request, policy))
        if outcome.status is not OperationStatus.SUCCEEDED:
            raise TeardownCommandRejected("branch remains; fresh confirmation required")
        return await self._checkpoint(command, policy, expected_head, create=True)

    async def validate_completed(
        self, command: CommandEnvelope, policy: ProjectPolicy, expected_head: str | None
    ) -> None:
        await self._checkpoint(command, policy, expected_head, create=False)

    async def _checkpoint(
        self,
        command: CommandEnvelope,
        policy: ProjectPolicy,
        expected_head: str | None,
        *,
        create: bool,
    ) -> RunSnapshot:
        # Validate source outside the checkpoint transaction: its own run lock must
        # be released before this transaction acquires the same lock.
        async with self._factory() as work:
            run = await work.runs.get_for_update(command.run_id)
            request = self._request(run, policy, command, expected_head)
            intent = await work.operations.get_by_idempotency_key(request.idempotency_key)
            if intent is None:
                raise TeardownCommandRejected("branch receipt is absent")
            expected = {
                "source_command_id": str(command.id),
                "request_digest": request.request_digest,
                "branch": run.branch_name,
                "expected_head": expected_head,
                "removed": True,
            }
            if (
                intent.status is not OperationStatus.SUCCEEDED
                or intent.run_id != request.run_id
                or intent.kind != request.kind
                or intent.idempotency_key != request.idempotency_key
                or intent.request_schema_version != request.request_schema_version
                or intent.request_digest != request.request_digest
                or canonical_digest(intent.request_payload)
                != canonical_digest(request.request_payload)
                or intent.outcome_schema_version != 1
                or intent.remote_resource_id is not None
                or intent.outcome is None
                or canonical_digest(intent.outcome) != canonical_digest(expected)
            ):
                raise TeardownCommandRejected("branch receipt differs")
            await work.commit()
        await self._source(intent, policy)
        async with self._factory() as work:
            current = await work.runs.get_for_update(command.run_id)
            if current != run:
                raise CommandRecoveryRequired("branch resource changed before checkpoint")
            await _fence(command, work)
            payload = {
                "operation_intent_id": str(intent.id),
                "request_digest": request.request_digest,
                "source_command_id": str(command.id),
                "branch": run.branch_name,
                "expected_head": expected_head,
            }
            events = await work.events.list_after(run.id, 0)
            matches = [event for event in events if event.event_type == "resource.branch_removed"]
            if matches:
                if (
                    len(matches) != 1
                    or matches[0].actor_class != "worker"
                    or matches[0].actor_id is not None
                    or matches[0].payload_schema_version != 1
                    or matches[0].run_version > run.version
                    or canonical_digest(matches[0].payload) != canonical_digest(payload)
                ):
                    raise TeardownCommandRejected("branch checkpoint differs")
            elif not create:
                raise TeardownCommandRejected("branch checkpoint is absent")
            else:
                current = await work.runs.update_resource(
                    run.id,
                    run.version,
                    worktree_path=None,
                    database_state=run.database_state,
                    event_type="resource.branch_removed",
                    event_payload=payload,
                    actor_class="worker",
                )
            await work.commit()
            return current


class _BranchRecovery:
    def __init__(self, runtime: BranchRemovalRuntime) -> None:
        self._runtime = runtime

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        raise RecoveryError("startup branch recovery cannot invoke deletion")

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        return await self._runtime.reconcile(intent)


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
            run.state not in TEARDOWN_STATES
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
        if policy.database.enabled:
            request = database_request(
                identity, policy.version, "database.teardown", ResourceState.REMOVED
            )
            database_intent = await work.operations.get_by_idempotency_key(request.idempotency_key)
            if database_intent is None:
                raise TeardownCommandRejected("branch database ownership is unproven")
            _validate_intent(database_intent, request)
            expected_database = DatabaseProvisioner._outcome(
                state=ResourceState.REMOVED, identity=identity, secret_id=None
            )
            checkpoints = [
                event for event in events if event.event_type == "resource.database_removed"
            ]
            expected_payload = _teardown_checkpoint_payload(
                removal_request, intent.id, target_state=ResourceState.REMOVED
            )
            if (
                database_intent.status is not OperationStatus.SUCCEEDED
                or database_intent.outcome_schema_version != 1
                or database_intent.remote_resource_id is not None
                or database_intent.outcome is None
                or canonical_digest(database_intent.outcome)
                != canonical_digest(expected_database.payload)
                or len(checkpoints) != 1
                or checkpoints[0].run_id != run.id
                or checkpoints[0].actor_class != "system"
                or checkpoints[0].actor_id is not None
                or checkpoints[0].payload_schema_version != 1
                or checkpoints[0].run_version > run.version
                or canonical_digest(checkpoints[0].payload) != canonical_digest(expected_payload)
            ):
                raise TeardownCommandRejected("branch database ownership is unproven")
        return identity
    except WorktreeProvisionerError, ValueError, TypeError:
        raise TeardownCommandRejected("branch resource ownership is unproven") from None
