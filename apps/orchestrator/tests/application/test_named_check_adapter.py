from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from forge.application.adapters.named_check import (
    NAMED_CHECK_KIND,
    NamedCheckCancellation,
    NamedCheckOperationAdapter,
    NamedCheckOperationError,
    NamedCheckReceiptError,
    _decode_receipt,
    _environment_keys_digest,
)
from forge.application.ports.runner import CommandResult, CommandTerminalResult
from forge.application.ports.worktrees import ManagedWorktree
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.operation import OperationIntent, OperationStatus, canonical_digest
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode, StepKind
from forge.domain.resource import WorktreeIdentity
from forge.domain.validation import command_spec_digest
from forge.persistence.repositories.artifacts import ArtifactRepository


class _Unused:
    def __getattr__(self, name: str):
        raise AssertionError(f"unexpected adapter dependency use: {name}")


def _adapter_and_intent(
    *, command_name: str = "unit"
) -> tuple[NamedCheckOperationAdapter, OperationIntent]:
    project_id = uuid4()
    run_id = uuid4()
    identity = WorktreeIdentity.for_run(project_id, run_id, "forge/test", False)
    worktree = ManagedWorktree(
        identity=identity, path=Path("C:/managed/forge-test"), base_sha="a" * 40
    )
    command = CommandSpec(
        kind=StepKind.TEST,
        name="unit",
        argv=("pytest",),
        timeout_seconds=30,
        environment_keys=("TOKEN",),
    )
    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path="C:/repo",
        github_repository="owner/repo",
        default_branch="main",
        commands=(command,),
    )
    environment = {"TOKEN": "transient-value"}
    payload: dict[str, object] = {
        "agent_execution_id": str(uuid4()),
        "command_digest": command_spec_digest(command),
        "command_name": command_name,
        "environment_keys_digest": _environment_keys_digest(environment),
        "head_sha": "b" * 40,
        "kind": StepKind.TEST.value,
        "policy_version": 1,
        "project_id": str(project_id),
        "protocol_version": 1,
        "run_id": str(run_id),
        "step_id": str(uuid4()),
        "tool_call_id": str(uuid4()),
        "worktree_id": identity.worktree_name,
    }
    intent = OperationIntent(
        run_id=run_id,
        kind=NAMED_CHECK_KIND,
        idempotency_key=f"named-check:{uuid4().hex}",
        request_digest=canonical_digest(payload),
        request_payload=payload,
    )
    return NamedCheckOperationAdapter(
        worktree=worktree,
        policy=policy,
        controlled_git=_Unused(),
        runner_factory=_Unused(),
        environment=environment,
        artifacts=_Unused(),
        artifact_store=_Unused(),
    ), intent


def test_request_is_closed_and_does_not_persist_environment_values() -> None:
    adapter, intent = _adapter_and_intent()

    values, command, _ = adapter._request(intent)

    assert command.name == "unit"
    assert values["environment_keys_digest"] == _environment_keys_digest({"TOKEN": "anything"})
    assert "transient-value" not in repr(values)


def test_unknown_command_is_rejected_before_runner_creation() -> None:
    adapter, intent = _adapter_and_intent(command_name="unknown")

    with pytest.raises(NamedCheckOperationError):
        adapter._request(intent)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol_version", True),
        ("policy_version", True),
        ("agent_execution_id", "not-an-id"),
        ("step_id", "00000000-0000-0000-0000-000000000000"),
        ("head_sha", "not-a-sha"),
    ],
)
def test_request_rejects_malformed_authority(field, value) -> None:
    adapter, intent = _adapter_and_intent()
    payload = {**intent.request_payload, field: value}
    malformed = replace(intent, request_payload=payload, request_digest=canonical_digest(payload))
    with pytest.raises(NamedCheckOperationError):
        adapter._request(malformed)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.__setitem__("receipt_version", True),
        lambda value: value.__setitem__("intent_id", "not-a-uuid"),
        lambda value: value.__setitem__("stdout_digest", "A" * 64),
        lambda value: value.__setitem__("unexpected", "value"),
    ),
)
def test_recovery_receipt_decoder_rejects_tampered_closed_schema(mutate) -> None:
    _, intent = _adapter_and_intent()
    receipt = {
        "caller_cancelled": False,
        "command_result_digest": "a" * 64,
        "intent_id": str(intent.id),
        "receipt_version": 1,
        "request_digest": intent.request_digest,
        "request_payload": dict(intent.request_payload),
        "stderr_digest": "b" * 64,
        "stdout_digest": "c" * 64,
        "tool_call_id": str(intent.request_payload["tool_call_id"]),
    }
    mutate(receipt)
    data = json.dumps(receipt, separators=(",", ":"), sort_keys=True).encode()

    with pytest.raises(NamedCheckReceiptError):
        _decode_receipt(data)


