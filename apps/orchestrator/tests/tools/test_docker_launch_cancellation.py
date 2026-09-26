"""Cancellation must stop an owned container while its Docker client still waits."""

import asyncio
import threading
from contextlib import suppress

import pytest
from forge.application.ports.runner import RunCommandRequest
from forge.tools.docker import DockerRunner
from forge.tools.paths import CanonicalRoot
from forge.tools.runner import RunnerExecutionError
from test_docker_runner import _command, _FakeArtifacts, _policy, _ProcessResult


class PendingDockerLaunch:
    def __init__(self, *, delayed, foreign=False):
        self.started = threading.Event()
        self.allow_visible = threading.Event()
        self.allow_return = threading.Event()
        self.checked_pending = threading.Event()
        self.checked_foreign = threading.Event()
        self.removed = threading.Event()
        self.visible = False
        self.foreign = foreign
        self.label = None
        self.container_id = "a" * 64
        self.removal_targets = []
        if not delayed:
            self.allow_visible.set()

    def run_argv(self, argv, **kwargs):
        if argv[:2] == ("docker", "run"):
            self.label = next(
                value.split("=", 1)[1] for value in argv if value.startswith("forge.owner-token=")
            )
            self.started.set()
            assert self.allow_visible.wait(3)
            self.visible = True
            assert self.allow_return.wait(3)
            return _ProcessResult(
                return_code=137, stdout="interrupted\n", stdout_original_byte_count=12
            )
        if argv[:2] == ("docker", "inspect"):
            if self.visible and not self.removed.is_set():
                if self.foreign:
                    self.checked_foreign.set()
                return _ProcessResult(
                    stdout=f"{self.container_id}\t{'foreign' if self.foreign else self.label}\n"
                )
            if self.started.is_set():
                self.checked_pending.set()
            return _ProcessResult(
                return_code=1, stdout="", stderr=f"Error: No such container: {argv[-1]}\n"
            )
        assert argv[:3] == ("docker", "rm", "-f")
        self.removal_targets.append(argv[-1])
        assert not self.foreign and argv[-1] == self.container_id
        self.removed.set()
        return _ProcessResult()


@pytest.mark.parametrize("delayed", [False, True])
async def test_managed_cancel_removes_visible_identity_before_launch_returns(
    tmp_path, monkeypatch, delayed
):
    process = PendingDockerLaunch(delayed=delayed)
    command = _command()
    root = CanonicalRoot(tmp_path)
    artifacts = _FakeArtifacts()
    runner = DockerRunner(
        policy=_policy(command),
        root=root,
        image_digest="sha256:" + "4" * 64,
        process_runner=process,
        artifact_store=artifacts,
    )

    async def repaired(*args, **kwargs):
        return False

    monkeypatch.setattr(runner, "_repair_for_terminal", repaired)
    with root.open_directory():
        task = asyncio.create_task(
            runner._run_terminal_at(
                RunCommandRequest(command_name=command.name, kind=command.kind),
                root.path,
                managed=True,
            )
        )
        try:
            assert await asyncio.to_thread(process.started.wait, 1)
            task.cancel()
            if delayed:
                assert await asyncio.to_thread(process.checked_pending.wait, 1)
                assert not task.done() and not process.removed.is_set()
                task.cancel()
                process.allow_visible.set()
            assert await asyncio.to_thread(process.removed.wait, 1)
            assert not task.done() and not process.allow_return.is_set()
            task.cancel()
            process.allow_return.set()
            terminal = await task
        finally:
            process.allow_visible.set()
            process.allow_return.set()
            with suppress(asyncio.CancelledError, RunnerExecutionError):
                await task
    assert terminal.caller_cancelled and terminal.result.exit_code == 137
    assert not terminal.result.timed_out
    assert process.removal_targets == [process.container_id]
    assert len(artifacts.values) == 2


async def test_managed_cancel_never_removes_foreign_container_while_launch_is_pending(
    tmp_path, monkeypatch
):
    process = PendingDockerLaunch(delayed=False, foreign=True)
    command = _command()
    root = CanonicalRoot(tmp_path)
    artifacts = _FakeArtifacts()
    runner = DockerRunner(
        policy=_policy(command),
        root=root,
        image_digest="sha256:" + "4" * 64,
        process_runner=process,
        artifact_store=artifacts,
    )

    async def repaired(*args, **kwargs):
        pytest.fail("foreign container must retain the recovery fence before access repair")

    monkeypatch.setattr(runner, "_repair_for_terminal", repaired)
    with root.open_directory():
        task = asyncio.create_task(
            runner._run_terminal_at(
                RunCommandRequest(command_name=command.name, kind=command.kind),
                root.path,
                managed=True,
            )
        )
        try:
            assert await asyncio.to_thread(process.started.wait, 1)
            task.cancel()
            assert await asyncio.to_thread(process.checked_foreign.wait, 1)
            assert not task.done() and not process.removal_targets
            process.allow_return.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await task
            assert "Docker cleanup or access repair requires recovery" in error.value.__notes__
        finally:
            process.allow_return.set()
            with suppress(asyncio.CancelledError, RunnerExecutionError):
                await task
    assert not process.removal_targets and not artifacts.values
