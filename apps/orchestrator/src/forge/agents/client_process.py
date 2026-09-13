"""Bounded interactive JSONL sessions for trusted official-client adapters.

Launch specifications are adapter-owned, never agent commands. Persistence and
broker admission remain outside this transport; lifecycle hooks bind their receipts.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import math
import os
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import IO, Any, Protocol, Self, cast
from uuid import uuid4

from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.observability.redaction import Redactor


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError("nonfinite JSON constant")


def _json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite JSON number")
    return result


class ClientProcessError(RuntimeError):
    """Sanitized process failure."""


class ClientProtocolError(ClientProcessError):
    """Malformed or excessive client protocol."""


class ClientProcessTimeout(ClientProcessError):
    """Absolute attempt deadline reached."""


class ProcessIdentityStatus(StrEnum):
    MATCH = "match"
    GONE = "gone"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ClientLaunchSpec:
    argv: tuple[str, ...] = field(repr=False)
    cwd: str | os.PathLike[str]
    environment: Mapping[str, str] = field(repr=False)
    allowed_environment: frozenset[str] = frozenset()
    duration_seconds: float = 30.0
    settlement_seconds: float = 2.0
    stdout_max_bytes: int = 1024 * 1024
    stderr_max_bytes: int = 1024 * 1024
    frame_max_bytes: int = 256 * 1024

    def __post_init__(self) -> None:
        argv = tuple(self.argv)
        if not argv or any(type(a) is not str or not a or "\0" in a for a in argv):
            raise ValueError("invalid client argv")
        if not Path(argv[0]).is_absolute():
            raise ValueError("client executable must be absolute")
        if (
            type(self.duration_seconds) not in (int, float)
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds <= 0
        ):
            raise ValueError("client duration must be finite and positive")
        if (
            type(self.settlement_seconds) not in (int, float)
            or not math.isfinite(self.settlement_seconds)
            or not 0 < self.settlement_seconds <= 30
        ):
            raise ValueError("settlement duration must be finite and bounded")
        for stream_limit in (self.stdout_max_bytes, self.stderr_max_bytes, self.frame_max_bytes):
            if type(stream_limit) is not int or not 0 < stream_limit <= 64 * 1024 * 1024:
                raise ValueError("client stream limits must be bounded positive integers")
        allowed = frozenset(self.allowed_environment)
        env = dict(self.environment)
        for key, value in env.items():
            if (
                type(key) is not str
                or not key
                or "=" in key
                or "\0" in key
                or type(value) is not str
                or "\0" in value
            ):
                raise ValueError("invalid client environment")
        if not env.keys() <= allowed:
            raise ValueError("client environment is not allowlisted")
        if os.name == "nt" and len({k.upper() for k in env}) != len(env):
            raise ValueError("client environment contains case aliases")
        if os.name == "nt":
            from forge.agents.client_process_win32 import system_environment

            # An isolated Windows environment still needs its OS directory for
            # loader/Winsock initialization. Never inherit ambient credentials,
            # PATH, or an operator-supplied replacement for this OS-owned value.
            system = system_environment()
            for key, value in env.items():
                if (
                    key.upper() == "SYSTEMROOT"
                    and value.casefold() != system["SystemRoot"].casefold()
                ):
                    raise ValueError("client cannot override the Windows system directory")
            env = {key: value for key, value in env.items() if key.upper() != "SYSTEMROOT"} | system
            allowed |= frozenset(system)
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "cwd", str(Path(self.cwd).resolve(strict=True)))
        object.__setattr__(self, "allowed_environment", allowed)
        object.__setattr__(self, "environment", MappingProxyType(env))


@dataclass(frozen=True, slots=True)
class ClientProcessReceipt:
    launch_id: str
    pid: int
    process_start_token: str
    launched_monotonic: float


@dataclass(frozen=True, slots=True)
class ClientProcessResult:
    receipt: ClientProcessReceipt
    return_code: int | None
    frames: tuple[Mapping[str, Any], ...] = field(repr=False)
    stdout_byte_count: int
    stderr: str = field(repr=False)
    stderr_byte_count: int
    stdout_truncated: bool
    stderr_truncated: bool
    outcome: str
    stop_confirmed: bool


def terminal_launch_proof(result: ClientProcessResult) -> SubscriptionLaunchTerminalProof:
    """Project only bounded process evidence; never protocol frames or stderr."""
    return SubscriptionLaunchTerminalProof.model_validate(
        {
            "launch_id": result.receipt.launch_id,
            "pid": result.receipt.pid,
            "process_identity": result.receipt.process_start_token,
            "outcome": result.outcome,
            "return_code": result.return_code,
            "stop_confirmed": result.stop_confirmed,
            "stdout_bytes": result.stdout_byte_count,
            "stderr_bytes": result.stderr_byte_count,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
        }
    )


class ClientSettlementUncertain(ClientProcessError):
    """The process stopped but receipt persistence needs idempotent reconciliation."""

    def __init__(self, result: ClientProcessResult):
        super().__init__("client receipt settlement is uncertain")
        self.result = result
        self.receipt = result.receipt


class ClientProcessLifecycle(Protocol):
    async def launch_intent(self, launch_id: str) -> None: ...
    async def started(self, receipt: ClientProcessReceipt) -> None: ...
    async def finished(
        self, receipt: ClientProcessReceipt, result: ClientProcessResult | None
    ) -> None: ...


def _process_token(pid: int) -> str | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        from forge.agents.client_process_win32 import identity_token

        return identity_token(pid)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except FileNotFoundError:
        return None
    except OSError, IndexError:
        raise ClientProcessError("process identity unavailable") from None


class _OwnedProcess(Protocol):
    pid: int
    stdin: IO[bytes]
    stdout: IO[bytes]
    stderr: IO[bytes]

    def token(self) -> str: ...
    def resume(self) -> None: ...
    def wait(self, seconds: float) -> int: ...
    def terminate_tree(self) -> None: ...
    def close(self) -> None: ...


class _PosixProcess:
    def __init__(self, spec: ClientLaunchSpec) -> None:
        self.process = subprocess.Popen(
            spec.argv,
            cwd=spec.cwd,
            env=dict(spec.environment),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
            close_fds=True,
        )
        assert (
            self.process.stdin is not None
            and self.process.stdout is not None
            and self.process.stderr is not None
        )
        self.pid = self.process.pid
        self.stdin, self.stdout, self.stderr = (
            self.process.stdin,
            self.process.stdout,
            self.process.stderr,
        )
        try:
            self._token = _process_token(self.pid)
        except BaseException:
            try:
                self.terminate_tree()
                self.process.wait(timeout=2)
            finally:
                self.close()
            raise

    def token(self) -> str:
        return self._token or ""

    def resume(self) -> None:
        pass

    def wait(self, seconds: float) -> int:
        try:
            return self.process.wait(timeout=max(0, seconds))
        except subprocess.TimeoutExpired:
            raise TimeoutError("process settlement deadline") from None

    def terminate_tree(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            if sys.platform != "win32":
                os.killpg(self.pid, signal.SIGKILL)

    def close(self) -> None:
        for stream in (self.stdin, self.stdout, self.stderr):
            stream.close()


def _spawn(spec: ClientLaunchSpec) -> _OwnedProcess:
    try:
        if os.name == "nt":
            from forge.agents.client_process_win32 import launch_suspended

            return cast(
                _OwnedProcess, launch_suspended(spec.argv, str(spec.cwd), dict(spec.environment))
            )
        return _PosixProcess(spec)
    except OSError, ValueError:
        raise ClientProcessError("official client launch failed") from None


def _encode_frame(value: Mapping[str, Any], limit: int) -> bytes:
    if not isinstance(value, Mapping):
        raise ClientProtocolError("client request must be an object")
    try:
        result = (
            json.dumps(
                dict(value), separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
            + b"\n"
        )
    except ValueError, TypeError, RecursionError:
        raise ClientProtocolError("client request is not bounded JSON") from None
    if len(result) > limit:
        raise ClientProtocolError("client request frame exceeds limit")
    return result


def _observe_task_error(task: asyncio.Task[Any]) -> None:
    # A watchdog can settle an abandoned session; retain the task/result for the
    # owner without emitting an unobserved exception containing provider data.
    if not task.cancelled():
        task.exception()


class ClientProcessSession:
    """One interactive attempt with a single deadline and exactly one settlement."""

    def __init__(
        self,
        process: _OwnedProcess,
        receipt: ClientProcessReceipt,
        spec: ClientLaunchSpec,
        lifecycle: ClientProcessLifecycle | None,
        deadline: float,
        before_stop: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.process, self.receipt, self.spec = process, receipt, spec
        self.lifecycle, self.deadline = lifecycle, deadline
        self._before_stop = before_stop
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._frames: list[Mapping[str, Any]] = []
        self._settlement_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._close_task: asyncio.Task[ClientProcessResult] | None = None
        self._failure: ClientProcessError | None = None
        self._out_bytes = self._err_bytes = 0
        self._stderr = bytearray()
        self._tasks: list[asyncio.Task[None]] = []
        self._exit_task: asyncio.Task[None] | None = None
        self._result: ClientProcessResult | None = None
        self._redactor = Redactor(
            secrets=[
                v
                for k, v in spec.environment.items()
                if v and any(s in k.lower() for s in ("key", "token", "secret", "password"))
            ]
        )

    def begin(self) -> None:
        self._tasks = [
            asyncio.create_task(self._stdout()),
            asyncio.create_task(self._stderr_reader()),
        ]
        self._exit_task = asyncio.create_task(self._watch_exit())

    def _settle(self, outcome: str) -> asyncio.Task[ClientProcessResult]:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._finish(outcome))
            self._close_task.add_done_callback(_observe_task_error)
        return self._close_task

    async def _watch_exit(self) -> None:
        try:
            await asyncio.to_thread(
                self.process.wait, max(0, self.deadline - asyncio.get_running_loop().time())
            )
            self._settle("exited")
        except TimeoutError:
            self._failure = ClientProcessTimeout("official client exceeded its duration limit")
            self._settle("timeout")
        except OSError:
            self._failure = ClientProcessError("client wait failed")
            self._settle("stop_uncertain")

    async def _stdout(self) -> None:
        pending = bytearray()
        try:
            while chunk := await asyncio.to_thread(
                self.process.stdout.read, min(4096, self.spec.frame_max_bytes + 1)
            ):
                self._out_bytes += len(chunk)
                if self._out_bytes > self.spec.stdout_max_bytes:
                    raise ClientProtocolError("client total output exceeds limit")
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    self._admit_frame(line)
                if len(pending) > self.spec.frame_max_bytes:
                    raise ClientProtocolError("client frame exceeds limit")
            if pending:
                raise ClientProtocolError("client frame is not newline terminated")
        except (ClientProtocolError, OSError, ValueError) as error:
            self._failure = (
                error
                if isinstance(error, ClientProtocolError)
                else ClientProtocolError("client output failed")
            )
            self._settle("protocol_error")
        finally:
            self._queue.put_nowait(None)

    def _admit_frame(self, line: bytes | bytearray) -> None:
        if len(line) + 1 > self.spec.frame_max_bytes:
            raise ClientProtocolError("client frame exceeds limit")
        try:
            decoded = json.loads(
                line,
                object_pairs_hook=_json_object,
                parse_constant=_reject_json_constant,
                parse_float=_json_float,
            )
        except ValueError, UnicodeError, RecursionError:
            raise ClientProtocolError("client emitted malformed JSONL") from None
        if not isinstance(decoded, dict):
            raise ClientProtocolError("client frame must be an object")
        self._frames.append(decoded)
        self._queue.put_nowait(copy.deepcopy(decoded))

    async def _stderr_reader(self) -> None:
        try:
            while chunk := await asyncio.to_thread(self.process.stderr.read, 4096):
                self._err_bytes += len(chunk)
                self._stderr.extend(chunk[: max(0, self.spec.stderr_max_bytes - len(self._stderr))])
        except OSError, ValueError:
            if self._close_task is None:
                self._failure = ClientProcessError("client stderr failed")
                self._settle("protocol_error")

    async def send(self, frame: Mapping[str, Any]) -> None:
        payload = _encode_frame(frame, self.spec.frame_max_bytes)
        try:
            async with asyncio.timeout_at(self.deadline):
                async with self._send_lock:
                    if self._close_task is not None:
                        raise ClientProcessError("client session is closed")
                    await asyncio.to_thread(self._write, payload)
        except TimeoutError:
            self._failure = ClientProcessTimeout("official client exceeded its duration limit")
            await asyncio.shield(self._settle("timeout"))
            raise self._failure from None
        except asyncio.CancelledError:
            await asyncio.shield(self._settle("cancelled"))
            raise
        except OSError, ValueError:
            await asyncio.shield(self._settle("protocol_error"))
            raise ClientProcessError("client input failed") from None

    def _write(self, payload: bytes) -> None:
        remaining = memoryview(payload)
        while remaining:
            count = self.process.stdin.write(remaining)
            if not count:
                raise OSError("closed client input")
            remaining = remaining[count:]

    async def close_stdin(self) -> None:
        async with self._send_lock:
            await asyncio.to_thread(self.process.stdin.close)

    async def receive(self) -> dict[str, Any] | None:
        try:
            async with asyncio.timeout_at(self.deadline):
                item = await self._queue.get()
        except TimeoutError:
            self._failure = ClientProcessTimeout("official client exceeded its duration limit")
            await asyncio.shield(self._settle("timeout"))
            raise self._failure from None
        except asyncio.CancelledError:
            await asyncio.shield(self._settle("cancelled"))
            raise
        if self._failure is not None:
            await asyncio.shield(self._settle("protocol_error"))
            raise self._failure
        return item

    async def _finish(self, outcome: str) -> ClientProcessResult:
        confirmed = outcome != "stop_uncertain"
        code = None
        if self._before_stop is not None:
            callback = self._before_stop

            async def invoke_before_stop() -> None:
                await callback()

            revoke_task = asyncio.create_task(invoke_before_stop())
            revoke_task.add_done_callback(_observe_task_error)
            done, _ = await asyncio.wait((revoke_task,), timeout=self.spec.settlement_seconds)
            if not done:
                revoke_task.cancel()
            if not done or revoke_task.cancelled() or revoke_task.exception() is not None:
                confirmed = False
                self._failure = ClientProcessError("client authority revocation is uncertain")
        try:
            self.process.terminate_tree()
            code = await asyncio.to_thread(self.process.wait, 2)
        except OSError, TimeoutError:
            confirmed = False
        # Kill releases inherited pipe ends; collect readers before closing handles.
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=2)
            if pending:
                confirmed = False
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        if self._exit_task and self._exit_task is not asyncio.current_task():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._exit_task), 2)
        self.process.close()
        if self._failure and outcome in {"exited", "completed"}:
            outcome = "protocol_error"
        self._result = ClientProcessResult(
            self.receipt,
            code,
            tuple(self._frames)
            if outcome == "exited" and self._failure is None and confirmed
            else (),
            self._out_bytes,
            str(self._redactor.redact(self._stderr.decode("utf-8", "replace"))),
            self._err_bytes,
            self._out_bytes > self.spec.stdout_max_bytes,
            self._err_bytes > self.spec.stderr_max_bytes,
            outcome if confirmed else "stop_uncertain",
            confirmed,
        )
        self._queue.put_nowait(None)
        if self.lifecycle:
            self._settlement_task = asyncio.create_task(
                self.lifecycle.finished(self.receipt, self._result)
            )
            self._settlement_task.add_done_callback(_observe_task_error)
            done, _ = await asyncio.wait(
                (self._settlement_task,), timeout=self.spec.settlement_seconds
            )
            if not done:
                self._settlement_task.cancel()
                await asyncio.sleep(0)
                raise ClientSettlementUncertain(self._result)
            if self._settlement_task.cancelled() or self._settlement_task.exception() is not None:
                raise ClientSettlementUncertain(self._result)
        return self._result

    async def close(self, *, completed: bool = False) -> ClientProcessResult:
        return await asyncio.shield(self._settle("completed" if completed else "cancelled"))

    async def wait_closed(self) -> ClientProcessResult:
        if self._exit_task and self._close_task is None:
            await asyncio.shield(self._exit_task)
        return await asyncio.shield(self._settle("exited"))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


class ClientProcessSupervisor:
    async def start(
        self,
        spec: ClientLaunchSpec,
        *,
        lifecycle: ClientProcessLifecycle | None = None,
        before_stop: Callable[[], Awaitable[None]] | None = None,
    ) -> ClientProcessSession:
        deadline = asyncio.get_running_loop().time() + spec.duration_seconds
        launch_id = str(uuid4())
        async with asyncio.timeout_at(deadline):
            if lifecycle:
                await lifecycle.launch_intent(launch_id)
        process = _spawn(spec)
        receipt = ClientProcessReceipt(launch_id, process.pid, process.token(), time.monotonic())
        session = ClientProcessSession(process, receipt, spec, lifecycle, deadline, before_stop)
        try:
            async with asyncio.timeout_at(deadline):
                if lifecycle:
                    await lifecycle.started(receipt)
            process.resume()
            session.begin()
            return session
        except BaseException as error:
            outcome = (
                "cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "timeout"
                if isinstance(error, TimeoutError)
                else "protocol_error"
            )
            settlement = session._settle(outcome)
            interrupted = isinstance(error, asyncio.CancelledError)
            while not settlement.done():
                try:
                    await asyncio.shield(settlement)
                except asyncio.CancelledError:
                    interrupted = True
            settlement.result()
            if interrupted:
                raise asyncio.CancelledError from None
            raise

    async def run(
        self,
        spec: ClientLaunchSpec,
        request: Mapping[str, Any],
        *,
        lifecycle: ClientProcessLifecycle | None = None,
    ) -> ClientProcessResult:
        session = await self.start(spec, lifecycle=lifecycle)
        try:
            await session.send(request)
            await session.close_stdin()
            while await session.receive() is not None:
                pass
            result = await session.wait_closed()
            if session._failure:
                raise session._failure
            return result
        finally:
            await session.close()

    @staticmethod
    def identity_status(receipt: ClientProcessReceipt) -> ProcessIdentityStatus:
        try:
            token = _process_token(receipt.pid)
        except OSError, ClientProcessError:
            return ProcessIdentityStatus.UNCERTAIN
        if token is None:
            return ProcessIdentityStatus.GONE
        if not receipt.process_start_token or token != receipt.process_start_token:
            return ProcessIdentityStatus.UNCERTAIN
        return ProcessIdentityStatus.MATCH
