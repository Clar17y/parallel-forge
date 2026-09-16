from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_client_process_exchanges_bounded_jsonl_frames() -> None:
    from forge.agents.client_process import ClientLaunchSpec, ClientProcessSupervisor

    spec = ClientLaunchSpec(
        argv=(sys.executable, "-c", "import sys; print(sys.stdin.readline().strip())"),
        cwd=".",
        environment={},
    )
    result = await ClientProcessSupervisor().run(spec, {"request": "hello"})

    assert result.frames == ({"request": "hello"},)
    assert result.return_code == 0
    assert result.receipt.pid > 0


@pytest.mark.asyncio
async def test_pinned_launch_consumes_verified_executable_and_file_bytes(tmp_path) -> None:
    from forge.agents.client_process import (
        ClientLaunchSpec,
        ClientPinnedFile,
        ClientProcessSupervisor,
    )

    pinned = tmp_path / "policy.json"
    pinned.write_text('{"policy":"closed"}', encoding="utf-8")
    executable = str(Path(sys.executable).resolve(strict=True))
    code = (
        "import json,pathlib,sys; sys.stdin.readline(); "
        "path=json.loads(sys.argv[1].partition('=')[2]); "
        "print(json.dumps({'policy':json.loads(pathlib.Path(path).read_text())}),flush=True)"
    )
    spec = ClientLaunchSpec(
        argv=(executable, "-c", code, "__FORGE_PINNED_POLICY__"),
        cwd=tmp_path,
        environment={},
        executable_digest=hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
        pinned_files=(
            ClientPinnedFile(
                argument_placeholder="__FORGE_PINNED_POLICY__",
                argument_prefix="policy=",
                path=pinned,
                digest=hashlib.sha256(pinned.read_bytes()).hexdigest(),
            ),
        ),
    )

    session = await ClientProcessSupervisor().start(spec)
    try:
        try:
            pinned.write_text('{"policy":"changed"}', encoding="utf-8")
        except PermissionError:
            pass
        await session.send({})
        frame = await session.receive()
        result = await session.wait_closed()
    finally:
        await session.close(completed=True)

    assert frame == {"policy": {"policy": "closed"}}
    assert result.frames == (frame,)


@pytest.mark.asyncio
async def test_pinned_launch_rejects_executable_drift_before_spawn(tmp_path) -> None:
    from forge.agents.client_process import (
        ClientLaunchSpec,
        ClientProcessError,
        ClientProcessSupervisor,
    )

    executable = tmp_path / ("client.exe" if os.name == "nt" else "client")
    executable.write_bytes(b"trusted")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    spec = ClientLaunchSpec(
        argv=(str(executable),),
        cwd=tmp_path,
        environment={},
        executable_digest=digest,
    )
    executable.write_bytes(b"changed")

    with pytest.raises(ClientProcessError):
        await ClientProcessSupervisor().start(spec)


@pytest.mark.asyncio
async def test_pinned_launch_rejects_file_drift_before_child_execution(tmp_path) -> None:
    from forge.agents.client_process import (
        ClientLaunchSpec,
        ClientPinnedFile,
        ClientProcessError,
        ClientProcessSupervisor,
    )

    marker, pinned = tmp_path / "ran", tmp_path / "policy.json"
    pinned.write_bytes(b"trusted")
    digest = hashlib.sha256(pinned.read_bytes()).hexdigest()
    executable = str(Path(sys.executable).resolve(strict=True))
    spec = ClientLaunchSpec(
        argv=(
            executable,
            "-c",
            "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('ran')",
            str(marker),
            "__FORGE_PINNED_POLICY__",
        ),
        cwd=tmp_path,
        environment={},
        executable_digest=hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
        pinned_files=(
            ClientPinnedFile(
                argument_placeholder="__FORGE_PINNED_POLICY__",
                argument_prefix="policy=",
                path=pinned,
                digest=digest,
            ),
        ),
    )
    pinned.write_bytes(b"changed")

    with pytest.raises(ClientProcessError):
        await ClientProcessSupervisor().start(spec)

    assert not marker.exists()


