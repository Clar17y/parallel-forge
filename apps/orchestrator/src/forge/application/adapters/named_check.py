"""Durable terminal receipts for policy-selected named checks.

The parent tool service creates the closed ``named_check`` intent before this
adapter is called.  This adapter deliberately has no policy or worktree lookup:
its constructor receives the already-frozen capability and its recovery path
never creates a runner.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Final, Literal, Self, cast
from uuid import UUID

from forge.application.adapters.named_check_receipts import (
    NamedCheckReceiptError,
    decode_command_result,
    encode_command_result,
    verify_output_envelope,
)
from forge.application.ports.artifacts import ArtifactRepository, ArtifactStore
from forge.application.ports.operations import OperationAdapter
from forge.application.ports.runner import (
    CommandResult,
    CommandTerminalResult,
    LaunchOwnership,
    LaunchOwnershipRejected,
    RunCommandRequest,
    TerminalRunnerPort,
    WorktreeRunnerFactoryPort,
)
from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationStatus,
    canonical_digest,
)
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode
from forge.domain.validation import command_spec_digest, effective_network_enabled

NAMED_CHECK_KIND: Final = "named_check"
_STREAMS: Final[tuple[Literal["stdout", "stderr"], ...]] = ("stdout", "stderr")
_RESULT_MEDIA: Final = "application/vnd.forge.command-result+json"
_RECEIPT_MEDIA: Final = "application/vnd.forge.named-check-receipt+json"
_OUTPUT_MEDIA: Final = "application/vnd.forge.command-output+json"
_RECEIPT_KEYS: Final = frozenset(
    {
        "caller_cancelled",
        "command_result_digest",
        "intent_id",
        "receipt_version",
        "request_digest",
        "request_payload",
        "stderr_digest",
        "stdout_digest",
        "tool_call_id",
    }
)
_CANCELLED_RECEIPT_KEYS: Final = frozenset(
    {
        "disposition",
        "intent_id",
        "receipt_version",
        "request_digest",
        "request_payload",
        "tool_call_id",
    }
)
_CANCELLED_BEFORE_LAUNCH: Final = "cancelled_before_launch"


class NamedCheckOperationError(RuntimeError):
    """A named-check effect is malformed or lacks durable terminal proof."""


class NamedCheckCancellation:
    """Relay caller cancellation only to the runner's terminal boundary."""

    def __init__(self) -> None:
        self._requested = False
        self._ownership = LaunchOwnership()
        self._runner_task: asyncio.Task[CommandTerminalResult] | None = None

    def request(self) -> None:
        if self._requested:
            return
        self._requested = True
        self._ownership.request_cancellation()
        if self._runner_task is not None and not self._runner_task.done():
            self._runner_task.cancel()

    @property
    def requested(self) -> bool:
        """Whether cancellation arrived before or during terminal execution."""

        return self._requested

    @property
    def ownership(self) -> LaunchOwnership:
        return self._ownership

    async def run(
        self, runner: TerminalRunnerPort, request: RunCommandRequest
    ) -> CommandTerminalResult | None:
        task = asyncio.create_task(runner.run_terminal(request))
        self._runner_task = task
        if self._requested:
            # Let run_terminal enter its cancellation handler before delivery.
            asyncio.get_running_loop().call_soon(task.cancel)
        try:
            terminal = await task
            return CommandTerminalResult(
                result=terminal.result,
                caller_cancelled=terminal.caller_cancelled or self._requested,
            )
        except asyncio.CancelledError, LaunchOwnershipRejected:
            if self._requested and not self._ownership.accepted:
                return None
            raise
        finally:
            self._runner_task = None


