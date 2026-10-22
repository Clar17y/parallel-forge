"""DirectRunWorker: one autonomous CLI session per claimed direct run."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.direct import (
    RESULT_SCHEMA,
    DirectLaunch,
    DirectRunRecord,
    build_direct_launch,
    build_direct_prompt,
    parse_direct_result,
)
from forge.domain.subscription_installations import load_subscription_installation_manifest
from forge.observability.redaction import RedactionPolicy, Redactor
from forge.persistence.repositories.direct import PostgresDirectRepository
from forge.settings import Settings


@dataclass(frozen=True, slots=True)
class DirectLaunchContext:
    run: DirectRunRecord
    session_id: UUID
    worktree: Path
    prompt: str
    prompt_file: Path
    schema_file: Path
    final_file: Path
    settings: Settings


ArgvFactory = Callable[[object, DirectLaunchContext], DirectLaunch]
InstallationResolver = Callable[[DirectRunRecord], object | None]


class DirectRunWorker:
    """Claim one QUEUED direct run and run its single session to settlement."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        argv_factory: ArgvFactory | None = None,
        installation_resolver: InstallationResolver | None = None,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._repo = PostgresDirectRepository(factory)
        self._argv_factory = argv_factory
        self._installation_resolver = installation_resolver
        self._redactor = Redactor(policy=RedactionPolicy(max_string_bytes=8192))

    async def run_once(self) -> UUID | None:
        await self._settle_orphans()
        run = await self._repo.claim_next()
        if run is None:
            return None
        try:
            await self._execute(run)
        except Exception:  # noqa: BLE001 - settle the claimed run, never leave it RUNNING
            try:
                current = await self._repo.get(run.id)
                if current.state in {"RUNNING", "FINALIZING"}:
                    await self._repo.update_run(
                        run.id, state="FAILED", failure_reason="worker error"
                    )
            except Exception:  # noqa: BLE001, S110
                pass
        return run.id

    async def _settle_orphans(self) -> None:
        for run in await self._repo.running_runs():
            sessions = await self._repo.sessions(run.id)
            alive = any(s.pid and _pid_alive(s.pid) for s in sessions)
            if alive:
                continue
            for session in sessions:
                if session.state == "RUNNING":
                    await self._repo.update_session(
                        session.id, state="FAILED", ended_at=datetime.now(UTC),
                        failure_reason="worker interrupted",
                    )
            await self._repo.update_run(
                run.id, state="FAILED", failure_reason="worker interrupted"
            )

    def _resolve_installation(self, run: DirectRunRecord) -> object:
        if self._installation_resolver is not None:
            resolved = self._installation_resolver(run)
            if resolved is None:
                raise ValueError("unknown direct route")
            return resolved
        manifest = load_subscription_installation_manifest(
            self._settings.subscription_installations_path
        )
        if manifest is not None:
            for entry in manifest.installations:
                if (
                    entry.client == run.client
                    and entry.model == run.model
                    and (run.effort is None or entry.effort == run.effort)
                ):
                    return entry
        raise ValueError("unknown direct route")

    async def _execute(self, run: DirectRunRecord) -> None:
        canonical, default_branch = await self._repo.project_for(run.id)
        data_root = Path(self._settings.data_root)
        worktree = data_root / "direct-worktrees" / str(run.id)
        branch = f"forge/direct-{str(run.id)[:8]}"
        base_sha = await _git(canonical, "rev-parse", default_branch)
        if base_sha is None:
            await self._repo.update_run(
                run.id, state="FAILED", failure_reason="base branch unavailable"
            )
            return
        worktree.parent.mkdir(parents=True, exist_ok=True)
        created = await _git_raw(
            canonical, "worktree", "add", "-b", branch, str(worktree), default_branch
        )
        if created != 0:
            await self._repo.update_run(
                run.id, state="FAILED", failure_reason="worktree creation failed"
            )
            return
        run = await self._repo.update_run(
            run.id, branch=branch, worktree_path=str(worktree), base_sha=base_sha
        )
        session = await self._repo.create_session(run_id=run.id, run=run)
        transcript_dir = data_root / "direct-transcripts" / str(run.id)
        transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = transcript_dir / f"{session.id}.log"
        await self._repo.update_session(session.id, transcript_path=str(transcript_path))

        installation = self._resolve_installation(run)
        prompt = build_direct_prompt(
            run.task_text, has_agents_md=(worktree / "AGENTS.md").exists()
        )
        prompt_file = transcript_dir / f"{session.id}.prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        schema_file = transcript_dir / f"{session.id}.schema.json"
        schema_file.write_text(json.dumps(RESULT_SCHEMA), encoding="utf-8")
        final_file = transcript_dir / f"{session.id}.final.txt"
        ctx = DirectLaunchContext(
            run=run, session_id=session.id, worktree=worktree, prompt=prompt,
            prompt_file=prompt_file, schema_file=schema_file, final_file=final_file,
            settings=self._settings,
        )
        if self._argv_factory is not None:
            launch = self._argv_factory(installation, ctx)
        else:
            launch = build_direct_launch(
                installation, worktree=worktree, prompt=prompt, prompt_file=prompt_file,
                schema_file=schema_file, final_file=final_file,
                max_model_steps=self._settings.direct_max_model_steps,
            )
        outcome = await self._run_session(run, session.id, launch, worktree, transcript_path)
        await self._settle(run, session.id, outcome, worktree, base_sha, final_file)

    async def _run_session(
        self,
        run: DirectRunRecord,
        session_id: UUID,
        launch: DirectLaunch,
        worktree: Path,
        transcript_path: Path,
    ) -> tuple[str, int | None, str]:
        """Return (kind, exit_code, redacted output): kind in exited|cancelled|timeout."""

        silence = float(self._settings.direct_silence_timeout_seconds)
        duration_limit = float(self._settings.direct_session_timeout_seconds)
        proc = await asyncio.create_subprocess_exec(
            *_spawn_argv(launch.argv),
            cwd=str(worktree),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
        )
        await self._repo.update_session(session_id, pid=proc.pid)
        if launch.stdin_data is not None:
            try:
                proc.stdin.write(launch.stdin_data)  # type: ignore[union-attr]
                await proc.stdin.drain()  # type: ignore[union-attr]
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            proc.stdin.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001, S110
            pass

        started = monotonic()
        last_output = started
        output_bytes = 0
        last_db_update = 0.0
        collected: list[str] = []
        kind = "exited"
        assert proc.stdout is not None
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def _reader() -> None:
            try:
                while True:
                    line = await proc.stdout.readline()  # type: ignore[union-attr]
                    if not line:
                        break
                    await queue.put(line)
            finally:
                await queue.put(None)

        reader = asyncio.create_task(_reader())
        with transcript_path.open("a", encoding="utf-8") as handle:
            while True:
                now = monotonic()
                if now - last_output > silence or now - started > duration_limit:
                    kind = "timeout"
                    await _kill_tree(proc)
                    break
                if now - last_db_update >= 0.5:
                    last_db_update = now
                    if await self._repo.cancel_requested(run.id):
                        kind = "cancelled"
                        await _kill_tree(proc)
                        break
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=0.25)
                except TimeoutError:
                    continue
                if line is None:
                    break
                last_output = monotonic()
                text = line.decode("utf-8", errors="replace")
                redacted, _ = self._redactor.redact_text(text.rstrip("\n"))
                stamp = datetime.now(UTC).isoformat()
                handle.write(f"[{stamp}] {redacted}\n")
                handle.flush()
                output_bytes += len(line)
                collected.append(redacted)
                if len(collected) > 2000:
                    collected = collected[-2000:]
                if monotonic() - last_db_update >= 2 or output_bytes % 65536 < len(line):
                    last_db_update = monotonic()
                    await self._repo.update_session(
                        session_id, last_output_at=datetime.now(UTC),
                        output_bytes=output_bytes,
                    )
        await reader
        exit_code = await proc.wait()
        await self._repo.update_session(
            session_id, last_output_at=datetime.now(UTC), output_bytes=output_bytes,
            exit_code=exit_code,
        )
        return kind, exit_code, "\n".join(collected)[-262144:]

    async def _settle(
        self,
        run: DirectRunRecord,
        session_id: UUID,
        outcome: tuple[str, int | None, str],
        worktree: Path,
        base_sha: str,
        final_file: Path,
    ) -> None:
        kind, exit_code, collected_text = outcome
        now = datetime.now(UTC)
        if kind == "cancelled":
            await self._repo.update_session(
                session_id, state="KILLED", ended_at=now, failure_reason="cancelled"
            )
            await self._repo.update_run(run.id, state="CANCELLED")
            return
        if kind == "timeout":
            await self._repo.update_session(
                session_id, state="TIMED_OUT", ended_at=now, failure_reason="watchdog timeout"
            )
            await self._repo.update_run(
                run.id, state="FAILED", failure_reason="session timed out"
            )
            return
        if exit_code not in (0, None):
            await self._repo.update_session(
                session_id, state="FAILED", ended_at=now,
                failure_reason=f"exit code {exit_code}",
            )
            await self._repo.update_run(
                run.id, state="FAILED", failure_reason=f"session exited with code {exit_code}"
            )
            return
        final_text = collected_text
        if final_file.exists():
            raw_final = final_file.read_text(encoding="utf-8", errors="replace")
            redacted_final, _ = self._redactor.redact_text(raw_final)
            final_text = (redacted_final + "\n" + collected_text)[-262144:]
        result = parse_direct_result(final_text)
        await self._repo.update_session(session_id, state="SUCCEEDED", ended_at=now)
        if result.status == "needs_decision":
            await self._repo.update_run(
                run.id, state="NEEDS_DECISION", summary=result.summary,
                question=result.question, options=result.options,
                result_degraded=result.degraded,
            )
            return
        if result.status == "blocked":
            await self._repo.update_run(
                run.id, state="FAILED", summary=result.summary,
                failure_reason=(result.summary or "session blocked")[:512],
                result_degraded=result.degraded,
            )
            return
        await self._repo.update_run(run.id, state="FINALIZING", summary=result.summary,
                                    result_degraded=result.degraded)
        head_sha = await _commit_worktree(worktree, base_sha, result.summary, run.id)
        await self._repo.update_run(run.id, state="DONE", head_sha=head_sha)