def _spec(code: str, **kwargs: object):
    from forge.agents.client_process import ClientLaunchSpec

    return ClientLaunchSpec(argv=(sys.executable, "-c", code), cwd=".", environment={}, **kwargs)


@pytest.mark.skipif(os.name != "nt", reason="Windows process environment")
async def test_windows_client_has_os_metadata_for_sockets_without_ambient_values(monkeypatch):
    from forge.agents.client_process import ClientProcessSupervisor

    monkeypatch.setenv("FORGE_CLIENT_PRIVATE_TEST", "fixture-only")
    result = await ClientProcessSupervisor().run(
        _spec(
            "import asyncio,json,os,socket; s=socket.socket(); s.close(); "
            "print(json.dumps({'socket': True, 'root': bool(os.environ.get('SystemRoot')), "
            "'private': 'FORGE_CLIENT_PRIVATE_TEST' in os.environ}))"
        ),
        {},
    )
    assert result.frames == ({"socket": True, "root": True, "private": False},), result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows process environment")
def test_windows_system_directory_cannot_be_overridden():
    from forge.agents.client_process import ClientLaunchSpec

    with pytest.raises(ValueError, match="cannot override"):
        ClientLaunchSpec(
            argv=(sys.executable, "-V"),
            cwd=".",
            environment={"systemroot": "C:/untrusted"},
            allowed_environment=frozenset({"systemroot"}),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["success", "failure", "timeout"])
async def test_revokes_before_process_stop_and_bounds_failed_revocation(revocation: str) -> None:
    from forge.agents.client_process import ClientProcessSupervisor, ProcessIdentityStatus

    calls = []
    session = None

    async def revoke():
        assert session is not None
        calls.append(ClientProcessSupervisor.identity_status(session.receipt))
        if revocation == "failure":
            raise OSError("injected revocation failure")
        if revocation == "timeout":
            await asyncio.Event().wait()

    session = await ClientProcessSupervisor().start(
        _spec("import time; time.sleep(30)", settlement_seconds=0.05), before_stop=revoke
    )
    result = await asyncio.wait_for(session.close(), 5)
    assert len(calls) == 1 and calls[0] is not ProcessIdentityStatus.GONE
    assert ClientProcessSupervisor.identity_status(session.receipt) is ProcessIdentityStatus.GONE
    assert result.stop_confirmed is (revocation == "success")
    if revocation != "success":
        assert result.outcome == "stop_uncertain"
    assert await session.close() == result
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_rejects_malformed_and_oversized_frames() -> None:
    from forge.agents.client_process import ClientProcessSupervisor, ClientProtocolError

    with pytest.raises(ClientProtocolError):
        await ClientProcessSupervisor().run(_spec("print('nope')"), {})
    with pytest.raises(ClientProtocolError):
        await ClientProcessSupervisor().run(
            _spec("print('{\"x\":\"' + 'a'*200 + '\"}')", frame_max_bytes=32), {}
        )


@pytest.mark.asyncio
async def test_timeout_and_cancellation_reap_child_tree() -> None:
    from forge.agents.client_process import ClientProcessSupervisor, ClientProcessTimeout

    supervisor = ClientProcessSupervisor()
    with pytest.raises(ClientProcessTimeout):
        await supervisor.run(_spec("import time; time.sleep(10)", duration_seconds=0.05), {})
    task = asyncio.create_task(supervisor.run(_spec("import time; time.sleep(10)"), {}))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_bounds_stderr_and_redacts_secret() -> None:
    from forge.agents.client_process import ClientProcessSupervisor

    result = await ClientProcessSupervisor().run(
        _spec(
            "import sys; print('{\"ok\":true}'); sys.stderr.write('token=abc ' + 'z'*100)",
            stderr_max_bytes=16,
        ),
        {},
    )
    assert result.stderr_truncated and "abc" not in result.stderr


