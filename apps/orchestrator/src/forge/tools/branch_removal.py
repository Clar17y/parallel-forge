"""Frozen, source-authorized branch deletion operations and recovery receipts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationRequest,
    OperationStatus,
    canonical_digest,
)
from forge.domain.resource import WorktreeIdentity
from forge.tools.runner import await_deferred_cancellation

BRANCH_REMOVAL_KIND = "git.branch_delete"


class BranchRemovalError(RuntimeError):
    """An immutable branch-removal operation requires reconciliation."""


class BranchRemovalBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: UUID
    project_id: UUID
    source_command_id: UUID
    policy_version: int = Field(strict=True, ge=1)
    branch: str = Field(strict=True, min_length=1, max_length=255)
    database_enabled: bool = Field(strict=True)
    worktree_name: str = Field(strict=True, min_length=1, max_length=128)
    base_sha: str = Field(strict=True, min_length=40, max_length=40, pattern=r"^[a-f0-9]{40}$")
    expected_head: str | None = Field(
        strict=True, min_length=40, max_length=40, pattern=r"^[a-f0-9]{40}$"
    )

    def identity(self) -> WorktreeIdentity:
        identity = WorktreeIdentity.for_run(
            self.project_id, self.run_id, self.branch, self.database_enabled
        )
        if identity.worktree_name != self.worktree_name:
            raise BranchRemovalError("branch removal identity is invalid")
        return identity


def branch_removal_request(
    handle: ManagedWorktree,
    *,
    policy_version: int,
    source_command_id: UUID,
    expected_head: str | None,
) -> OperationRequest:
    identity = handle.identity
    if identity.run_id is None:
        raise BranchRemovalError("branch removal requires a persisted run")
    binding = BranchRemovalBinding(
        run_id=identity.run_id,
        project_id=identity.project_id,
        source_command_id=source_command_id,
        policy_version=policy_version,
        branch=identity.branch,
        database_enabled=identity.database_name is not None,
        worktree_name=identity.worktree_name,
        base_sha=handle.base_sha,
        expected_head=expected_head,
    )
    if binding.identity() != identity:
        raise BranchRemovalError("branch removal identity is invalid")
    payload = binding.model_dump(mode="json")
    return OperationRequest(
        run_id=binding.run_id,
        kind=BRANCH_REMOVAL_KIND,
        idempotency_key=_key(binding),
        request_digest=canonical_digest(payload),
        request_payload=payload,
    )


def _key(binding: BranchRemovalBinding) -> str:
    return f"{binding.run_id}:branch_delete:{binding.source_command_id}"


class BranchRemovalAdapter:
    """Require a caller's durable source-authority check before any Git access.

    The source validator must bind the operator admission, command, frozen policy,
    and removed-resource checkpoints to this intent and return the exact handle.
    Recovery never substitutes a newly observed commit or retries deletion.
    """

    def __init__(
        self,
        request: OperationRequest,
        git: ControlledGitPort,
        validate_source: Callable[[OperationIntent], Awaitable[ManagedWorktree]],
    ) -> None:
        try:
            binding = BranchRemovalBinding.model_validate(dict(request.request_payload))
            binding.identity()
            payload = binding.model_dump(mode="json")
        except ValueError, TypeError:
            raise BranchRemovalError("branch removal request is invalid") from None
        if (
            request.kind != BRANCH_REMOVAL_KIND
            or request.request_schema_version != 1
            or request.run_id != binding.run_id
            or request.idempotency_key != _key(binding)
            or request.request_digest != canonical_digest(payload)
            or dict(request.request_payload) != payload
        ):
            raise BranchRemovalError("branch removal request is invalid")
        self._request = request
        self._binding = binding
        self._git = git
        self._validate_source = validate_source

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        return await self._execute(intent, invoke=True)

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        return await self._execute(intent, invoke=False)

    async def _execute(self, intent: OperationIntent, *, invoke: bool) -> OperationOutcome:
        request = self._request
        if (
            intent.run_id != request.run_id
            or intent.kind != request.kind
            or intent.idempotency_key != request.idempotency_key
            or intent.request_schema_version != request.request_schema_version
            or intent.request_digest != request.request_digest
            or dict(intent.request_payload) != dict(request.request_payload)
        ):
            raise BranchRemovalError("branch removal intent differs from its source")
        try:
            handle = await self._validate_source(intent)
            expected = self._git.expected_worktree(self._binding.identity(), self._binding.base_sha)
            if handle != expected:
                raise BranchRemovalError("branch removal source handle differs")
            removed, cancelled = await await_deferred_cancellation(
                asyncio.to_thread(self._operate, handle, invoke)
            )
        except Exception:  # noqa: BLE001 - callback and Git diagnostics are private
            raise BranchRemovalError("branch removal requires reconciliation") from None
        if cancelled:
            raise asyncio.CancelledError()
        return OperationOutcome(
            status=OperationStatus.SUCCEEDED if removed else OperationStatus.FAILED,
            payload={
                "source_command_id": str(self._binding.source_command_id),
                "request_digest": request.request_digest,
                "branch": self._binding.branch,
                "expected_head": self._binding.expected_head,
                "removed": removed,
            },
            error=None if removed else "branch remains; fresh confirmation required",
        )

    def _operate(self, handle: ManagedWorktree, invoke: bool) -> bool:
        if self._binding.expected_head is None:
            return self._git.retained_branch_head(handle) is None
        if invoke:
            self._git.delete_retained_branch(handle, self._binding.expected_head)
        return self._git.inspect_retained_branch_deletion(handle, self._binding.expected_head)