def _spawn_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Route Windows batch shims (e.g. muse.cmd) through cmd.exe.

    CreateProcess cannot execute .cmd/.bat files directly, and the
    manifest names muse.cmd for Muse on this platform.
    """

    if sys.platform == "win32" and argv and argv[0].lower().endswith((".cmd", ".bat")):
        return ("cmd.exe", "/c", *argv)
    return argv


async def _git(cwd: str, *args: str) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", cwd, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", errors="replace").strip()


async def _git_raw(cwd: str, *args: str) -> int:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", cwd, *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait()


async def _commit_worktree(worktree: Path, base_sha: str, summary: str | None, run_id: UUID) -> str:
    await _git_raw(str(worktree), "add", "-A")
    status = await _git(str(worktree), "status", "--porcelain")
    if not status:
        return base_sha
    subject = (summary or "").strip().splitlines()[0][:200] if summary else ""
    if not subject:
        subject = f"Direct run {str(run_id)[:8]}"
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(worktree),
        "-c", "user.email=forge@localhost", "-c", "user.name=Forge",
        "commit", "-m", subject,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    head = await _git(str(worktree), "rev-parse", "HEAD")
    return head or base_sha


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    pid = proc.pid
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            os.killpg(pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except TimeoutError:
        proc.kill()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


__all__ = ["DirectLaunchContext", "DirectRunWorker"]