def test_unknown_receipt_identity_is_not_a_kill_target() -> None:
    from forge.agents.client_process import (
        ClientProcessReceipt,
        ClientProcessSupervisor,
        ProcessIdentityStatus,
    )

    receipt = ClientProcessReceipt("x", 99999999, "old", 0)
    assert ClientProcessSupervisor.identity_status(receipt) is ProcessIdentityStatus.GONE


@pytest.mark.skipif(os.name != "nt", reason="Windows native launch")
@pytest.mark.asyncio
async def test_native_windows_launch_stays_suspended_until_resumed(tmp_path) -> None:
    from forge.agents.client_process_win32 import launch_suspended

    marker = tmp_path / "executed"
    child = launch_suspended(
        (
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran'); print(sys.stdin.readline().strip(), flush=True)",
            str(marker),
        ),
        str(tmp_path),
        {},
    )
    try:
        assert child.token()
        assert not marker.exists()
        child.stdin.write(b'{"ok":true}\n')
        child.resume()
        line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 3)
        assert line == b'{"ok":true}\r\n' or line == b'{"ok":true}\n'
        assert await asyncio.to_thread(child.wait, 3) == 0
        assert marker.read_text() == "ran"
    finally:
        child.terminate_tree()
        child.close()


@pytest.mark.asyncio
async def test_interactive_callbacks_keep_stdin_open_and_settle_once() -> None:
    from forge.agents.client_process import ClientProcessSupervisor

    class Hooks:
        def __init__(self):
            self.finishes = []

        async def launch_intent(self, launch_id):
            pass

        async def started(self, receipt):
            pass

        async def finished(self, receipt, result):
            self.finishes.append(result)

    hooks = Hooks()
    session = await ClientProcessSupervisor().start(
        _spec("import sys; [print(line.strip(),flush=True) for line in sys.stdin]"), lifecycle=hooks
    )
    try:
        for value in range(3):
            await session.send({"value": value})
            assert await session.receive() == {"value": value}
    finally:
        await session.close()
        await session.close()
    assert len(hooks.finishes) == 1


@pytest.mark.asyncio
async def test_idle_session_expires_without_another_api_call() -> None:
    from forge.agents.client_process import ClientProcessSupervisor

    session = await ClientProcessSupervisor().start(
        _spec("import time; time.sleep(30)", duration_seconds=0.15)
    )
    result = await asyncio.wait_for(session.wait_closed(), 3)
    assert result.outcome == "timeout" and result.stop_confirmed


@pytest.mark.asyncio
async def test_cumulative_small_frames_exceed_limit() -> None:
    from forge.agents.client_process import ClientProcessSupervisor, ClientProtocolError

    with pytest.raises(ClientProtocolError):
        await ClientProcessSupervisor().run(
            _spec("[print('{\"x\":1}',flush=True) for _ in range(1000)]", stdout_max_bytes=40), {}
        )


@pytest.mark.asyncio
async def test_started_callback_failure_stops_owned_child() -> None:
    from forge.agents.client_process import ClientProcessSupervisor, ProcessIdentityStatus

    class Hooks:
        receipt = None
        finished_count = 0

        async def launch_intent(self, launch_id):
            pass

        async def started(self, receipt):
            self.receipt = receipt
            raise ValueError("injected")

        async def finished(self, receipt, result):
            self.finished_count += 1

    hooks = Hooks()
    with pytest.raises(ValueError, match="injected"):
        await ClientProcessSupervisor().start(_spec("import time; time.sleep(30)"), lifecycle=hooks)
    assert hooks.receipt is not None
    assert ClientProcessSupervisor.identity_status(hooks.receipt) != ProcessIdentityStatus.MATCH
    assert hooks.finished_count == 1