class NamedCheckOperationAdapter(OperationAdapter):
    """Run one already-admitted command and retain exact, replayable evidence.

    ``environment`` is transient Forge-owned material.  It is passed to the
    runner but only the sorted key digest contained in the intent is persisted.
    """

    def __init__(
        self,
        *,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        controlled_git: ControlledGitPort | None,
        runner_factory: WorktreeRunnerFactoryPort | None,
        environment: Mapping[str, str] | None,
        artifacts: ArtifactRepository,
        artifact_store: ArtifactStore,
        cancellation: NamedCheckCancellation | None = None,
    ) -> None:
        self._worktree = worktree
        self._policy = policy
        self._git = controlled_git
        self._factory = runner_factory
        self._environment = None if environment is None else dict(environment)
        self._artifacts = artifacts
        self._store = artifact_store
        self._cancellation = cancellation

    @classmethod
    def for_recovery(
        cls,
        *,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        artifacts: ArtifactRepository,
        artifact_store: ArtifactStore,
    ) -> Self:
        """Verify existing evidence without command capabilities or secret material."""
        return cls(
            worktree=worktree,
            policy=policy,
            controlled_git=None,
            runner_factory=None,
            environment=None,
            artifacts=artifacts,
            artifact_store=artifact_store,
        )

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        values, command, call_id = self._request(intent)
        if self._cancellation is not None and self._cancellation.requested:
            return await self.cancel_before_launch(intent)
        if self._git is None or self._factory is None or self._environment is None:
            raise NamedCheckOperationError("recovery adapter cannot execute commands")
        if self._git.head_sha(self._worktree) != values["head_sha"]:
            raise NamedCheckOperationError("named check worktree head changed")
        runner = self._factory.create(self._worktree, self._policy)
        request = RunCommandRequest(
            command_name=command.name,
            kind=command.kind,
            environment=self._environment,
            launch_ownership=None if self._cancellation is None else self._cancellation.ownership,
        )
        terminal = await (
            runner.run_terminal(request)
            if self._cancellation is None
            else self._cancellation.run(runner, request)
        )
        if terminal is None:
            return await self.cancel_before_launch(intent)
        result = terminal.result
        self._result_matches(values, command, result)
        return await self._persist(intent, call_id, result, terminal.caller_cancelled)

    async def cancel_before_launch(self, intent: OperationIntent) -> OperationOutcome:
        """Persist canonical evidence that this admission had no command effect."""

        if self._environment is None:
            raise NamedCheckOperationError("recovery adapter cannot create cancellation proof")
        values, _, call_id = self._request(intent)
        receipt = _encode_cancelled_receipt(intent, call_id, values)
        descriptor = await self._store.put_bytes(receipt, media_type=_RECEIPT_MEDIA)
        if (
            descriptor.byte_count != len(receipt)
            or hashlib.sha256(receipt).hexdigest() != descriptor.digest
            or await self._store.verify(descriptor.digest) is not True
        ):
            raise NamedCheckOperationError("named check cancellation receipt verification failed")
        await self._artifacts.record(
            descriptor, run_id=intent.run_id, producer_type="named_check", producer_id=call_id
        )
        return OperationOutcome(
            payload={
                "disposition": _CANCELLED_BEFORE_LAUNCH,
                "receipt_digest": descriptor.digest,
                "tool_call_id": str(call_id),
            }
        )

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        values, command, call_id = self._request(intent)
        receipts = await self._artifacts.get_by_producer(
            run_id=intent.run_id, producer_type="named_check", producer_id=call_id
        )
        if len(receipts) != 1:
            return _needs_reconciliation()
        descriptor = receipts[0]
        try:
            if not _is_descriptor(
                descriptor, intent.run_id, "named_check", call_id, _RECEIPT_MEDIA
            ):
                raise NamedCheckReceiptError()
            receipt_data = await self._verified_bytes(descriptor)
            if descriptor.parent_digests == ():
                cancelled = _decode_cancelled_receipt(receipt_data)
                if (
                    cancelled["intent_id"] != str(intent.id)
                    or cancelled["request_digest"] != intent.request_digest
                    or canonical_digest(cast(Mapping[str, object], cancelled["request_payload"]))
                    != intent.request_digest
                    or cancelled["tool_call_id"] != str(call_id)
                ):
                    raise NamedCheckReceiptError()
                return OperationOutcome(
                    payload={
                        "disposition": _CANCELLED_BEFORE_LAUNCH,
                        "receipt_digest": descriptor.digest,
                        "tool_call_id": str(call_id),
                    }
                )
            receipt = _decode_receipt(receipt_data)
            if (
                receipt["intent_id"] != str(intent.id)
                or receipt["request_digest"] != intent.request_digest
            ):
                raise NamedCheckReceiptError()
            if canonical_digest(
                cast(Mapping[str, object], receipt["request_payload"])
            ) != intent.request_digest or receipt["tool_call_id"] != str(call_id):
                raise NamedCheckReceiptError()
            command_result_digest = cast(str, receipt["command_result_digest"])
            stdout_digest = cast(str, receipt["stdout_digest"])
            stderr_digest = cast(str, receipt["stderr_digest"])
            parents = tuple(sorted({command_result_digest, stdout_digest, stderr_digest}))
            if descriptor.parent_digests != parents:
                raise NamedCheckReceiptError()
            result_descriptor = await self._artifacts.get_by_digest(
                command_result_digest, run_id=intent.run_id
            )
            if not _is_descriptor(
                result_descriptor, intent.run_id, "command_result", call_id, _RESULT_MEDIA
            ):
                raise NamedCheckReceiptError()
            result = decode_command_result(await self._verified_bytes(result_descriptor))
            if (
                result_descriptor.digest != command_result_digest
                or result.evidence_digest != command_result_digest
                or result.stdout_digest != stdout_digest
                or result.stderr_digest != stderr_digest
                or result_descriptor.parent_digests != tuple(sorted({stdout_digest, stderr_digest}))
            ):
                raise NamedCheckReceiptError()
            self._result_matches(values, command, result)
            for stream in _STREAMS:
                digest = result.stdout_digest if stream == "stdout" else result.stderr_digest
                output = await self._artifacts.get_by_digest(digest, run_id=intent.run_id)
                if not _is_descriptor(
                    output, intent.run_id, "command_output", intent.run_id, _OUTPUT_MEDIA
                ):
                    raise NamedCheckReceiptError()
                if output.digest != digest or output.parent_digests:
                    raise NamedCheckReceiptError()
                verify_output_envelope(
                    await self._verified_bytes(output), stream=stream, result=result
                )
        except NamedCheckReceiptError, OSError, TypeError, ValueError:
            return _needs_reconciliation()
        return _outcome(intent, receipt, result)

    async def _verified_bytes(self, descriptor: ArtifactDescriptor) -> bytes:
        if descriptor.byte_count < 0 or descriptor.byte_count > 6 * 1024 * 1024 + 1024:
            raise NamedCheckReceiptError()
        if await self._store.verify(descriptor.digest) is not True:
            raise NamedCheckReceiptError()
        data = await self._store.open_bytes(descriptor.digest)
        if (
            len(data) != descriptor.byte_count
            or hashlib.sha256(data).hexdigest() != descriptor.digest
        ):
            raise NamedCheckReceiptError()
        return data

    def _request(self, intent: OperationIntent) -> tuple[dict[str, object], CommandSpec, UUID]:
        keys = {
            "agent_execution_id",
            "command_digest",
            "command_name",
            "environment_keys_digest",
            "head_sha",
            "kind",
            "policy_version",
            "project_id",
            "protocol_version",
            "run_id",
            "step_id",
            "tool_call_id",
            "worktree_id",
        }
        if (
            intent.kind != NAMED_CHECK_KIND
            or intent.request_schema_version != 1
            or set(intent.request_payload) != keys
            or canonical_digest(intent.request_payload) != intent.request_digest
        ):
            raise NamedCheckOperationError("named check request is invalid")
        values = dict(intent.request_payload)
        try:
            for key in ("agent_execution_id", "step_id", "tool_call_id", "project_id", "run_id"):
                value = values[key]
                if not isinstance(value, str):
                    raise TypeError
                parsed = UUID(value)
                if parsed.int == 0 or str(parsed) != value:
                    raise ValueError
            if (
                type(values["protocol_version"]) is not int
                or type(values["policy_version"]) is not int
                or not isinstance(values["head_sha"], str)
                or re.fullmatch(r"[0-9a-f]{40}", values["head_sha"]) is None
            ):
                raise ValueError
            call_id = UUID(str(values["tool_call_id"]))
            command = next(
                item for item in self._policy.commands if item.name == values["command_name"]
            )
            if (
                call_id.int == 0
                or values["run_id"] != str(intent.run_id)
                or values["project_id"] != str(self._worktree.identity.project_id)
                or self._worktree.identity.run_id != intent.run_id
                or values["worktree_id"] != self._worktree.identity.worktree_name
                or values["policy_version"] != self._policy.version
                or values["kind"] != command.kind.value
                or values["command_digest"] != command_spec_digest(command)
                or values["protocol_version"] != 1
                or not isinstance(values["environment_keys_digest"], str)
                or re.fullmatch(r"[0-9a-f]{64}", values["environment_keys_digest"]) is None
                or (
                    self._environment is not None
                    and (
                        not set(self._environment) <= set(command.environment_keys)
                        or values["environment_keys_digest"]
                        != _environment_keys_digest(self._environment)
                    )
                )
            ):
                raise ValueError
        except KeyError, StopIteration, TypeError, ValueError:
            raise NamedCheckOperationError("named check request is invalid") from None
        return values, command, call_id

    def _result_matches(
        self, values: Mapping[str, object], command: CommandSpec, result: CommandResult
    ) -> None:
        if (
            result.command_name != command.name
            or result.kind is not command.kind
            or result.command_digest != command_spec_digest(command)
            or result.policy_version != values["policy_version"]
            or result.runner_mode is not self._policy.runner_mode
            or result.network_enabled
            != effective_network_enabled(self._policy.runner_mode, command.network_enabled)
            or result.unsandboxed is not (self._policy.runner_mode is RunnerMode.TRUSTED_HOST)
        ):
            raise NamedCheckOperationError("named check result is not admitted")

    async def _persist(
        self, intent: OperationIntent, call_id: UUID, result: CommandResult, cancelled: bool
    ) -> OperationOutcome:
        for stream in _STREAMS:
            digest = result.stdout_digest if stream == "stdout" else result.stderr_digest
            if await self._store.verify(digest) is not True:
                raise NamedCheckOperationError("named check output verification failed")
            data = await self._store.open_bytes(digest)
            verify_output_envelope(data, stream=stream, result=result)
            descriptor = await self._store.put_bytes(data, media_type=_OUTPUT_MEDIA)
            await self._artifacts.record(
                descriptor,
                run_id=intent.run_id,
                producer_type="command_output",
                producer_id=intent.run_id,
            )
        result_bytes = encode_command_result(result)
        result_descriptor = await self._store.put_bytes(result_bytes, media_type=_RESULT_MEDIA)
        if (
            result_descriptor.digest != result.evidence_digest
            or result_descriptor.byte_count != len(result_bytes)
            or await self._store.verify(result_descriptor.digest) is not True
        ):
            raise NamedCheckOperationError("named check result digest differs")
        await self._artifacts.record(
            result_descriptor,
            run_id=intent.run_id,
            producer_type="command_result",
            producer_id=call_id,
            parent_digests=tuple(sorted({result.stdout_digest, result.stderr_digest})),
        )
        receipt = _encode_receipt(intent, call_id, result, cancelled)
        receipt_descriptor = await self._store.put_bytes(receipt, media_type=_RECEIPT_MEDIA)
        if (
            receipt_descriptor.byte_count != len(receipt)
            or hashlib.sha256(receipt).hexdigest() != receipt_descriptor.digest
            or await self._store.verify(receipt_descriptor.digest) is not True
        ):
            raise NamedCheckOperationError("named check receipt verification failed")
        await self._artifacts.record(
            receipt_descriptor,
            run_id=intent.run_id,
            producer_type="named_check",
            producer_id=call_id,
            parent_digests=tuple(
                sorted({result.evidence_digest, result.stdout_digest, result.stderr_digest})
            ),
        )
        return _outcome(intent, _decode_receipt(receipt), result)


