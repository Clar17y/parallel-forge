"""Tests for ControllerCheckOperationAdapter and controller_check_request."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from forge.application.adapters.controller_check import (
    CONTROLLER_CHECK_KIND,
    ControllerCheckOperationAdapter,
    ControllerCheckOperationError,
    controller_check_request,
)
from forge.application.adapters.named_check import NamedCheckCancellation
from forge.application.ports.runner import CommandResult, CommandTerminalResult
from forge.application.ports.worktrees import ManagedWorktree
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.operation import OperationIntent, OperationStatus, canonical_digest
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode, StepKind
from forge.domain.resource import WorktreeIdentity
from forge.domain.validation import command_spec_digest
from forge.persistence.repositories.artifacts import ArtifactRepository


class _Unused:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected dependency call: {name}")


class _Git:
    def __init__(self, head: str) -> None:
        self.head = head

    def head_sha(self, worktree: ManagedWorktree) -> str:
        return self.head


class _Runner:
    def __init__(self, terminal: CommandTerminalResult) -> None:
        self.terminal = terminal
        self.calls = 0

    async def run_terminal(self, request: Any) -> CommandTerminalResult:
        self.calls += 1
        return self.terminal


class _Factory:
    def __init__(self, runner: _Runner) -> None:
        self.runner = runner
        self.calls = 0

    def create(self, worktree: ManagedWorktree, policy: ProjectPolicy) -> _Runner:
        self.calls += 1
        return self.runner


def _make_fixture_context(
    *,
    command_name: str = "unit",
    command_required: bool = True,
    environment: Mapping[str, str] | None = None,
) -> tuple[ManagedWorktree, ProjectPolicy, UUID, UUID, UUID, str]:
    project_id = uuid4()
    run_id = uuid4()
    step_id = uuid4()
    result_id = uuid4()
    head_sha = "a" * 40
    identity = WorktreeIdentity.for_run(project_id, run_id, "forge/test", False)
    worktree = ManagedWorktree(
        identity=identity, path=Path("/managed/forge-test").resolve(), base_sha=head_sha
    )
    command = CommandSpec(
        kind=StepKind.TEST,
        name=command_name,
        argv=("pytest", "-q"),
        timeout_seconds=30,
        required=command_required,
        environment_keys=("TEST_ENV",) if environment else (),
    )
    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path=str(Path("/repo").resolve()),
        github_repository="owner/repo",
        default_branch="main",
        commands=(command,),
    )
    return worktree, policy, run_id, step_id, result_id, head_sha


def test_controller_check_request_builder_happy_path() -> None:
    worktree, policy, run_id, step_id, result_id, head_sha = _make_fixture_context(
        environment={"TEST_ENV": "secret-value"}
    )
    request = controller_check_request(
        run_id=run_id,
        step_id=step_id,
        result_id=result_id,
        worktree=worktree,
        policy=policy,
        command_name="unit",
        head_sha=head_sha,
        environment={"TEST_ENV": "secret-value"},
    )
    assert request.kind == CONTROLLER_CHECK_KIND
    assert request.run_id == run_id
    assert request.idempotency_key == f"{run_id}:controller-check:{step_id}:unit"
    assert request.request_schema_version == 1

    payload = request.request_payload
    assert payload["run_id"] == str(run_id)
    assert payload["project_id"] == str(policy.id)
    assert payload["step_id"] == str(step_id)
    assert payload["result_id"] == str(result_id)
    assert payload["worktree_id"] == worktree.identity.worktree_name
    assert payload["policy_version"] == 1
    assert payload["command_name"] == "unit"
    assert payload["kind"] == "test"
    assert payload["head_sha"] == head_sha
    assert payload["protocol_version"] == 1
    assert "agent_execution_id" not in payload
    assert "tool_call_id" not in payload
    assert "secret-value" not in json.dumps(dict(payload))


def test_controller_check_request_rejects_optional_command() -> None:
    worktree, policy, run_id, step_id, result_id, head_sha = _make_fixture_context(
        command_name="optional_check", command_required=False
    )
    with pytest.raises(ControllerCheckOperationError, match="policy.required_checks"):
        controller_check_request(
            run_id=run_id,
            step_id=step_id,
            result_id=result_id,
            worktree=worktree,
            policy=policy,
            command_name="optional_check",
            head_sha=head_sha,
        )


def test_controller_check_request_rejects_nil_uuid() -> None:
    worktree, policy, run_id, step_id, _, head_sha = _make_fixture_context()
    nil_uuid = UUID(int=0)
    with pytest.raises(ControllerCheckOperationError, match="non-nil UUID"):
        controller_check_request(
            run_id=run_id,
            step_id=step_id,
            result_id=nil_uuid,
            worktree=worktree,
            policy=policy,
            command_name="unit",
            head_sha=head_sha,
        )


def test_controller_check_request_rejects_mismatched_policy_or_worktree() -> None:
    worktree, policy, run_id, step_id, result_id, head_sha = _make_fixture_context()
    foreign_policy = policy.model_copy(update={"id": uuid4()})
    with pytest.raises(ControllerCheckOperationError, match="policy project does not match"):
        controller_check_request(
            run_id=run_id,
            step_id=step_id,
            result_id=result_id,
            worktree=worktree,
            policy=foreign_policy,
            command_name="unit",
            head_sha=head_sha,
        )

    foreign_run_id = uuid4()
    with pytest.raises(ControllerCheckOperationError, match="worktree run does not match"):
        controller_check_request(
            run_id=foreign_run_id,
            step_id=step_id,
            result_id=result_id,
            worktree=worktree,
            policy=policy,
            command_name="unit",
            head_sha=head_sha,
        )


def test_controller_check_request_rejects_non_allowlisted_environment_keys() -> None:
    worktree, policy, run_id, step_id, result_id, head_sha = _make_fixture_context()
    with pytest.raises(ControllerCheckOperationError, match="non-allowlisted keys"):
        controller_check_request(
            run_id=run_id,
            step_id=step_id,
            result_id=result_id,
            worktree=worktree,
            policy=policy,
            command_name="unit",
            head_sha=head_sha,
            environment={"FORBIDDEN_VAR": "value"},
        )


def test_adapter_constructor_rejects_optional_command() -> None:
    worktree, policy, run_id, step_id, result_id, head_sha = _make_fixture_context(
        command_name="optional_check", command_required=False
    )
    with pytest.raises(ControllerCheckOperationError, match="policy.required_checks"):
        ControllerCheckOperationAdapter(
            run_id=run_id,
            step_id=step_id,
            result_id=result_id,
            worktree=worktree,
            policy=policy,
            command_name="optional_check",
            head_sha=head_sha,
            artifacts=cast(Any, _Unused()),
            artifact_store=cast(Any, _Unused()),
        )


def test_adapter_request_rejects_agent_payload_and_unknown_fields() -> None:
    worktree, policy, run_id, step_id, result_id, head_sha = _make_fixture_context()
    req = controller_check_request(
        run_id=run_id,
        step_id=step_id,
        result_id=result_id,
        worktree=worktree,
        policy=policy,
        command_name="unit",
        head_sha=head_sha,
    )
    adapter = ControllerCheckOperationAdapter(
        run_id=run_id,
        step_id=step_id,
        result_id=result_id,
        worktree=worktree,
        policy=policy,
        command_name="unit",
        head_sha=head_sha,
        artifacts=cast(Any, _Unused()),
        artifact_store=cast(Any, _Unused()),
    )

    # Injected agent_execution_id must be rejected
    tampered_payload = dict(req.request_payload)
    tampered_payload["agent_execution_id"] = str(uuid4())
    intent = OperationIntent(
        run_id=run_id,
        kind=CONTROLLER_CHECK_KIND,
        idempotency_key=req.idempotency_key,
        request_digest=canonical_digest(tampered_payload),
        request_payload=tampered_payload,
    )
    with pytest.raises(ControllerCheckOperationError):
        adapter._request(intent)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol_version", True),
        ("policy_version", True),
        ("step_id", "00000000-0000-0000-0000-000000000000"),
        ("result_id", "00000000-0000-0000-0000-000000000000"),
        ("head_sha", "c" * 40),
        ("run_id", str(uuid4())),
    ],
)
def test_adapter_request_rejects_tampered_authority(field: str, value: Any) -> None:
    worktree, policy, run_id, step_id, result_id, head_sha = _make_fixture_context()
    req = controller_check_request(
        run_id=run_id,
        step_id=step_id,
        result_id=result_id,
        worktree=worktree,
        policy=policy,
        command_name="unit",
        head_sha=head_sha,
    )
    adapter = ControllerCheckOperationAdapter(
        run_id=run_id,
        step_id=step_id,
        result_id=result_id,
        worktree=worktree,
        policy=policy,
        command_name="unit",
        head_sha=head_sha,
        artifacts=cast(Any, _Unused()),
        artifact_store=cast(Any, _Unused()),
    )
    tampered_payload = dict(req.request_payload)
    tampered_payload[field] = value
    intent = OperationIntent(
        run_id=run_id,
        kind=CONTROLLER_CHECK_KIND,
        idempotency_key=req.idempotency_key,
        request_digest=canonical_digest(tampered_payload),
        request_payload=tampered_payload,
    )
    with pytest.raises(ControllerCheckOperationError):
        adapter._request(intent)


async def _setup_real_adapter(
    tmp_path: Path,
    persisted_run: Any,
    session_factory: Any,
    *,
    exit_code: int | None = 0,
    timed_out: bool = False,
    cancelled: bool = False,
    environment: Mapping[str, str] = MappingProxyType({}),
) -> tuple[
    ControllerCheckOperationAdapter, OperationIntent, _Factory, ManagedWorktree, ProjectPolicy
]:
    project_id = persisted_run.project_id
    run_id = persisted_run.id
    step_id = uuid4()
    result_id = uuid4()
    head_sha = "b" * 40
    identity = WorktreeIdentity.for_run(project_id, run_id, "forge/test", False)
    worktree = ManagedWorktree(
        identity=identity, path=Path("/managed/forge-test").resolve(), base_sha=head_sha
    )
    command = CommandSpec(
        kind=StepKind.TEST,
        name="unit",
        argv=("pytest", "-q"),
        timeout_seconds=30,
        required=True,
        environment_keys=("TEST_SECRET",) if environment else (),
    )
    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path=str(Path("/repo").resolve()),
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
        duration_ms=10,
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

    req = controller_check_request(
        run_id=run_id,
        step_id=step_id,
        result_id=result_id,
        worktree=worktree,
        policy=policy,
        command_name="unit",
        head_sha=head_sha,
        environment=environment,
    )
    intent = OperationIntent(
        run_id=run_id,
        kind=CONTROLLER_CHECK_KIND,
        idempotency_key=req.idempotency_key,
        request_digest=req.request_digest,
        request_payload=req.request_payload,
    )

    adapter = ControllerCheckOperationAdapter(
        run_id=run_id,
        step_id=step_id,
        result_id=result_id,
        worktree=worktree,
        policy=policy,
        command_name="unit",
        head_sha=head_sha,
        artifacts=ArtifactRepository(session_factory),
        artifact_store=store,
        controlled_git=cast(Any, _Git(head_sha)),
        runner_factory=cast(Any, factory),
        environment=environment,
    )
    return adapter, intent, factory, worktree, policy


@pytest.mark.integration
@pytest.mark.parametrize(
    ("exit_code", "timed_out", "cancelled"),
    [
        (0, False, False),
        (1, False, False),
        (None, True, False),
        (0, False, True),
    ],
)
async def test_invoke_happy_path_and_readonly_recovery_no_second_launch(
    tmp_path: Path,
    persisted_run: Any,
    session_factory: Any,
    exit_code: int | None,
    timed_out: bool,
    cancelled: bool,
) -> None:
    adapter, intent, factory, worktree, policy = await _setup_real_adapter(
        tmp_path,
        persisted_run,
        session_factory,
        exit_code=exit_code,
        timed_out=timed_out,
        cancelled=cancelled,
    )

    outcome = await adapter.invoke(intent)
    assert outcome.status == OperationStatus.SUCCEEDED
    assert outcome.payload["result_id"] == str(adapter._result_id)
    assert outcome.payload["caller_cancelled"] is cancelled
    assert outcome.payload["exit_code"] == exit_code
    assert outcome.payload["timed_out"] is timed_out
    assert factory.calls == 1
    assert factory.runner.calls == 1

    # Readonly recovery adapter has no git/runner/env capabilities
    recovery_adapter = ControllerCheckOperationAdapter.for_recovery(
        run_id=adapter._run_id,
        step_id=adapter._step_id,
        result_id=adapter._result_id,
        worktree=worktree,
        policy=policy,
        command_name="unit",
        head_sha=adapter._head_sha,
        artifacts=adapter._artifacts,
        artifact_store=adapter._store,
    )
    recovered = await recovery_adapter.reconcile(intent)
    assert recovered == outcome
    # Reconcile must not make any new runner launches
    assert factory.calls == 1
    assert factory.runner.calls == 1


@pytest.mark.integration
async def test_transient_environment_values_absent_from_durable_artifacts(
    tmp_path: Path, persisted_run: Any, session_factory: Any
) -> None:
    secret_value = "super-secret-transient-token-123"
    adapter, intent, _, _, _ = await _setup_real_adapter(
        tmp_path, persisted_run, session_factory, environment={"TEST_SECRET": secret_value}
    )
    outcome = await adapter.invoke(intent)

    # 1. Check intent request payload
    assert secret_value not in json.dumps(dict(intent.request_payload))
    # 2. Check outcome payload
    assert secret_value not in json.dumps(dict(outcome.payload))
    # 3. Check persisted receipt bytes
    receipt_bytes = await adapter._store.open_bytes(cast(str, outcome.payload["receipt_digest"]))
    assert secret_value.encode() not in receipt_bytes
    # 4. Check all store files
    for p in tmp_path.glob("**/*"):
        if p.is_file():
            assert secret_value.encode() not in p.read_bytes()


@pytest.mark.integration
async def test_head_drift_before_launch_rejects_without_runner(
    tmp_path: Path, persisted_run: Any, session_factory: Any
) -> None:
    adapter, intent, factory, _, _ = await _setup_real_adapter(
        tmp_path, persisted_run, session_factory
    )
    cast(_Git, adapter._git).head = "c" * 40

    with pytest.raises(ControllerCheckOperationError, match="head changed"):
        await adapter.invoke(intent)

    assert factory.calls == 0


@pytest.mark.integration
async def test_head_drift_during_launch_rejects_after_runner_execution(
    tmp_path: Path, persisted_run: Any, session_factory: Any
) -> None:
    adapter, intent, factory, _, _ = await _setup_real_adapter(
        tmp_path, persisted_run, session_factory
    )

    class DriftingRunner:
        def __init__(self, terminal: CommandTerminalResult, git: _Git) -> None:
            self.terminal = terminal
            self.git = git
            self.calls = 0

        async def run_terminal(self, request: Any) -> CommandTerminalResult:
            self.calls += 1
            self.git.head = "f" * 40
            return self.terminal

    runner = DriftingRunner(factory.runner.terminal, cast(_Git, adapter._git))
    factory.runner = runner  # type: ignore[assignment]

    with pytest.raises(ControllerCheckOperationError, match="during execution"):
        await adapter.invoke(intent)

    assert factory.calls == 1
    assert runner.calls == 1


@pytest.mark.integration
async def test_foreign_result_mismatch_rejected(
    tmp_path: Path, persisted_run: Any, session_factory: Any
) -> None:
    adapter, intent, factory, _, _ = await _setup_real_adapter(
        tmp_path, persisted_run, session_factory
    )
    # Runner produces foreign result with wrong command name
    wrong_result = CommandResult(
        command_name="lint",
        kind=StepKind.LINT,
        command_digest=factory.runner.terminal.result.command_digest,
        policy_version=1,
        exit_code=0,
        timed_out=False,
        started_at=datetime.now(UTC),
        duration_ms=10,
        stdout_digest=factory.runner.terminal.result.stdout_digest,
        stderr_digest=factory.runner.terminal.result.stderr_digest,
        runner_mode=RunnerMode.DOCKER,
        image_digest="sha256:" + "d" * 64,
        network_enabled=False,
        stdout_original_byte_count=0,
        stderr_original_byte_count=0,
        stdout_truncated=False,
        stderr_truncated=False,
        unsandboxed=False,
    )
    factory.runner.terminal = CommandTerminalResult(result=wrong_result, caller_cancelled=False)

    with pytest.raises(ControllerCheckOperationError, match="not admitted"):
        await adapter.invoke(intent)


@pytest.mark.integration
async def test_cancellation_before_launch_creates_proof_and_reconciles(
    tmp_path: Path, persisted_run: Any, session_factory: Any
) -> None:
    adapter, intent, factory, _, _ = await _setup_real_adapter(
        tmp_path, persisted_run, session_factory
    )
    cancellation = NamedCheckCancellation()
    adapter._cancellation = cancellation
    cancellation.request()

    outcome = await adapter.invoke(intent)
    assert outcome.status == OperationStatus.SUCCEEDED
    assert outcome.payload["disposition"] == "cancelled_before_launch"
    assert outcome.payload["result_id"] == str(adapter._result_id)
    assert factory.calls == 0

    # Reconcile on recovery adapter verifies the no-effect proof
    recovery = ControllerCheckOperationAdapter.for_recovery(
        run_id=adapter._run_id,
        step_id=adapter._step_id,
        result_id=adapter._result_id,
        worktree=adapter._worktree,
        policy=adapter._policy,
        command_name=adapter._command_name,
        head_sha=adapter._head_sha,
        artifacts=adapter._artifacts,
        artifact_store=adapter._store,
    )
    recovered = await recovery.reconcile(intent)
    assert recovered == outcome


@pytest.mark.integration
async def test_cancellation_during_runner_prelaunch_yields_canonical_no_effect(
    tmp_path: Path, persisted_run: Any, session_factory: Any
) -> None:
    adapter, intent, factory, _, _ = await _setup_real_adapter(
        tmp_path, persisted_run, session_factory
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    class PrelaunchRunner:
        calls = 0

        async def run_terminal(self, request: Any) -> CommandTerminalResult:
            self.calls += 1
            entered.set()
            await release.wait()
            assert request.launch_ownership is not None
            request.launch_ownership.accept_launch()
            return factory.runner.terminal

    runner = PrelaunchRunner()
    factory.runner = runner  # type: ignore[assignment]
    cancellation = NamedCheckCancellation()
    adapter._cancellation = cancellation

    task = asyncio.create_task(adapter.invoke(intent))
    await asyncio.wait_for(entered.wait(), 5)
    cancellation.request()
    release.set()
    outcome = await asyncio.wait_for(task, 5)

    assert outcome.payload["disposition"] == "cancelled_before_launch"
    assert factory.calls == runner.calls == 1


@pytest.mark.integration
async def test_reconcile_missing_or_ambiguous_receipt_needs_reconciliation(
    tmp_path: Path, persisted_run: Any, session_factory: Any
) -> None:
    adapter, intent, factory, _, _ = await _setup_real_adapter(
        tmp_path, persisted_run, session_factory
    )
    # Reconcile before invoke has no receipt
    assert (await adapter.reconcile(intent)).status == OperationStatus.NEEDS_RECONCILIATION
    assert factory.calls == 0


@pytest.mark.integration
async def test_reconcile_rejects_tampered_receipt_authority(
    tmp_path: Path, persisted_run: Any, session_factory: Any
) -> None:
    adapter, intent, _, _, _ = await _setup_real_adapter(tmp_path, persisted_run, session_factory)
    outcome = await adapter.invoke(intent)
    receipt_digest = cast(str, outcome.payload["receipt_digest"])

    # Tamper with receipt payload (substitute boolean)
    receipt_data = json.loads(await adapter._store.open_bytes(receipt_digest))
    receipt_data["request_payload"]["policy_version"] = True
    new_bytes = json.dumps(receipt_data, separators=(",", ":"), sort_keys=True).encode()
    new_desc = await adapter._store.put_bytes(
        new_bytes, media_type="application/vnd.forge.controller-check-receipt+json"
    )

    original_artifacts = adapter._artifacts
    (receipt_artifact,) = await original_artifacts.get_by_producer(
        run_id=intent.run_id, producer_type=CONTROLLER_CHECK_KIND, producer_id=adapter._result_id
    )
    from dataclasses import replace

    tampered_artifact = replace(
        new_desc,
        run_id=intent.run_id,
        producer_type=CONTROLLER_CHECK_KIND,
        producer_id=adapter._result_id,
        parent_digests=receipt_artifact.parent_digests,
    )

    class SubstitutedArtifacts:
        async def get_by_producer(self, **kwargs: Any) -> tuple[ArtifactDescriptor, ...]:
            return (tampered_artifact,)

        async def get_by_digest(self, digest: str, *, run_id: UUID) -> ArtifactDescriptor:
            return await original_artifacts.get_by_digest(digest, run_id=run_id)

    adapter._artifacts = cast(Any, SubstitutedArtifacts())
    recovered = await adapter.reconcile(intent)
    assert recovered.status == OperationStatus.NEEDS_RECONCILIATION


@pytest.mark.integration
async def test_recovery_adapter_cannot_execute_commands_or_create_no_effect_proof() -> None:
    worktree, policy, run_id, step_id, result_id, head_sha = _make_fixture_context()
    req = controller_check_request(
        run_id=run_id,
        step_id=step_id,
        result_id=result_id,
        worktree=worktree,
        policy=policy,
        command_name="unit",
        head_sha=head_sha,
    )
    intent = OperationIntent(
        run_id=run_id,
        kind=CONTROLLER_CHECK_KIND,
        idempotency_key=req.idempotency_key,
        request_digest=req.request_digest,
        request_payload=req.request_payload,
    )
    recovery = ControllerCheckOperationAdapter.for_recovery(
        run_id=run_id,
        step_id=step_id,
        result_id=result_id,
        worktree=worktree,
        policy=policy,
        command_name="unit",
        head_sha=head_sha,
        artifacts=cast(Any, _Unused()),
        artifact_store=cast(Any, _Unused()),
    )
    with pytest.raises(ControllerCheckOperationError, match="cannot execute"):
        await recovery.invoke(intent)
    with pytest.raises(ControllerCheckOperationError, match="cannot create cancellation"):
        await recovery.cancel_before_launch(intent)
