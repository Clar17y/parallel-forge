"""Controller-owned validation check operation adapter and request builder.

This adapter executes policy-required validation checks under controller authority.
It operates without inventing AgentExecution or tool call identities, binding the
exact controller step and result identifiers to command evidence and durable receipts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Final, Literal, Self, cast
from uuid import UUID

from forge.application.adapters.named_check import NamedCheckCancellation
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
    RunCommandRequest,
    WorktreeRunnerFactoryPort,
)
from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationRequest,
    OperationStatus,
    canonical_digest,
)
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode
from forge.domain.validation import command_spec_digest, validate_runner_image_reference

CONTROLLER_CHECK_KIND: Final = "controller_named_check"
_STREAMS: Final[tuple[Literal["stdout", "stderr"], ...]] = ("stdout", "stderr")
_RESULT_MEDIA: Final = "application/vnd.forge.command-result+json"
_RECEIPT_MEDIA: Final = "application/vnd.forge.controller-check-receipt+json"
_OUTPUT_MEDIA: Final = "application/vnd.forge.command-output+json"
_CANCELLED_BEFORE_LAUNCH: Final = "cancelled_before_launch"
_MAX_DESCRIPTOR_BYTES: Final = 6 * 1024 * 1024 + 1024
_HEX_64_PATTERN: Final = re.compile(r"\A[0-9a-f]{64}\Z", re.ASCII)
_HEX_40_PATTERN: Final = re.compile(r"\A[0-9a-f]{40}\Z", re.ASCII)
_ENV_KEY_PATTERN: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_CONTROLLER_CHECK_PAYLOAD_KEYS: Final = frozenset(
    {
        "command_digest",
        "command_name",
        "environment_keys_digest",
        "head_sha",
        "kind",
        "policy_version",
        "project_id",
        "protocol_version",
        "result_id",
        "run_id",
        "step_id",
        "worktree_id",
    }
)

_RECEIPT_KEYS: Final = frozenset(
    {
        "caller_cancelled",
        "command_result_digest",
        "intent_id",
        "receipt_version",
        "request_digest",
        "request_payload",
        "result_id",
        "stderr_digest",
        "stdout_digest",
    }
)

_CANCELLED_RECEIPT_KEYS: Final = frozenset(
    {
        "disposition",
        "intent_id",
        "receipt_version",
        "request_digest",
        "request_payload",
        "result_id",
    }
)


class ControllerCheckOperationError(ValueError, RuntimeError):
    """A controller check effect is malformed or lacks durable terminal proof."""


def _environment_keys_digest(environment: Mapping[str, str] | Iterable[str]) -> str:
    if isinstance(environment, Mapping):
        keys = list(environment.keys())
    else:
        keys = list(environment)
    return hashlib.sha256("\n".join(sorted(keys)).encode("utf-8")).hexdigest()


def _extract_and_validate_env_keys(
    *,
    environment: Mapping[str, str] | Iterable[str] | None,
    environment_keys: Iterable[str] | None,
    allowed_keys: tuple[str, ...],
) -> list[str]:
    if environment_keys is not None:
        raw_keys = list(environment_keys)
    elif environment is not None:
        if isinstance(environment, Mapping):
            raw_keys = list(environment.keys())
        else:
            raw_keys = list(environment)
    else:
        raw_keys = []

    for key in raw_keys:
        if not isinstance(key, str) or not _ENV_KEY_PATTERN.fullmatch(key) or "\x00" in key:
            raise ControllerCheckOperationError("invalid environment key")

    if len(set(raw_keys)) != len(raw_keys):
        raise ControllerCheckOperationError("duplicate environment keys")

    if not set(raw_keys).issubset(set(allowed_keys)):
        raise ControllerCheckOperationError("environment contains non-allowlisted keys")

    return sorted(raw_keys)


def controller_check_request(
    *,
    run_id: UUID,
    step_id: UUID,
    result_id: UUID,
    worktree: ManagedWorktree,
    policy: ProjectPolicy,
    command_name: str,
    head_sha: str,
    environment: Mapping[str, str] | Iterable[str] | None = None,
    environment_keys: Iterable[str] | None = None,
) -> OperationRequest:
    """Build a bounded, redacted OperationRequest for a controller check."""
    for uid, name in ((run_id, "run_id"), (step_id, "step_id"), (result_id, "result_id")):
        if not isinstance(uid, UUID) or uid.int == 0:
            raise ControllerCheckOperationError(f"{name} must be a non-nil UUID")

    if not isinstance(worktree, ManagedWorktree):
        raise ControllerCheckOperationError("worktree must be a ManagedWorktree")
    if not isinstance(policy, ProjectPolicy):
        raise ControllerCheckOperationError("policy must be a ProjectPolicy")
    if policy.id != worktree.identity.project_id:
        raise ControllerCheckOperationError("policy project does not match worktree project")
    if worktree.identity.run_id != run_id:
        raise ControllerCheckOperationError("worktree run does not match run_id")

    if not isinstance(head_sha, str) or _HEX_40_PATTERN.fullmatch(head_sha) is None:
        raise ControllerCheckOperationError(
            "head_sha must be 40-character lowercase hexadecimal SHA-1"
        )

    if not isinstance(command_name, str) or not command_name:
        raise ControllerCheckOperationError("command_name must be a non-empty string")

    command = next((c for c in policy.required_checks if c.name == command_name), None)
    if command is None:
        raise ControllerCheckOperationError(
            f"command '{command_name}' is not in policy.required_checks"
        )

    keys = _extract_and_validate_env_keys(
        environment=environment,
        environment_keys=environment_keys,
        allowed_keys=command.environment_keys,
    )
    env_keys_digest = _environment_keys_digest(keys)

    payload: dict[str, object] = {
        "command_digest": command_spec_digest(command),
        "command_name": command.name,
        "environment_keys_digest": env_keys_digest,
        "head_sha": head_sha,
        "kind": command.kind.value,
        "policy_version": policy.version,
        "project_id": str(policy.id),
        "protocol_version": 1,
        "result_id": str(result_id),
        "run_id": str(run_id),
        "step_id": str(step_id),
        "worktree_id": worktree.identity.worktree_name,
    }

    idempotency_key = f"{run_id}:controller-check:{step_id}:{command.name}"
    if len(idempotency_key) > 255:
        raise ControllerCheckOperationError("idempotency key exceeds maximum length")

    return OperationRequest(
        run_id=run_id,
        kind=CONTROLLER_CHECK_KIND,
        idempotency_key=idempotency_key,
        request_digest=canonical_digest(payload),
        request_payload=payload,
        request_schema_version=1,
    )


class ControllerCheckOperationAdapter(OperationAdapter):
    """Run one controller-owned validation check and retain replayable evidence."""

    def __init__(
        self,
        *,
        run_id: UUID,
        step_id: UUID,
        result_id: UUID,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        command_name: str,
        head_sha: str,
        artifacts: ArtifactRepository,
        artifact_store: ArtifactStore | None = None,
        store: ArtifactStore | None = None,
        controlled_git: ControlledGitPort | None = None,
        runner_factory: WorktreeRunnerFactoryPort | None = None,
        environment: Mapping[str, str] | None = None,
        cancellation: NamedCheckCancellation | None = None,
    ) -> None:
        resolved_store = artifact_store if artifact_store is not None else store
        if resolved_store is None:
            raise ControllerCheckOperationError("artifact store is required")

        for uid, name in ((run_id, "run_id"), (step_id, "step_id"), (result_id, "result_id")):
            if not isinstance(uid, UUID) or uid.int == 0:
                raise ControllerCheckOperationError(f"{name} must be a non-nil UUID")

        if not isinstance(worktree, ManagedWorktree):
            raise ControllerCheckOperationError("worktree must be a ManagedWorktree")
        if not isinstance(policy, ProjectPolicy):
            raise ControllerCheckOperationError("policy must be a ProjectPolicy")
        if policy.id != worktree.identity.project_id:
            raise ControllerCheckOperationError("policy project does not match worktree project")
        if worktree.identity.run_id != run_id:
            raise ControllerCheckOperationError("worktree run does not match run_id")

        if not isinstance(head_sha, str) or _HEX_40_PATTERN.fullmatch(head_sha) is None:
            raise ControllerCheckOperationError(
                "head_sha must be 40-character lowercase hexadecimal SHA-1"
            )

        if not isinstance(command_name, str) or not command_name:
            raise ControllerCheckOperationError("command_name must be a non-empty string")

        command = next((c for c in policy.required_checks if c.name == command_name), None)
        if command is None:
            raise ControllerCheckOperationError(
                f"command '{command_name}' is not in policy.required_checks"
            )

        if environment is not None:
            if not isinstance(environment, Mapping):
                raise ControllerCheckOperationError("environment must be a mapping")
            detached = dict(environment)
            for k, v in detached.items():
                if (
                    not isinstance(k, str)
                    or not k
                    or "\x00" in k
                    or not isinstance(v, str)
                    or "\x00" in v
                ):
                    raise ControllerCheckOperationError("invalid environment entry")
            if not set(detached.keys()).issubset(set(command.environment_keys)):
                raise ControllerCheckOperationError("environment contains non-allowlisted keys")
            self._environment: dict[str, str] | None = detached
        else:
            self._environment = None

        self._run_id = run_id
        self._step_id = step_id
        self._result_id = result_id
        self._worktree = worktree
        self._policy = policy
        self._command_name = command_name
        self._command = command
        self._head_sha = head_sha
        self._artifacts = artifacts
        self._store = resolved_store
        self._git = controlled_git
        self._factory = runner_factory
        self._cancellation = cancellation

    @classmethod
    def for_recovery(
        cls,
        *,
        run_id: UUID,
        step_id: UUID,
        result_id: UUID,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        command_name: str,
        head_sha: str,
        artifacts: ArtifactRepository,
        artifact_store: ArtifactStore | None = None,
        store: ArtifactStore | None = None,
    ) -> Self:
        """Verify existing evidence without command execution capabilities."""
        return cls(
            run_id=run_id,
            step_id=step_id,
            result_id=result_id,
            worktree=worktree,
            policy=policy,
            command_name=command_name,
            head_sha=head_sha,
            artifacts=artifacts,
            artifact_store=artifact_store,
            store=store,
            controlled_git=None,
            runner_factory=None,
            environment=None,
            cancellation=None,
        )

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        values, command, result_id = self._request(intent)
        if self._cancellation is not None and self._cancellation.requested:
            return await self.cancel_before_launch(intent)
        if self._git is None or self._factory is None or self._environment is None:
            raise ControllerCheckOperationError("recovery adapter cannot execute commands")
        if self._git.head_sha(self._worktree) != values["head_sha"]:
            raise ControllerCheckOperationError("controller check worktree head changed")

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

        # Recheck git head after terminal effect before accepting result
        if self._git.head_sha(self._worktree) != values["head_sha"]:
            raise ControllerCheckOperationError(
                "controller check worktree head changed during execution"
            )

        result = terminal.result
        self._result_matches(values, command, result)
        return await self._persist(intent, result_id, result, terminal.caller_cancelled)

    async def cancel_before_launch(self, intent: OperationIntent) -> OperationOutcome:
        """Persist canonical evidence that this admission had no command effect."""
        if self._environment is None:
            raise ControllerCheckOperationError("recovery adapter cannot create cancellation proof")
        values, _, result_id = self._request(intent)
        receipt = _encode_cancelled_receipt(intent, result_id, values)
        descriptor = await self._store.put_bytes(receipt, media_type=_RECEIPT_MEDIA)
        if (
            descriptor.byte_count != len(receipt)
            or hashlib.sha256(receipt).hexdigest() != descriptor.digest
            or await self._store.verify(descriptor.digest) is not True
        ):
            raise ControllerCheckOperationError(
                "controller check cancellation receipt verification failed"
            )
        await self._artifacts.record(
            descriptor,
            run_id=intent.run_id,
            producer_type=CONTROLLER_CHECK_KIND,
            producer_id=result_id,
        )
        return OperationOutcome(
            payload={
                "disposition": _CANCELLED_BEFORE_LAUNCH,
                "receipt_digest": descriptor.digest,
                "result_id": str(result_id),
            }
        )

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        try:
            values, command, result_id = self._request(intent)
        except ControllerCheckOperationError:
            return _needs_reconciliation()

        receipts = await self._artifacts.get_by_producer(
            run_id=intent.run_id, producer_type=CONTROLLER_CHECK_KIND, producer_id=result_id
        )
        if len(receipts) != 1:
            return _needs_reconciliation()
        descriptor = receipts[0]
        try:
            if not _is_descriptor(
                descriptor, intent.run_id, CONTROLLER_CHECK_KIND, result_id, _RECEIPT_MEDIA
            ):
                raise ControllerCheckOperationError()
            receipt_data = await self._verified_bytes(descriptor)
            if descriptor.parent_digests == ():
                cancelled = _decode_cancelled_receipt(receipt_data)
                if (
                    cancelled["intent_id"] != str(intent.id)
                    or cancelled["request_digest"] != intent.request_digest
                    or canonical_digest(cast(Mapping[str, object], cancelled["request_payload"]))
                    != intent.request_digest
                    or cancelled["result_id"] != str(result_id)
                ):
                    raise ControllerCheckOperationError()
                return OperationOutcome(
                    payload={
                        "disposition": _CANCELLED_BEFORE_LAUNCH,
                        "receipt_digest": descriptor.digest,
                        "result_id": str(result_id),
                    }
                )
            receipt = _decode_receipt(receipt_data)
            if (
                receipt["intent_id"] != str(intent.id)
                or receipt["request_digest"] != intent.request_digest
            ):
                raise ControllerCheckOperationError()
            if canonical_digest(
                cast(Mapping[str, object], receipt["request_payload"])
            ) != intent.request_digest or receipt["result_id"] != str(result_id):
                raise ControllerCheckOperationError()
            command_result_digest = cast(str, receipt["command_result_digest"])
            stdout_digest = cast(str, receipt["stdout_digest"])
            stderr_digest = cast(str, receipt["stderr_digest"])
            parents = tuple(sorted({command_result_digest, stdout_digest, stderr_digest}))
            if descriptor.parent_digests != parents:
                raise ControllerCheckOperationError()
            result_descriptor = await self._artifacts.get_by_digest(
                command_result_digest, run_id=intent.run_id
            )
            if not _is_descriptor(
                result_descriptor, intent.run_id, "command_result", result_id, _RESULT_MEDIA
            ):
                raise ControllerCheckOperationError()
            result = decode_command_result(await self._verified_bytes(result_descriptor))
            if (
                result_descriptor.digest != command_result_digest
                or result.evidence_digest != command_result_digest
                or result.stdout_digest != stdout_digest
                or result.stderr_digest != stderr_digest
                or result_descriptor.parent_digests != tuple(sorted({stdout_digest, stderr_digest}))
            ):
                raise ControllerCheckOperationError()
            self._result_matches(values, command, result)
            for stream in _STREAMS:
                digest = result.stdout_digest if stream == "stdout" else result.stderr_digest
                output = await self._artifacts.get_by_digest(digest, run_id=intent.run_id)
                if not _is_descriptor(
                    output, intent.run_id, "command_output", intent.run_id, _OUTPUT_MEDIA
                ):
                    raise ControllerCheckOperationError()
                if output.digest != digest or output.parent_digests:
                    raise ControllerCheckOperationError()
                verify_output_envelope(
                    await self._verified_bytes(output), stream=stream, result=result
                )
        except (
            NamedCheckReceiptError,
            ControllerCheckOperationError,
            OSError,
            TypeError,
            ValueError,
        ):
            return _needs_reconciliation()
        return _outcome(intent, receipt, result)

    async def _verified_bytes(self, descriptor: ArtifactDescriptor) -> bytes:
        if descriptor.byte_count < 0 or descriptor.byte_count > _MAX_DESCRIPTOR_BYTES:
            raise ControllerCheckOperationError("invalid descriptor byte count")
        if await self._store.verify(descriptor.digest) is not True:
            raise ControllerCheckOperationError("artifact store verification failed")
        data = await self._store.open_bytes(descriptor.digest)
        if (
            len(data) != descriptor.byte_count
            or hashlib.sha256(data).hexdigest() != descriptor.digest
        ):
            raise ControllerCheckOperationError("descriptor content integrity failed")
        return data

    def _request(self, intent: OperationIntent) -> tuple[dict[str, object], CommandSpec, UUID]:
        if (
            intent.kind != CONTROLLER_CHECK_KIND
            or intent.request_schema_version != 1
            or intent.run_id != self._run_id
            or frozenset(intent.request_payload.keys()) != _CONTROLLER_CHECK_PAYLOAD_KEYS
            or canonical_digest(intent.request_payload) != intent.request_digest
        ):
            raise ControllerCheckOperationError("controller check request is invalid")

        values = dict(intent.request_payload)
        try:
            for key in ("step_id", "result_id", "project_id", "run_id"):
                val = values[key]
                if not isinstance(val, str):
                    raise TypeError
                parsed = UUID(val)
                if parsed.int == 0 or str(parsed) != val:
                    raise ValueError

            for ver_key in ("protocol_version", "policy_version"):
                v = values[ver_key]
                if type(v) is not int or isinstance(v, bool):
                    raise TypeError

            head = values["head_sha"]
            if not isinstance(head, str) or _HEX_40_PATTERN.fullmatch(head) is None:
                raise ValueError

            cmd_digest = values["command_digest"]
            if not isinstance(cmd_digest, str) or _HEX_64_PATTERN.fullmatch(cmd_digest) is None:
                raise ValueError

            env_digest = values["environment_keys_digest"]
            if not isinstance(env_digest, str) or _HEX_64_PATTERN.fullmatch(env_digest) is None:
                raise ValueError

            # Verify against frozen constructor authority
            if (
                values["run_id"] != str(self._run_id)
                or values["step_id"] != str(self._step_id)
                or values["result_id"] != str(self._result_id)
                or values["project_id"] != str(self._policy.id)
                or values["worktree_id"] != self._worktree.identity.worktree_name
                or values["command_name"] != self._command_name
                or values["head_sha"] != self._head_sha
                or values["protocol_version"] != 1
                or values["policy_version"] != self._policy.version
                or values["kind"] != self._command.kind.value
                or values["command_digest"] != command_spec_digest(self._command)
            ):
                raise ValueError

            if self._environment is not None:
                if values["environment_keys_digest"] != _environment_keys_digest(self._environment):
                    raise ValueError
                if not set(self._environment.keys()).issubset(set(self._command.environment_keys)):
                    raise ValueError
        except (KeyError, TypeError, ValueError) as err:
            raise ControllerCheckOperationError(
                "controller check request authority mismatch"
            ) from err

        return values, self._command, self._result_id

    def _result_matches(
        self, values: Mapping[str, object], command: CommandSpec, result: CommandResult
    ) -> None:
        if (
            result.command_name != command.name
            or result.kind is not command.kind
            or result.command_digest != command_spec_digest(command)
            or result.policy_version != values["policy_version"]
            or result.runner_mode is not self._policy.runner_mode
            or result.network_enabled != command.network_enabled
            or result.unsandboxed is not (self._policy.runner_mode is RunnerMode.TRUSTED_HOST)
        ):
            raise ControllerCheckOperationError("controller check result is not admitted")

        if self._policy.runner_mode is RunnerMode.DOCKER:
            if not result.image_digest:
                raise ControllerCheckOperationError(
                    "Docker command evidence requires an image digest"
                )
            try:
                validate_runner_image_reference(result.image_digest)
            except (ValueError, TypeError) as err:
                raise ControllerCheckOperationError(f"invalid runner image: {err}") from err
        elif self._policy.runner_mode is RunnerMode.TRUSTED_HOST:
            if result.image_digest is not None:
                raise ControllerCheckOperationError(
                    "trusted-host command evidence has no container image"
                )
            if not result.unsandboxed:
                raise ControllerCheckOperationError(
                    "trusted-host command evidence must disclose unsandboxed"
                )

    async def _persist(
        self, intent: OperationIntent, result_id: UUID, result: CommandResult, cancelled: bool
    ) -> OperationOutcome:
        for stream in _STREAMS:
            digest = result.stdout_digest if stream == "stdout" else result.stderr_digest
            if await self._store.verify(digest) is not True:
                raise ControllerCheckOperationError("controller check output verification failed")
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
            raise ControllerCheckOperationError("controller check result digest differs")
        await self._artifacts.record(
            result_descriptor,
            run_id=intent.run_id,
            producer_type="command_result",
            producer_id=result_id,
            parent_digests=tuple(sorted({result.stdout_digest, result.stderr_digest})),
        )

        receipt = _encode_receipt(intent, result_id, result, cancelled)
        receipt_descriptor = await self._store.put_bytes(receipt, media_type=_RECEIPT_MEDIA)
        if (
            receipt_descriptor.byte_count != len(receipt)
            or hashlib.sha256(receipt).hexdigest() != receipt_descriptor.digest
            or await self._store.verify(receipt_descriptor.digest) is not True
        ):
            raise ControllerCheckOperationError("controller check receipt verification failed")
        await self._artifacts.record(
            receipt_descriptor,
            run_id=intent.run_id,
            producer_type=CONTROLLER_CHECK_KIND,
            producer_id=result_id,
            parent_digests=tuple(
                sorted({result.evidence_digest, result.stdout_digest, result.stderr_digest})
            ),
        )
        return _outcome(intent, _decode_receipt(receipt), result)


def _encode_value(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _encode_receipt(
    intent: OperationIntent, result_id: UUID, result: CommandResult, cancelled: bool
) -> bytes:
    payload = {
        "caller_cancelled": cancelled,
        "command_result_digest": result.evidence_digest,
        "intent_id": str(intent.id),
        "receipt_version": 1,
        "request_digest": intent.request_digest,
        "request_payload": dict(intent.request_payload),
        "result_id": str(result_id),
        "stderr_digest": result.stderr_digest,
        "stdout_digest": result.stdout_digest,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _encode_cancelled_receipt(
    intent: OperationIntent, result_id: UUID, values: Mapping[str, object]
) -> bytes:
    return _encode_value(
        {
            "disposition": _CANCELLED_BEFORE_LAUNCH,
            "intent_id": str(intent.id),
            "receipt_version": 1,
            "request_digest": intent.request_digest,
            "request_payload": dict(values),
            "result_id": str(result_id),
        }
    )


def _decode_receipt(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        raise ControllerCheckOperationError("invalid receipt json") from None
    if (
        not isinstance(value, dict)
        or frozenset(value.keys()) != _RECEIPT_KEYS
        or _encode_value(value) != data
    ):
        raise ControllerCheckOperationError("receipt keys or encoding invalid")
    if (
        type(value["caller_cancelled"]) is not bool
        or type(value["receipt_version"]) is not int
        or isinstance(value["receipt_version"], bool)
        or value["receipt_version"] != 1
    ):
        raise ControllerCheckOperationError("receipt version or cancellation marker invalid")
    if not all(
        isinstance(value[key], str)
        for key in _RECEIPT_KEYS - {"caller_cancelled", "receipt_version", "request_payload"}
    ):
        raise ControllerCheckOperationError("receipt string fields invalid")
    if not isinstance(value["request_payload"], dict):
        raise ControllerCheckOperationError("receipt request payload invalid")
    try:
        if (
            UUID(cast(str, value["intent_id"])).int == 0
            or UUID(cast(str, value["result_id"])).int == 0
        ):
            raise ValueError
        for key in ("command_result_digest", "request_digest", "stdout_digest", "stderr_digest"):
            digest = value[key]
            if not isinstance(digest, str) or _HEX_64_PATTERN.fullmatch(digest) is None:
                raise ValueError
    except (TypeError, ValueError) as err:
        raise ControllerCheckOperationError("receipt field format invalid") from err
    return value


def _decode_cancelled_receipt(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        raise ControllerCheckOperationError("invalid cancelled receipt json") from None
    if (
        not isinstance(value, dict)
        or frozenset(value.keys()) != _CANCELLED_RECEIPT_KEYS
        or _encode_value(value) != data
        or value.get("disposition") != _CANCELLED_BEFORE_LAUNCH
        or type(value.get("receipt_version")) is not int
        or isinstance(value.get("receipt_version"), bool)
        or value["receipt_version"] != 1
        or not isinstance(value.get("request_payload"), dict)
        or not all(
            isinstance(value[key], str)
            for key in _CANCELLED_RECEIPT_KEYS
            - {"disposition", "receipt_version", "request_payload"}
        )
    ):
        raise ControllerCheckOperationError("cancelled receipt keys or types invalid")
    try:
        if (
            UUID(cast(str, value["intent_id"])).int == 0
            or UUID(cast(str, value["result_id"])).int == 0
        ):
            raise ValueError
        digest = value["request_digest"]
        if not isinstance(digest, str) or _HEX_64_PATTERN.fullmatch(digest) is None:
            raise ValueError
    except (TypeError, ValueError) as err:
        raise ControllerCheckOperationError("cancelled receipt fields invalid") from err
    return value


def _outcome(
    intent: OperationIntent, receipt: Mapping[str, object], result: CommandResult
) -> OperationOutcome:
    return OperationOutcome(
        payload={
            "caller_cancelled": receipt["caller_cancelled"],
            "command_result_digest": result.evidence_digest,
            "exit_code": result.exit_code,
            "receipt_digest": hashlib.sha256(_encode_value(receipt)).hexdigest(),
            "result_id": str(receipt["result_id"]),
            "stderr_digest": result.stderr_digest,
            "stdout_digest": result.stdout_digest,
            "timed_out": result.timed_out,
        }
    )


def _needs_reconciliation() -> OperationOutcome:
    return OperationOutcome(
        status=OperationStatus.NEEDS_RECONCILIATION,
        error="controller check outcome requires reconciliation",
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


__all__ = [
    "CONTROLLER_CHECK_KIND",
    "ControllerCheckOperationAdapter",
    "ControllerCheckOperationError",
    "controller_check_request",
]
