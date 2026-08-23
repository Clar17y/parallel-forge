"""Bounded, no-shell process execution below a canonical repository root."""

from __future__ import annotations

import contextlib
import math
import os
import subprocess
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO

from forge.application.ports.repository import ProcessExecutionError, ProcessResult
from forge.tools.paths import (
    CanonicalRoot,
    PathEscape,
    RepositoryAccessDenied,
    _DirectoryAccess,
)

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_STREAM_LIMIT = 1024 * 1024
_READ_CHUNK = 64 * 1024


class _BoundedStream:
    """Drain one pipe while retaining no more than its configured prefix."""

    def __init__(self, stream: IO[bytes], limit: int) -> None:
        self._stream = stream
        self._limit = limit
        self.data = bytearray()
        self.original_byte_count = 0
        self.error: BaseException | None = None

    def drain(self) -> None:
        try:
            while True:
                chunk = self._stream.read(_READ_CHUNK)
                if not chunk:
                    return
                self.original_byte_count += len(chunk)
                if len(self.data) < self._limit:
                    self.data.extend(chunk[: self._limit - len(self.data)])
        except (OSError, ValueError, RuntimeError) as error:
            self.error = error

    @property
    def truncated(self) -> bool:
        return self.original_byte_count > self._limit


class ProcessRunner:
    """Run one explicit argv below a pinned root with bounded output."""

    def __init__(
        self,
        root: CanonicalRoot,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        stdout_max_bytes: int = _DEFAULT_STREAM_LIMIT,
        stderr_max_bytes: int = _DEFAULT_STREAM_LIMIT,
    ) -> None:
        if not isinstance(root, CanonicalRoot):
            raise TypeError("process runner requires a canonical repository root")
        self._root = root
        self._timeout_seconds = _positive_timeout(timeout_seconds)
        self._stdout_max_bytes = _positive_bound(stdout_max_bytes)
        self._stderr_max_bytes = _positive_bound(stderr_max_bytes)

    def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str],
        environment: Mapping[str, str],
        timeout_seconds: float | None = None,
    ) -> ProcessResult:
        command = _validate_argv(argv)
        env = _validate_environment(environment)
        timeout = (
            self._timeout_seconds if timeout_seconds is None else _positive_timeout(timeout_seconds)
        )
        normalized_cwd = _normalize_cwd(self._root, cwd)
        active_access = self._root._active_directory_access(normalized_cwd)
        if active_access is not None:
            return self._run_with_access(
                command,
                normalized_cwd,
                active_access,
                env,
                timeout,
                pass_fds=self._root._pass_fds_for_access(normalized_cwd, active_access),
            )

        try:
            with self._root._open_directory(normalized_cwd) as access:
                return self._run_with_access(
                    command,
                    normalized_cwd,
                    access,
                    env,
                    timeout,
                )
        except RepositoryAccessDenied, PathEscape:
            raise
        except OSError, subprocess.SubprocessError, ValueError, TypeError:
            raise ProcessExecutionError() from None

    def _run_with_access(
        self,
        command: tuple[str, ...],
        normalized_cwd: str,
        access: _DirectoryAccess,
        environment: dict[str, str],
        timeout: float,
        *,
        pass_fds: tuple[int, ...] = (),
    ) -> ProcessResult:
        retained_access = self._root._verify_directory_access(normalized_cwd, access)
        try:
            return self._run_in_directory(
                command,
                self._root._launch_path_for_access(
                    normalized_cwd, retained_access, require_fd=bool(pass_fds)
                ),
                environment,
                timeout,
                pass_fds=pass_fds,
            )
        finally:
            self._root._verify_directory_access(normalized_cwd, retained_access)

    def _run_in_directory(
        self,
        command: tuple[str, ...],
        cwd: str,
        environment: dict[str, str],
        timeout: float,
        pass_fds: tuple[int, ...] = (),
    ) -> ProcessResult:
        process: subprocess.Popen[bytes] | None = None
        try:
            if os.name != "nt" and pass_fds:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=environment,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    pass_fds=pass_fds,
                )
            else:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=environment,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
        except OSError, subprocess.SubprocessError, ValueError, TypeError:
            raise ProcessExecutionError() from None
        return _collect(process, timeout, self._stdout_max_bytes, self._stderr_max_bytes)


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes, bytearray)):
        raise ProcessExecutionError()
    try:
        command = tuple(argv)
    except TypeError, ValueError:
        raise ProcessExecutionError() from None
    if not command:
        raise ProcessExecutionError()
    for argument in command:
        if not isinstance(argument, str) or not argument or "\x00" in argument:
            raise ProcessExecutionError()
    return command


def _validate_environment(environment: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(environment, Mapping):
        raise ProcessExecutionError()
    try:
        values = dict(environment)
    except TypeError, ValueError:
        raise ProcessExecutionError() from None
    for key, value in values.items():
        if (
            not isinstance(key, str)
            or not key
            or "\x00" in key
            or not isinstance(value, str)
            or "\x00" in value
        ):
            raise ProcessExecutionError()
    return values


def _positive_bound(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("process byte bounds must be positive integers")
    return value


def _positive_timeout(value: float) -> float:
    if isinstance(value, bool):
        raise TypeError("process timeout must be numeric")
    if not isinstance(value, (int, float)):
        raise TypeError("process timeout must be numeric")
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError("process timeout must be a positive finite number")
    return converted


def _normalize_cwd(root: CanonicalRoot, cwd: str | os.PathLike[str]) -> str:
    try:
        raw = os.fspath(cwd)
    except TypeError, ValueError:
        raise PathEscape("repository working directory is invalid") from None
    if not isinstance(raw, str) or not raw:
        raise PathEscape("repository working directory is invalid")

    candidate = Path(raw)
    if candidate.is_absolute():
        if any(part in {".", ".."} for part in candidate.parts[1:]):
            raise PathEscape("repository working directory is not canonical")
        try:
            relative = candidate.relative_to(root.path)
        except ValueError:
            raise PathEscape("repository working directory is outside the root") from None
        return root.normalize(relative, allow_root=True)
    return root.normalize(raw, allow_root=True)


def _collect(
    process: subprocess.Popen[bytes],
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
) -> ProcessResult:
    stdout = process.stdout
    stderr = process.stderr
    if stdout is None or stderr is None:
        raise ProcessExecutionError()
    stdout_capture = _BoundedStream(stdout, stdout_limit)
    stderr_capture = _BoundedStream(stderr, stderr_limit)
    stdout_thread = threading.Thread(target=stdout_capture.drain, daemon=True)
    stderr_thread = threading.Thread(target=stderr_capture.drain, daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    try:
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_and_reap(process)
            return_code = process.returncode
        stdout_thread.join()
        stderr_thread.join()
    finally:
        with contextlib.suppress(OSError):
            stdout.close()
        with contextlib.suppress(OSError):
            stderr.close()
    if stdout_capture.error is not None or stderr_capture.error is not None:
        raise ProcessExecutionError()
    return ProcessResult(
        return_code=return_code,
        stdout=bytes(stdout_capture.data).decode("utf-8", errors="replace"),
        stderr=bytes(stderr_capture.data).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        stdout_original_byte_count=stdout_capture.original_byte_count,
        stderr_original_byte_count=stderr_capture.original_byte_count,
        stdout_truncated=stdout_capture.truncated,
        stderr_truncated=stderr_capture.truncated,
    )


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(OSError, ProcessLookupError):
        process.kill()
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        process.wait()


__all__ = ["ProcessExecutionError", "ProcessResult", "ProcessRunner"]