@pytest.mark.asyncio
async def test_cancellation_during_started_revokes_suspended_process() -> None:
    from forge.agents.client_process import ClientProcessSupervisor, ProcessIdentityStatus

    reached = asyncio.Event()

    class Hooks:
        receipt = None
        finished_count = 0

        async def launch_intent(self, launch_id):
            pass

        async def started(self, receipt):
            self.receipt = receipt
            reached.set()
            await asyncio.Event().wait()

        async def finished(self, receipt, result):
            self.finished_count += 1

    hooks = Hooks()
    task = asyncio.create_task(
        ClientProcessSupervisor().start(_spec("import time; time.sleep(30)"), lifecycle=hooks)
    )
    await asyncio.wait_for(reached.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ClientProcessSupervisor.identity_status(hooks.receipt) is ProcessIdentityStatus.GONE
    assert hooks.finished_count == 1


async def test_repeated_start_cancellation_waits_for_owned_receipt_settlement() -> None:
    from forge.agents.client_process import ClientProcessSupervisor

    started, settling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Hooks:
        async def launch_intent(self, launch_id):
            pass

        async def started(self, receipt):
            started.set()
            await asyncio.Event().wait()

        async def finished(self, receipt, result):
            settling.set()
            await release.wait()

    task = asyncio.create_task(
        ClientProcessSupervisor().start(
            _spec("import time; time.sleep(30)"),
            lifecycle=Hooks(),
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(settling.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "startup returned while its durable settlement was still running"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_descendant_is_terminated_even_after_parent_exit() -> None:
    from forge.agents.client_process import (
        ClientProcessReceipt,
        ClientProcessSupervisor,
        ProcessIdentityStatus,
        _process_token,
    )

    code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); print('{\"pid\":'+str(p.pid)+'}',flush=True); sys.stdin.readline()"
    session = await ClientProcessSupervisor().start(_spec(code))
    child_pid = (await session.receive())["pid"]
    child = ClientProcessReceipt("child", child_pid, _process_token(child_pid), 0)
    try:
        assert ClientProcessSupervisor.identity_status(child) is ProcessIdentityStatus.MATCH
        await session.send({"finish": True})
        result = await asyncio.wait_for(session.wait_closed(), 3)
        assert result.stop_confirmed
        assert ClientProcessSupervisor.identity_status(child) is ProcessIdentityStatus.GONE
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_concurrent_sends_are_complete_frames() -> None:
    from forge.agents.client_process import ClientProcessSupervisor

    session = await ClientProcessSupervisor().start(
        _spec("import sys; [print(line.strip(),flush=True) for line in sys.stdin]")
    )
    try:
        await asyncio.gather(*(session.send({"n": i, "text": "x" * 1000}) for i in range(10)))
        frames = [await session.receive() for _ in range(10)]
        assert {f["n"] for f in frames} == set(range(10))
        assert all(f["text"] == "x" * 1000 for f in frames)
    finally:
        await session.close()


def test_launch_inputs_are_immutable_and_bounded() -> None:
    from forge.agents.client_process import ClientLaunchSpec

    env = {"SAFE": "before"}
    spec = ClientLaunchSpec((sys.executable,), ".", env, frozenset({"SAFE"}))
    env["SAFE"] = "after"
    assert spec.environment["SAFE"] == "before"
    for value in (float("nan"), float("inf"), True, -1):
        with pytest.raises(ValueError):
            _spec("pass", duration_seconds=value)
    with pytest.raises(ValueError):
        ClientLaunchSpec((sys.executable,), ".", {"NOT_ALLOWED": "x"})


@pytest.mark.skipif(os.name != "nt", reason="Windows launch containment")
def test_job_assignment_failure_kills_suspended_child(tmp_path, monkeypatch) -> None:
    from forge.agents import client_process_win32 as native

    seen = []
    get_pid = native._api("GetProcessId", (native.H,), native.w.DWORD)

    def reject(job, process):
        seen.append(int(get_pid(process)))
        return False

    monkeypatch.setattr(native, "_assign", reject)
    with pytest.raises(OSError):
        native.launch_suspended(
            (sys.executable, "-c", "import time;time.sleep(30)"), str(tmp_path), {}
        )
    assert seen and native.identity_token(seen[0]) is None


@pytest.mark.asyncio
async def test_terminal_receipt_contains_same_frames_as_returned_result() -> None:
    from forge.agents.client_process import ClientProcessSupervisor

    class Hooks:
        def __init__(self):
            self.finishes = []

        async def launch_intent(self, launch_id):
            pass

        async def started(self, receipt):
            pass

        async def finished(self, receipt, result):
            self.finishes.append(result)

    hooks = Hooks()
    result = await ClientProcessSupervisor().run(
        _spec("import sys;print(sys.stdin.readline().strip())"), {"x": 1}, lifecycle=hooks
    )
    assert result.frames == ({"x": 1},)
    assert hooks.finishes == [result]


@pytest.mark.asyncio
async def test_terminal_persistence_has_separate_bounded_deadline() -> None:
    from forge.agents.client_process import (
        ClientProcessSupervisor,
        ClientSettlementUncertain,
        ProcessIdentityStatus,
    )

    class Hooks:
        receipt = None
        calls = 0

        async def launch_intent(self, launch_id):
            pass

        async def started(self, receipt):
            self.receipt = receipt

        async def finished(self, receipt, result):
            self.calls += 1
            await asyncio.Event().wait()

    hooks = Hooks()
    session = await ClientProcessSupervisor().start(
        _spec("pass", settlement_seconds=0.05), lifecycle=hooks
    )
    with pytest.raises(ClientSettlementUncertain):
        await asyncio.wait_for(session.wait_closed(), 1)
    assert session._close_task.done()
    assert hooks.calls == 1
    assert ClientProcessSupervisor.identity_status(hooks.receipt) is ProcessIdentityStatus.GONE


@pytest.mark.asyncio
async def test_lost_terminal_ack_retains_exact_receipt_for_reconciliation() -> None:
    from forge.agents.client_process import ClientProcessSupervisor, ClientSettlementUncertain

    stored = {}

    class Hooks:
        launches = 0

        async def launch_intent(self, launch_id):
            self.launches += 1

        async def started(self, receipt):
            pass

        async def finished(self, receipt, result):
            stored.setdefault(receipt.launch_id, result)
            assert stored[receipt.launch_id] == result
            raise OSError("injected lost acknowledgement")

    hooks = Hooks()
    with pytest.raises(ClientSettlementUncertain) as failure:
        await ClientProcessSupervisor().run(
            _spec("import sys;print(sys.stdin.readline().strip())"), {"x": 1}, lifecycle=hooks
        )
    receipt = failure.value.receipt
    result = failure.value.result
    assert stored[receipt.launch_id] == result and result.frames == ({"x": 1},)
    stored.setdefault(receipt.launch_id, result)
    assert len(stored) == 1 and hooks.launches == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", ['{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '{"x":1,"x":2}'])
async def test_rejects_ambiguous_or_nonfinite_json(frame: str) -> None:
    from forge.agents.client_process import ClientProcessSupervisor, ClientProtocolError

    with pytest.raises(ClientProtocolError):
        await ClientProcessSupervisor().run(_spec(f"print({frame!r})"), {})


@pytest.mark.skipif(os.name != "nt", reason="Windows launch resource cleanup")
def test_failed_attribute_initialization_is_not_deleted(monkeypatch) -> None:
    from forge.agents import client_process_win32 as native

    original = native._init_attrs
    deleted = []

    def initialize(pointer, *args):
        return original(pointer, *args) if pointer is None else False

    monkeypatch.setattr(native, "_init_attrs", initialize)
    monkeypatch.setattr(native, "_delete_attrs", deleted.append)
    with pytest.raises(OSError):
        native.launch_suspended((sys.executable, "-c", "pass"), ".", {})
    assert deleted == []


@pytest.mark.skipif(os.name != "nt", reason="Windows launch descriptor cleanup")
def test_failed_stream_conversion_closes_transferred_descriptor(monkeypatch) -> None:
    from forge.agents import client_process_win32 as native

    descriptors = []

    def reject(fd, *args, **kwargs):
        descriptors.append(fd)
        raise OSError("injected fdopen failure")

    monkeypatch.setattr(native.os, "fdopen", reject)
    try:
        with pytest.raises(OSError):
            native.launch_suspended((sys.executable, "-c", "pass"), ".", {})
        assert descriptors
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
    finally:
        for fd in descriptors:
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX launch identity cleanup")
def test_posix_identity_failure_reaps_new_child(monkeypatch) -> None:
    from forge.agents import client_process as transport

    original = transport.subprocess.Popen
    children = []

    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    def no_identity(pid):
        raise transport.ClientProcessError("injected identity failure")

    monkeypatch.setattr(transport.subprocess, "Popen", launch)
    monkeypatch.setattr(transport, "_process_token", no_identity)
    try:
        with pytest.raises(transport.ClientProcessError):
            transport._PosixProcess(_spec("import time; time.sleep(30)"))
        assert children and children[0].poll() is not None
        assert all(
            stream.closed for stream in (children[0].stdin, children[0].stdout, children[0].stderr)
        )
    finally:
        for child in children:
            if child.poll() is None:
                import signal

                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=2)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()


def test_linux_operational_pinning_rejects_non_linux():
    from forge.agents.client_process import linux_operational_pinning_supported

    assert not linux_operational_pinning_supported() or sys.platform == "linux"


def test_linux_operational_pinning_rejects_missing_memfd_or_proc_dir(monkeypatch):
    from forge.agents import client_process as transport

    monkeypatch.setattr(transport.sys, "platform", "linux")
    monkeypatch.delattr(transport.os, "memfd_create", raising=False)
    monkeypatch.setattr(transport.Path, "is_dir", lambda self: True)
    assert not transport.linux_operational_pinning_supported()

    monkeypatch.setattr(transport.os, "memfd_create", lambda *args, **kwargs: 1, raising=False)
    monkeypatch.setattr(
        transport.Path,
        "is_dir",
        lambda self: self.as_posix() != "/proc/self/fd",
    )
    assert not transport.linux_operational_pinning_supported()


def test_linux_operational_pinning_success_mechanics(monkeypatch):
    from forge.agents import client_process as transport

    popen_calls = []
    closed_fds = []

    class MockChild:
        def __init__(self):
            self.killed = False

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            self.killed = True

    child = MockChild()

    def mock_popen(argv, **kwargs):
        popen_calls.append({"argv": argv, "kwargs": kwargs})
        return child

    def mock_close(fd):
        closed_fds.append(fd)

    fake_fd = 77
    original_is_dir = Path.is_dir
    monkeypatch.setattr(transport.sys, "platform", "linux")
    monkeypatch.setattr(
        transport.os, "memfd_create", lambda *args, **kwargs: fake_fd, raising=False
    )
    monkeypatch.setattr(
        transport.Path,
        "is_dir",
        lambda self: True if self.as_posix() == "/proc/self/fd" else original_is_dir(self),
    )
    monkeypatch.setattr(
        transport, "_sealed_verified_file", lambda path, digest, executable: fake_fd
    )
    monkeypatch.setattr(transport.subprocess, "Popen", mock_popen)
    monkeypatch.setattr(transport.os, "close", mock_close)

    assert transport.linux_operational_pinning_supported() is True
    assert len(popen_calls) == 1
    call = popen_calls[0]
    executable_path = str(Path(sys.executable).resolve(strict=True))
    assert call["argv"] == [executable_path, "-I", "-S", "-c", ""]
    assert call["kwargs"]["executable"] == f"/proc/self/fd/{fake_fd}"
    assert call["kwargs"]["env"] == {}
    assert call["kwargs"]["stdin"] == transport.subprocess.DEVNULL
    assert call["kwargs"]["stdout"] == transport.subprocess.DEVNULL
    assert call["kwargs"]["stderr"] == transport.subprocess.DEVNULL
    assert call["kwargs"]["close_fds"] is True
    assert call["kwargs"]["pass_fds"] == (fake_fd,)
    assert closed_fds == [fake_fd]


@pytest.mark.parametrize("stage", ["memfd_create", "fchmod_seal", "popen", "nonzero", "timeout"])
def test_linux_operational_pinning_failure_modes_and_cleanup(monkeypatch, stage):
    import subprocess

    from forge.agents import client_process as transport

    fake_fd = 88
    closed_fds = []

    def mock_close(fd):
        closed_fds.append(fd)

    original_is_dir = Path.is_dir
    monkeypatch.setattr(transport.sys, "platform", "linux")
    monkeypatch.setattr(
        transport.os, "memfd_create", lambda *args, **kwargs: fake_fd, raising=False
    )
    monkeypatch.setattr(
        transport.Path,
        "is_dir",
        lambda self: True if self.as_posix() == "/proc/self/fd" else original_is_dir(self),
    )
    monkeypatch.setattr(transport.os, "close", mock_close)

    if stage == "memfd_create":

        def fail_memfd(*args, **kwargs):
            raise PermissionError(13, "memfd_create denied by seccomp")

        monkeypatch.setattr(transport, "_sealed_verified_file", fail_memfd)
        assert transport.linux_operational_pinning_supported() is False
        assert closed_fds == []

    elif stage == "fchmod_seal":

        def fail_seal(*args, **kwargs):
            raise OSError("F_ADD_SEALS denied by seccomp")

        monkeypatch.setattr(transport, "_sealed_verified_file", fail_seal)
        assert transport.linux_operational_pinning_supported() is False
        assert closed_fds == []

    elif stage == "popen":
        monkeypatch.setattr(
            transport, "_sealed_verified_file", lambda path, digest, executable: fake_fd
        )

        def fail_popen(*args, **kwargs):
            raise PermissionError(13, "execve on /proc/self/fd denied by seccomp")

        monkeypatch.setattr(transport.subprocess, "Popen", fail_popen)
        assert transport.linux_operational_pinning_supported() is False
        assert closed_fds == [fake_fd]

    elif stage == "nonzero":
        monkeypatch.setattr(
            transport, "_sealed_verified_file", lambda path, digest, executable: fake_fd
        )

        class NonZeroChild:
            def wait(self, timeout=None):
                return 1

            def poll(self):
                return 1

            def kill(self):
                pass

        monkeypatch.setattr(transport.subprocess, "Popen", lambda *args, **kwargs: NonZeroChild())
        assert transport.linux_operational_pinning_supported() is False
        assert closed_fds == [fake_fd]

    elif stage == "timeout":
        monkeypatch.setattr(
            transport, "_sealed_verified_file", lambda path, digest, executable: fake_fd
        )

        class TimeoutChild:
            def __init__(self):
                self.killed = False
                self.reaped = False

            def wait(self, timeout=None):
                if not self.killed:
                    raise subprocess.TimeoutExpired(cmd="probe", timeout=timeout or 3.0)
                self.reaped = True
                return -9

            def poll(self):
                return None if not self.killed else -9

            def kill(self):
                self.killed = True

        child = TimeoutChild()
        monkeypatch.setattr(transport.subprocess, "Popen", lambda *args, **kwargs: child)
        assert transport.linux_operational_pinning_supported() is False
        assert child.killed is True
        assert child.reaped is True
        assert closed_fds == [fake_fd]