class _Git:
    def __init__(self, head: str) -> None:
        self.head = head

    def head_sha(self, worktree: ManagedWorktree) -> str:
        return self.head


class _Runner:
    def __init__(self, terminal: CommandTerminalResult) -> None:
        self.terminal = terminal
        self.calls = 0

    async def run_terminal(self, request) -> CommandTerminalResult:
        self.calls += 1
        return self.terminal


class _Factory:
    def __init__(self, runner: _Runner) -> None:
        self.runner = runner
        self.calls = 0

    def create(self, worktree: ManagedWorktree, policy: ProjectPolicy) -> _Runner:
        self.calls += 1
        return self.runner


async def _real_adapter(
    tmp_path,
    persisted_run,
    session_factory,
    *,
    exit_code: int | None,
    timed_out: bool,
    cancelled: bool,
) -> tuple[NamedCheckOperationAdapter, OperationIntent, _Factory]:
    project_id = persisted_run.project_id
    identity = WorktreeIdentity.for_run(project_id, persisted_run.id, "forge/test", False)
    worktree = ManagedWorktree(
        identity=identity, path=Path("C:/managed/forge-test"), base_sha="a" * 40
    )
    command = CommandSpec(kind=StepKind.TEST, name="unit", argv=("pytest",), timeout_seconds=30)
    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path="C:/repo",
        github_repository="owner/repo",
        default_branch="main",
        commands=(command,),
    )
    store = FilesystemArtifactStore(tmp_path)
    envelopes = []
    for stream in ("stdout", "stderr"):
        envelopes.append(
            json.dumps(
                {
                    "captured_byte_count": 0,
                    "encoding": "utf-8-replacement",
                    "original_byte_count": 0,
                    "stream": stream,
                    "text": "",
                    "truncated": False,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
    stdout, stderr = envelopes
    stdout_digest, stderr_digest = (hashlib.sha256(item).hexdigest() for item in envelopes)
    await store.put_bytes(stdout, media_type="application/vnd.forge.command-output+json")
    await store.put_bytes(stderr, media_type="application/vnd.forge.command-output+json")
    result = CommandResult(
        command_name="unit",
        kind=StepKind.TEST,
        command_digest=command_spec_digest(command),
        policy_version=1,
        exit_code=exit_code,
        timed_out=timed_out,
        started_at=datetime.now(UTC),
        duration_ms=1,
        stdout_digest=stdout_digest,
        stderr_digest=stderr_digest,
        runner_mode=RunnerMode.DOCKER,
        image_digest="sha256:" + "d" * 64,
        network_enabled=False,
        stdout_original_byte_count=0,
        stderr_original_byte_count=0,
        stdout_truncated=False,
        stderr_truncated=False,
        unsandboxed=False,
    )
    terminal = CommandTerminalResult(result=result, caller_cancelled=cancelled)
    factory = _Factory(_Runner(terminal))
    payload: dict[str, object] = {
        "agent_execution_id": str(uuid4()),
        "command_digest": command_spec_digest(command),
        "command_name": "unit",
        "environment_keys_digest": _environment_keys_digest({}),
        "head_sha": "b" * 40,
        "kind": "test",
        "policy_version": 1,
        "project_id": str(project_id),
        "protocol_version": 1,
        "run_id": str(persisted_run.id),
        "step_id": str(uuid4()),
        "tool_call_id": str(uuid4()),
        "worktree_id": identity.worktree_name,
    }
    intent = OperationIntent(
        run_id=persisted_run.id,
        kind=NAMED_CHECK_KIND,
        idempotency_key=f"named:{uuid4().hex}",
        request_digest=canonical_digest(payload),
        request_payload=payload,
    )
    return (
        NamedCheckOperationAdapter(
            worktree=worktree,
            policy=policy,
            controlled_git=_Git("b" * 40),
            runner_factory=factory,
            environment={},
            artifacts=ArtifactRepository(session_factory),
            artifact_store=store,
        ),
        intent,
        factory,
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("exit_code", "timed_out", "cancelled"),
    ((0, False, False), (1, False, False), (None, True, False), (0, False, True)),
)
async def test_adapter_persists_terminal_receipt_and_reconciles_without_rerun(
    tmp_path, persisted_run, session_factory, exit_code, timed_out, cancelled
) -> None:
    adapter, intent, factory = await _real_adapter(
        tmp_path,
        persisted_run,
        session_factory,
        exit_code=exit_code,
        timed_out=timed_out,
        cancelled=cancelled,
    )
    outcome = await adapter.invoke(intent)
    recovered = await adapter.reconcile(intent)
    assert recovered == outcome
    assert factory.calls == 1
    assert factory.runner.calls == 1


@pytest.mark.integration
async def test_cancel_before_launch_persists_replayable_no_effect_receipt(
    tmp_path, persisted_run, session_factory
) -> None:
    adapter, intent, factory = await _real_adapter(
        tmp_path, persisted_run, session_factory, exit_code=0, timed_out=False, cancelled=False
    )

    outcome = await adapter.cancel_before_launch(intent)
    recovered = await adapter.reconcile(intent)

    assert recovered == outcome
    assert outcome.payload["disposition"] == "cancelled_before_launch"
    assert factory.calls == factory.runner.calls == 0


@pytest.mark.integration
async def test_changed_head_rejects_before_factory_creation(
    tmp_path, persisted_run, session_factory
) -> None:
    adapter, intent, factory = await _real_adapter(
        tmp_path, persisted_run, session_factory, exit_code=0, timed_out=False, cancelled=False
    )
    adapter._git.head = "c" * 40
    with pytest.raises(NamedCheckOperationError):
        await adapter.invoke(intent)
    assert factory.calls == 0


@pytest.mark.integration
async def test_reconcile_missing_or_ambiguous_receipt_never_launches_runner(
    tmp_path, persisted_run, session_factory
) -> None:
    adapter, intent, factory = await _real_adapter(
        tmp_path, persisted_run, session_factory, exit_code=0, timed_out=False, cancelled=False
    )
    assert (await adapter.reconcile(intent)).status is OperationStatus.NEEDS_RECONCILIATION
    assert factory.calls == 0


@pytest.mark.integration
async def test_reconcile_rejects_receipt_stream_pointer_different_from_result(
    tmp_path, persisted_run, session_factory
) -> None:
    adapter, intent, factory = await _real_adapter(
        tmp_path, persisted_run, session_factory, exit_code=0, timed_out=False, cancelled=False
    )
    outcome = await adapter.invoke(intent)
    receipt = json.loads(await adapter._store.open_bytes(outcome.payload["receipt_digest"]))
    receipt["stdout_digest"] = receipt["stderr_digest"]
    data = json.dumps(receipt, separators=(",", ":"), sort_keys=True).encode()
    descriptor = await adapter._store.put_bytes(
        data, media_type="application/vnd.forge.named-check-receipt+json"
    )
    descriptor = replace(
        descriptor,
        run_id=intent.run_id,
        producer_type="named_check",
        producer_id=UUID(str(intent.request_payload["tool_call_id"])),
        parent_digests=tuple(
            sorted(
                {
                    receipt["command_result_digest"],
                    receipt["stdout_digest"],
                    receipt["stderr_digest"],
                }
            )
        ),
    )
    original = adapter._artifacts

    class SubstitutedReceipt:
        async def get_by_producer(self, **kwargs):
            return (descriptor,)

        async def get_by_digest(self, digest, *, run_id):
            return await original.get_by_digest(digest, run_id=run_id)

    adapter._artifacts = SubstitutedReceipt()
    assert (await adapter.reconcile(intent)).status is OperationStatus.NEEDS_RECONCILIATION
    assert factory.calls == factory.runner.calls == 1


async def test_recovery_adapter_cannot_execute_or_create_no_effect_proof():
    admitted, intent = _adapter_and_intent()
    recovery = NamedCheckOperationAdapter.for_recovery(
        worktree=admitted._worktree,
        policy=admitted._policy,
        artifacts=_Unused(),
        artifact_store=_Unused(),
    )
    # The admitted intent binds a nonempty environment; recovery needs no values.
    with pytest.raises(NamedCheckOperationError, match="cannot execute"):
        await recovery.invoke(intent)
    with pytest.raises(NamedCheckOperationError, match="cannot create cancellation"):
        await recovery.cancel_before_launch(intent)


async def test_cancellation_before_runner_first_step_has_canonical_no_effect_outcome(
    tmp_path, persisted_run, session_factory
) -> None:
    adapter, intent, factory = await _real_adapter(
        tmp_path, persisted_run, session_factory, exit_code=0, timed_out=False, cancelled=False
    )
    cancellation = NamedCheckCancellation()
    adapter._cancellation = cancellation
    cancellation.request()

    outcome = await adapter.invoke(intent)

    assert outcome.payload["disposition"] == "cancelled_before_launch"
    assert factory.calls == factory.runner.calls == 0


@pytest.mark.integration
async def test_cancellation_during_runner_prelaunch_yields_canonical_no_effect(
    tmp_path, persisted_run, session_factory
) -> None:
    adapter, intent, factory = await _real_adapter(
        tmp_path, persisted_run, session_factory, exit_code=0, timed_out=False, cancelled=False
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    class PrelaunchRunner:
        calls = 0

        async def run_terminal(self, request):  # type: ignore[no-untyped-def]
            self.calls += 1
            entered.set()
            await release.wait()
            assert request.launch_ownership is not None
            request.launch_ownership.accept_launch()
            return factory.runner.terminal

    runner = PrelaunchRunner()
    factory.runner = runner
    cancellation = NamedCheckCancellation()
    adapter._cancellation = cancellation
    invocation = asyncio.create_task(adapter.invoke(intent))
    await asyncio.wait_for(entered.wait(), 5)
    cancellation.request()
    outcome = await asyncio.wait_for(invocation, 5)

    assert outcome.payload["disposition"] == "cancelled_before_launch"
    assert factory.calls == runner.calls == 1


@pytest.mark.parametrize("no_effect", [False, True])
async def test_reconcile_rejects_boolean_substitution_in_receipt_authority(
    tmp_path, persisted_run, session_factory, no_effect
):
    adapter, intent, factory = await _real_adapter(
        tmp_path, persisted_run, session_factory, exit_code=0, timed_out=False, cancelled=False
    )
    outcome = (
        await adapter.cancel_before_launch(intent) if no_effect else await adapter.invoke(intent)
    )
    call_id = UUID(str(intent.request_payload["tool_call_id"]))
    (original_descriptor,) = await adapter._artifacts.get_by_producer(
        run_id=intent.run_id, producer_type="named_check", producer_id=call_id
    )
    receipt = json.loads(await adapter._store.open_bytes(outcome.payload["receipt_digest"]))
    receipt["request_payload"]["policy_version"] = True
    data = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    stored = await adapter._store.put_bytes(data, media_type=original_descriptor.media_type)
    descriptor = replace(
        stored,
        run_id=intent.run_id,
        producer_type="named_check",
        producer_id=call_id,
        parent_digests=original_descriptor.parent_digests,
    )
    original = adapter._artifacts

    class SubstitutedReceipt:
        async def get_by_producer(self, **kwargs):
            return (descriptor,)

        async def get_by_digest(self, digest, *, run_id):
            return await original.get_by_digest(digest, run_id=run_id)

    adapter._artifacts = SubstitutedReceipt()
    assert (await adapter.reconcile(intent)).status is OperationStatus.NEEDS_RECONCILIATION
    assert factory.calls == (0 if no_effect else 1)


async def test_created_runner_task_cancelled_before_first_step_has_no_launch_proof(monkeypatch):
    from forge.application.ports.runner import RunCommandRequest

    cancellation = NamedCheckCancellation()
    entered = False

    class Runner:
        async def run_terminal(self, request):
            nonlocal entered
            entered = True
            raise AssertionError("cancelled task must not enter runner")

    create_task = asyncio.create_task

    def cancel_before_first_step(coroutine, *args, **kwargs):
        task = create_task(coroutine, *args, **kwargs)
        cancellation.request()
        task.cancel()
        return task

    monkeypatch.setattr(asyncio, "create_task", cancel_before_first_step)
    request = RunCommandRequest(
        command_name="unit", kind=StepKind.TEST, launch_ownership=cancellation.ownership
    )
    assert await cancellation.run(Runner(), request) is None
    assert entered is False
    assert cancellation.ownership.accepted is False


async def test_escaped_cancellation_after_launch_ownership_is_never_no_effect():
    from forge.application.ports.runner import RunCommandRequest

    cancellation = NamedCheckCancellation()

    class Runner:
        async def run_terminal(self, request):
            request.launch_ownership.accept_launch()
            cancellation.request()
            raise asyncio.CancelledError()

    request = RunCommandRequest(
        command_name="unit", kind=StepKind.TEST, launch_ownership=cancellation.ownership
    )
    with pytest.raises(asyncio.CancelledError):
        await cancellation.run(Runner(), request)
    assert cancellation.ownership.accepted is True
