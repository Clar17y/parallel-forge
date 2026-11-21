"""Inspect Linux provider session groups during direct-worker restart recovery."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from enum import Enum, auto
from pathlib import Path
from time import monotonic
from typing import NamedTuple


class _State(Enum):
    STOPPED = auto()
    LIVE = auto()
    UNCONFIRMED = auto()
    RESCAN = auto()


class _ProcStat(NamedTuple):
    state: bytes
    group: int
    session: int
    threads: int

    @property
    def stopped(self) -> bool:
        # A main thread can be a zombie while its other threads still execute.
        return self.state in (b"Z", b"X", b"x") and self.threads == 1


def _read_stat(entry: Path) -> _ProcStat:
    # The command name can contain arbitrary bytes, spaces and closing parentheses.
    prefix, separator, tail = (entry / "stat").read_bytes().rpartition(b") ")
    if not separator or not prefix.startswith(entry.name.encode("ascii") + b" ("):
        raise ValueError("invalid process stat")
    fields = tail.split(maxsplit=18)
    if len(fields) < 18 or len(fields[0]) != 1:
        raise ValueError("incomplete process stat")
    threads = int(fields[17])  # Field 20, num_threads.
    if threads < 1:
        raise ValueError("invalid process thread count")
    return _ProcStat(fields[0], int(fields[2]), int(fields[3]), threads)


def linux_pid_alive(pid: int) -> bool:
    """Observe exit without waiting on (or reaping) another process's child."""
    if pid <= 0:
        return False
    try:
        return not _read_stat(Path("/proc") / str(pid)).stopped
    except (OSError, ValueError):
        # A restricted or unavailable procfs cannot by itself prove exit.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            pass
        return True


def _member_state(entry: Path, pgid: int) -> _State | None:
    """Return None for a process outside this group or one that disappeared."""
    try:
        member = _read_stat(entry)
    except (OSError, ValueError):
        if sys.platform == "linux":
            try:
                if os.getpgid(int(entry.name)) != pgid:
                    return None
            except ProcessLookupError:
                return None
        return _State.UNCONFIRMED
    if member.group != pgid:
        return None
    if member.session != pgid:
        return _State.UNCONFIRMED
    return _State.STOPPED if member.stopped else _State.LIVE


def _same_pid_namespace(proc_root: Path) -> bool:
    # NSpid lists IDs from the namespace of this procfs mount to our own.
    try:
        lines = (proc_root / "self" / "status").read_bytes().splitlines()
    except OSError:
        return False
    for line in lines:
        if line.startswith(b"NSpid:"):
            return line.split()[1:] == [str(os.getpid()).encode("ascii")]
    return False


def _proc_lists_all_pids(proc_root: Path) -> bool:
    # With hidepid a visible zombie can share its group with an invisible,
    # still-running member. Only trust a zombie-only scan on an unrestricted mount.
    saw_proc = False
    for line in (proc_root / "self" / "mounts").read_bytes().splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[1] != b"/proc":
            continue
        if fields[2] != b"proc":
            return False
        for option in fields[3].split(b","):
            if option.startswith(b"hidepid=") and option not in (b"hidepid=0", b"hidepid=off"):
                return False
        saw_proc = True
    return saw_proc


def _signal_group(pgid: int, sig: int) -> bool:
    """True for an existing group, false for ESRCH. Other errors are inconclusive."""
    if sys.platform != "linux":
        raise OSError("Linux process groups unavailable")
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


def _listed_pids(proc_root: Path) -> set[str]:
    return {entry.name for entry in proc_root.iterdir() if entry.name.isdecimal()}


def _scan_members(pgid: int, proc_root: Path, pids: set[str]) -> _State | None:
    observed = None
    for pid in pids:
        member = _member_state(proc_root / pid, pgid)
        if member in (_State.LIVE, _State.UNCONFIRMED):
            return member
        if member is _State.STOPPED:
            observed = member
    return observed


def _group_state(pgid: int, proc_root: Path) -> _State:
    if not _signal_group(pgid, 0):
        return _State.STOPPED
    leader = _member_state(proc_root / str(pgid), pgid)
    if leader in (_State.LIVE, _State.UNCONFIRMED):
        return leader

    # The group can outlive its leader. Check every remaining process and
    # distinguish an unreaped exit from a zombie leader with active threads.
    pids = _listed_pids(proc_root)
    observed = _scan_members(pgid, proc_root, pids)
    if observed in (_State.LIVE, _State.UNCONFIRMED):
        return observed
    if observed is _State.STOPPED and _proc_lists_all_pids(proc_root):
        # A departing member can fork after the first listing. Inspect just the
        # new arrivals before repeating a whole scan on a busy machine.
        added = _listed_pids(proc_root) - pids
        if added:
            observed = _scan_members(pgid, proc_root, added)
            if observed in (_State.LIVE, _State.UNCONFIRMED):
                return observed
            if _listed_pids(proc_root) - pids - added:
                return _State.RESCAN
        return _State.STOPPED
    # The group may have disappeared during the scan.
    return _State.UNCONFIRMED if _signal_group(pgid, 0) else _State.STOPPED


async def _poll_group(
    pgid: int, proc_root: Path, timeout: float, *, wait_on: tuple[_State, ...],
) -> _State:
    deadline = monotonic() + timeout
    while True:
        state = _group_state(pgid, proc_root)
        remaining = deadline - monotonic()
        if state not in wait_on or remaining <= 0:
            return state
        await asyncio.sleep(min(0.05, remaining))


async def stop_linux_session_group(pgid: int, *, proc_root: Path = Path("/proc")) -> bool:
    """Return true only after the recorded dedicated group has ceased running."""
    if sys.platform != "linux":
        return False
    # Our own PID could not have been allocated while the old group still existed.
    if pgid <= 0 or pgid == os.getpid():
        return True
    if pgid in (os.getpgrp(), os.getsid(0)):
        return False
    try:
        if not _same_pid_namespace(proc_root):
            return not _signal_group(pgid, 0)
        state = await _poll_group(pgid, proc_root, 1.0, wait_on=(_State.RESCAN,))
        for sig, grace in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 2.0)):
            if state is not _State.LIVE:
                break
            if not _signal_group(pgid, sig):
                return True
            state = await _poll_group(
                pgid, proc_root, grace, wait_on=(_State.LIVE, _State.UNCONFIRMED, _State.RESCAN)
            )
        return state is _State.STOPPED
    except OSError:
        return False