def _environment_keys_digest(environment: Mapping[str, str]) -> str:
    return hashlib.sha256("\n".join(sorted(environment)).encode()).hexdigest()


def _encode_receipt(
    intent: OperationIntent, call_id: UUID, result: CommandResult, cancelled: bool
) -> bytes:
    payload = {
        "caller_cancelled": cancelled,
        "command_result_digest": result.evidence_digest,
        "intent_id": str(intent.id),
        "receipt_version": 1,
        "request_digest": intent.request_digest,
        "request_payload": dict(intent.request_payload),
        "stderr_digest": result.stderr_digest,
        "stdout_digest": result.stdout_digest,
        "tool_call_id": str(call_id),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _encode_cancelled_receipt(
    intent: OperationIntent, call_id: UUID, values: Mapping[str, object]
) -> bytes:
    return _encode_value(
        {
            "disposition": _CANCELLED_BEFORE_LAUNCH,
            "intent_id": str(intent.id),
            "receipt_version": 1,
            "request_digest": intent.request_digest,
            "request_payload": dict(values),
            "tool_call_id": str(call_id),
        }
    )


def _decode_receipt(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data.decode())
    except UnicodeDecodeError, json.JSONDecodeError:
        raise NamedCheckReceiptError() from None
    if (
        not isinstance(value, dict)
        or frozenset(value) != _RECEIPT_KEYS
        or _encode_value(value) != data
    ):
        raise NamedCheckReceiptError()
    if (
        type(value["caller_cancelled"]) is not bool
        or type(value["receipt_version"]) is not int
        or value["receipt_version"] != 1
    ):
        raise NamedCheckReceiptError()
    if not all(
        isinstance(value[key], str)
        for key in _RECEIPT_KEYS - {"caller_cancelled", "receipt_version", "request_payload"}
    ):
        raise NamedCheckReceiptError()
    if not isinstance(value["request_payload"], dict):
        raise NamedCheckReceiptError()
    try:
        UUID(cast(str, value["intent_id"]))
        UUID(cast(str, value["tool_call_id"]))
        for key in ("command_result_digest", "request_digest", "stdout_digest", "stderr_digest"):
            digest = value[key]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError
    except TypeError, ValueError:
        raise NamedCheckReceiptError() from None
    return value


def _decode_cancelled_receipt(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data.decode())
    except UnicodeDecodeError, json.JSONDecodeError:
        raise NamedCheckReceiptError() from None
    if (
        not isinstance(value, dict)
        or frozenset(value) != _CANCELLED_RECEIPT_KEYS
        or _encode_value(value) != data
        or value.get("disposition") != _CANCELLED_BEFORE_LAUNCH
        or type(value.get("receipt_version")) is not int
        or value["receipt_version"] != 1
        or not isinstance(value.get("request_payload"), dict)
        or not all(
            isinstance(value[key], str)
            for key in _CANCELLED_RECEIPT_KEYS
            - {"disposition", "receipt_version", "request_payload"}
        )
    ):
        raise NamedCheckReceiptError()
    try:
        if (
            UUID(cast(str, value["intent_id"])).int == 0
            or UUID(cast(str, value["tool_call_id"])).int == 0
        ):
            raise ValueError
    except TypeError, ValueError:
        raise NamedCheckReceiptError() from None
    digest = value["request_digest"]
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise NamedCheckReceiptError()
    return value


def _encode_value(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _outcome(
    intent: OperationIntent, receipt: Mapping[str, object], result: CommandResult
) -> OperationOutcome:
    return OperationOutcome(
        payload={
            "caller_cancelled": receipt["caller_cancelled"],
            "command_result_digest": result.evidence_digest,
            "exit_code": result.exit_code,
            "receipt_digest": hashlib.sha256(_encode_value(receipt)).hexdigest(),
            "stderr_digest": result.stderr_digest,
            "stdout_digest": result.stdout_digest,
            "timed_out": result.timed_out,
            "tool_call_id": receipt["tool_call_id"],
        }
    )


def _needs_reconciliation() -> OperationOutcome:
    return OperationOutcome(
        status=OperationStatus.NEEDS_RECONCILIATION,
        error="named check outcome requires reconciliation",
    )


def _is_descriptor(
    descriptor: ArtifactDescriptor,
    run_id: UUID,
    producer_type: str,
    producer_id: UUID,
    media_type: str,
) -> bool:
    return (
        bool(descriptor.digest)
        and descriptor.run_id == run_id
        and descriptor.producer_type == producer_type
        and descriptor.producer_id == producer_id
        and descriptor.media_type == media_type
        and descriptor.schema_version == 1
        and descriptor.truncated is False
        and descriptor.original_byte_count == descriptor.byte_count
        and descriptor.truncation_policy == "none"
    )


__all__ = ["NAMED_CHECK_KIND", "NamedCheckOperationAdapter", "NamedCheckOperationError"]
