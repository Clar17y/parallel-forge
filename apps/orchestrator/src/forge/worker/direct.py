"""DirectRunWorker: one autonomous CLI session per claimed direct run."""

from __future__ import annotations

import asyncio
import json
import logging
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

from forge.application.services.validation_failure_evidence import (
    build_failure_detail,
    extract_check_failure,
    merge_check_failures,
)
from forge.domain.direct import (
    RESULT_SCHEMA,
    REVIEW_CLIENT_PREFERENCE,
    DirectFinding,
    DirectLaunch,
    DirectResult,
    DirectReviewResult,
    DirectRunRecord,
    build_direct_launch,
    build_direct_prompt,
    build_direct_repair_prompt,
    build_direct_review_prompt,
    build_direct_review_repair_prompt,
    format_blocking_findings,
    parse_direct_result,
    parse_review_result,
)
from forge.domain.policy import CommandSpec, ProjectPolicy
from forge.domain.subscription_installations import load_subscription_installation_manifest
from forge.domain.validation import command_spec_digest
from forge.observability.redaction import RedactionPolicy, Redactor
from forge.persistence.repositories.direct import PostgresDirectRepository
from forge.settings import Settings

logger = logging.getLogger(__name__)

_RUNNABLE_CHECK_KINDS = frozenset({"test", "lint", "typecheck", "build"})


@dataclass(frozen=True, slots=True)
class _CheckRound:
    outcome: str  # passed | failed | error | cancelled
    detail: str = ""
    error_reason: str | None = None
    note: str | None = None


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
ReviewResolver = Callable[[DirectRunRecord, object], object | None]


class DirectRunWorker:
    """Claim one QUEUED direct run and run its single session to settlement."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        argv_factory: ArgvFactory | None = None,
        installation_resolver: InstallationResolver | None = None,
        review_resolver: ReviewResolver | None = None,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._repo = PostgresDirectRepository(factory)
        self._argv_factory = argv_factory
        self._installation_resolver = installation_resolver
        self._review_resolver = review_resolver
        self._redactor = Redactor(policy=RedactionPolicy(max_string_bytes=8192))
        self._active_run_id: UUID | None = None

    async def run_once(self) -> UUID | None:
        await self._settle_orphans()
        run = await self._repo.claim_next()
        if run is None:
            return None
        self._active_run_id = run.id
        try:
            await self._execute(run)
        except Exception:
            logger.exception("direct run %s failed in worker", run.id)
            try:
                current = await self._repo.get(run.id)
                if current.state in {"RUNNING", "FINALIZING", "CHECKING", "REVIEWING", "REPAIRING"}:
                    await self._repo.update_run(
                        run.id, state="FAILED", failure_reason="worker error"
                    )
            except Exception:  # noqa: BLE001, S110
                pass
        finally:
            self._active_run_id = None
        return run.id

    async def _settle_orphans(self) -> None:
        # Rule: a RUNNING/CHECKING/REPAIRING run with no live session pid is
        # orphaned. CHECKING/REPAIRING only exist inside a worker's active
        # run_once (skipped via _active_run_id, and a sequential worker never
        # sweeps mid-execution), so one seen here belongs to a dead worker.
        for run in await self._repo.running_runs():
            if run.id == self._active_run_id:
                continue
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

    def _resolve_review_installation(self, run: DirectRunRecord, implement: object) -> object | None:
        if self._review_resolver is not None:
            return self._review_resolver(run, implement)
        manifest = load_subscription_installation_manifest(
            self._settings.subscription_installations_path
        )
        if manifest is None:
            return None
        implement_client = str(getattr(implement, "client", run.client))
        order = {name: i for i, name in enumerate(REVIEW_CLIENT_PREFERENCE)}
        entries = sorted(
            manifest.installations,
            key=lambda e: order.get(str(getattr(e, "client", "")), len(order)),
        )
        for entry in entries:
            if str(getattr(entry, "client", "")) != implement_client:
                return entry
        return None

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
        transcript_dir = data_root / "direct-transcripts" / str(run.id)
        transcript_dir.mkdir(parents=True, exist_ok=True)
        installation = self._resolve_installation(run)
        has_agents_md = (worktree / "AGENTS.md").exists()
        prompt = build_direct_prompt(run.task_text, has_agents_md=has_agents_md)
        result = await self._launch_and_settle_session(
            run, installation, kind="implement", prompt=prompt,
            worktree=worktree, transcript_dir=transcript_dir,
        )
        if result is None:
            return
        head_sha = await self._commit_and_record(run, worktree, base_sha, result)
        summary = result.summary
        repairs_used = 0
        check_round = 0
        review_rounds = 0
        while True:
            # Inner Slice B check loop (separate counter from review rounds).
            check_note = ""
            while True:
                await self._repo.update_run(run.id, state="CHECKING", head_sha=head_sha)
                round_result = await self._run_checks(
                    run, worktree, head_sha, round_no=check_round
                )
                check_round += 1
                if round_result.outcome == "cancelled":
                    await self._repo.update_run(run.id, state="CANCELLED")
                    return
                if round_result.outcome == "passed":
                    check_note = f" ({round_result.note})" if round_result.note else ""
                    break
                if round_result.outcome == "error":
                    await self._repo.update_run(
                        run.id, state="FAILED", head_sha=head_sha,
                        failure_reason=(round_result.error_reason or "check error")[:512],
                    )
                    return
                detail = round_result.detail
                if repairs_used >= self._settings.direct_max_repair_rounds:
                    await self._repo.update_run(
                        run.id, state="FAILED", head_sha=head_sha,
                        summary=f"{summary}\n{detail}"[:8000],
                        failure_reason=(
                            f"checks still failing after "
                            f"{self._settings.direct_max_repair_rounds} repair rounds"
                        ),
                    )
                    return
                repairs_used += 1
                await self._repo.update_run(run.id, state="REPAIRING")
                repair_prompt = build_direct_repair_prompt(
                    run.task_text, detail, has_agents_md=has_agents_md
                )
                repair = await self._launch_and_settle_session(
                    run, installation, kind="repair", prompt=repair_prompt,
                    worktree=worktree, transcript_dir=transcript_dir,
                )
                if repair is None:
                    return
                previous_head = head_sha
                head_sha = await self._commit_and_record(run, worktree, previous_head, repair)
                summary = repair.summary or summary
                if head_sha == previous_head:
                    await self._repo.update_run(
                        run.id, state="FAILED", failure_reason="repair made no changes"
                    )
                    return
            # Slice C: cross-provider review after every passed CHECKING.
            await self._repo.update_run(run.id, state="REVIEWING", head_sha=head_sha)
            reviewer = self._resolve_review_installation(run, installation)
            if reviewer is None:
                await self._repo.update_run(
                    run.id, state="FAILED", head_sha=head_sha,
                    failure_reason="no distinct reviewer route",
                )
                return
            diff_text = await _git_diff(worktree, base_sha, head_sha)
            review_prompt = build_direct_review_prompt(
                run.task_text, diff_text, base_sha=base_sha, head_sha=head_sha,
                has_agents_md=has_agents_md,
            )
            review_round = review_rounds
            review_rounds += 1
            review = await self._launch_and_settle_review(
                run, reviewer, prompt=review_prompt, worktree=worktree,
                transcript_dir=transcript_dir, round_no=review_round,
            )
            if review is None:
                return
            blocking = tuple(f for f in review.findings if f.severity == "blocking")
            if not blocking:
                await self._repo.update_run(
                    run.id, state="DONE", head_sha=head_sha,
                    summary=f"{summary}{check_note}",
                )
                return
            if review_rounds >= self._settings.direct_max_review_rounds:
                await self._repo.update_run(
                    run.id, state="FAILED", head_sha=head_sha,
                    failure_reason=(
                        f"review still blocking after "
                        f"{self._settings.direct_max_review_rounds} rounds"
                    ),
                )
                return
            await self._repo.update_run(run.id, state="REPAIRING")
            review_repair_prompt = build_direct_review_repair_prompt(
                run.task_text, format_blocking_findings(review.findings),
                has_agents_md=has_agents_md,
            )
            repair = await self._launch_and_settle_session(
                run, installation, kind="repair", prompt=review_repair_prompt,
                worktree=worktree, transcript_dir=transcript_dir,
            )
            if repair is None:
                return
            previous_head = head_sha
            head_sha = await self._commit_and_record(run, worktree, previous_head, repair)
            summary = repair.summary or summary
            if head_sha == previous_head:
                await self._repo.update_run(
                    run.id, state="FAILED", failure_reason="repair made no changes"
                )
                return

    async def _commit_and_record(
        self, run: DirectRunRecord, worktree: Path, previous_head: str, result: DirectResult
    ) -> str:
        await self._repo.update_run(
            run.id, state="FINALIZING", summary=result.summary,
            result_degraded=result.degraded,
        )
        await _commit_worktree(worktree, previous_head, result.summary, run.id)
        head = await _git(str(worktree), "rev-parse", "HEAD")
        head_sha = head or previous_head
        await self._repo.update_run(run.id, head_sha=head_sha)
        return head_sha

    async def _launch_and_settle_session(
        self,
        run: DirectRunRecord,
        installation: object,
        *,
        kind: str,
        prompt: str,
        worktree: Path,
        transcript_dir: Path,
    ) -> DirectResult | None:
        """Run one session; settle the run and return None on terminal outcomes."""

        session = await self._repo.create_session(
            run_id=run.id, run=run, kind=kind,
            client=str(getattr(installation, "client", "")) or None,
            model=str(getattr(installation, "model", "")) or None,
        )
        transcript_path = transcript_dir / f"{session.id}.log"
        await self._repo.update_session(session.id, transcript_path=str(transcript_path))
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
        return await self._settle_session(run, session.id, outcome, final_file)

    async def _launch_and_settle_review(
        self, run: DirectRunRecord, reviewer: object, *, prompt: str,
        worktree: Path, transcript_dir: Path, round_no: int,
    ) -> DirectReviewResult | None:
        before = await _porcelain(worktree)
        session = await self._repo.create_session(
            run_id=run.id, run=run, kind="review",
            client=str(getattr(reviewer, "client", "")) or None,
            model=str(getattr(reviewer, "model", "")) or None,
        )
        transcript_path = transcript_dir / f"{session.id}.log"
        await self._repo.update_session(session.id, transcript_path=str(transcript_path))
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
            launch = self._argv_factory(reviewer, ctx)
        else:
            launch = build_direct_launch(
                reviewer, worktree=worktree, prompt=prompt, prompt_file=prompt_file,
                schema_file=schema_file, final_file=final_file,
                max_model_steps=self._settings.direct_max_model_steps,
            )
        outcome = await self._run_session(run, session.id, launch, worktree, transcript_path)
        # Post-hoc read-only enforcement before any verdict is applied.
        modified = await _revert_reviewer_changes(worktree, before)
        return await self._settle_review(
            run, session.id, outcome, final_file, reviewer=reviewer,
            round_no=round_no, modified=modified,
        )

    async def _settle_review(
        self, run: DirectRunRecord, session_id: UUID, outcome: tuple[str, int | None, str], final_file: Path,
        *, reviewer: object, round_no: int, modified: bool,
    ) -> DirectReviewResult | None:
        # Precedence (locked): DirectResult parse first ONLY for
        # needs_decision/blocked detection; otherwise review parse.
        # exit 0 + unparseable review JSON => FAILED, never clean.
        kind, exit_code, collected_text = outcome
        now = datetime.now(UTC)
        if kind == "cancelled":
            await self._repo.update_session(session_id, state="KILLED", ended_at=now, failure_reason="cancelled")
            await self._repo.update_run(run.id, state="CANCELLED")
            return None
        if kind == "timeout":
            await self._repo.update_session(session_id, state="TIMED_OUT", ended_at=now, failure_reason="watchdog timeout")
            await self._repo.update_run(run.id, state="FAILED", failure_reason="session timed out")
            return None
        if exit_code not in (0, None):
            await self._repo.update_session(session_id, state="FAILED", ended_at=now, failure_reason=f"exit code {exit_code}")
            await self._repo.update_run(run.id, state="FAILED", failure_reason=f"session exited with code {exit_code}")
            return None
        final_text = collected_text
        if final_file.exists():
            raw_final = final_file.read_text(encoding="utf-8", errors="replace")
            redacted_final, _ = self._redactor.redact_text(raw_final)
            final_text = (redacted_final + "\n" + collected_text)[-262144:]
        await self._repo.update_session(session_id, state="SUCCEEDED", ended_at=now)
        direct = parse_direct_result(final_text)
        if not direct.degraded and direct.status == "needs_decision":
            await self._repo.update_run(run.id, state="NEEDS_DECISION", summary=direct.summary, question=direct.question, options=direct.options, result_degraded=direct.degraded)
            return None
        if not direct.degraded and direct.status == "blocked":
            await self._repo.update_run(run.id, state="FAILED", summary=direct.summary, failure_reason=(direct.summary or "session blocked")[:512], result_degraded=direct.degraded)
            return None
        review = parse_review_result(final_text)
        if review is None:
            await self._repo.update_run(run.id, state="FAILED", failure_reason="review result unparseable")
            return None
        summary_r, _ = self._redactor.redact_text(review.summary)
        findings_payload_items: list[dict[str, object]] = []
        redacted_findings: list[DirectFinding] = []
        for finding in review.findings:
            detail_r, _ = self._redactor.redact_text(finding.detail)
            file_r: str | None = None
            if finding.file is not None:
                file_r, _ = self._redactor.redact_text(finding.file)
            redacted_findings.append(
                DirectFinding(severity=finding.severity, file=file_r, detail=detail_r[:1000])
            )
            findings_payload_items.append(
                {"severity": finding.severity, "file": file_r, "detail": detail_r[:1000]}
            )
        findings_payload = tuple(findings_payload_items)
        redacted_review = DirectReviewResult(
            verdict=review.verdict, summary=summary_r,
            findings=tuple(redacted_findings), degraded=review.degraded,
        )
        await self._repo.create_review(
            run_id=run.id, session_id=session_id, round=round_no,
            client=str(getattr(reviewer, "client", "")), model=str(getattr(reviewer, "model", "")),
            verdict=review.verdict, findings=findings_payload, summary=summary_r,
            degraded=review.degraded, reviewer_modified_worktree=modified,
        )
        return redacted_review

    async def _settle_session(
        self,
        run: DirectRunRecord,
        session_id: UUID,
        outcome: tuple[str, int | None, str],
        final_file: Path,
    ) -> DirectResult | None:
        kind, exit_code, collected_text = outcome
        now = datetime.now(UTC)
        if kind == "cancelled":
            await self._repo.update_session(
                session_id, state="KILLED", ended_at=now, failure_reason="cancelled"
            )
            await self._repo.update_run(run.id, state="CANCELLED")
            return None
        if kind == "timeout":
            await self._repo.update_session(
                session_id, state="TIMED_OUT", ended_at=now, failure_reason="watchdog timeout"
            )
            await self._repo.update_run(
                run.id, state="FAILED", failure_reason="session timed out"
            )
            return None
        if exit_code not in (0, None):
            await self._repo.update_session(
                session_id, state="FAILED", ended_at=now,
                failure_reason=f"exit code {exit_code}",
            )
            await self._repo.update_run(
                run.id, state="FAILED", failure_reason=f"session exited with code {exit_code}"
            )
            return None
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
            return None
        if result.status == "blocked":
            await self._repo.update_run(
                run.id, state="FAILED", summary=result.summary,
                failure_reason=(result.summary or "session blocked")[:512],
                result_degraded=result.degraded,
            )
            return None
        return result

    async def _run_checks(
        self, run: DirectRunRecord, worktree: Path, head_sha: str, *, round_no: int
    ) -> _CheckRound:
        tree_sha = await _git(str(worktree), "rev-parse", "HEAD^{tree}") or head_sha
        policy_pair = await self._repo.policy_for(run.id)
        if policy_pair is None:
            return _CheckRound(outcome="passed", note="no checks: no project policy")
        policy, policy_version = policy_pair
        runnable = [
            command for command in policy.commands
            if str(command.kind) in _RUNNABLE_CHECK_KINDS and command.path_scope is None
        ]
        skipped = [
            command.name for command in policy.commands if command not in runnable
        ]
        if not runnable:
            note = "no checks: no runnable commands"
            if skipped:
                note += f" (skipped: {', '.join(skipped)})"
            return _CheckRound(outcome="passed", note=note)
        transcript_dir = Path(self._settings.data_root) / "direct-transcripts" / str(run.id)
        transcript_dir.mkdir(parents=True, exist_ok=True)
        log_path = transcript_dir / f"checks-r{round_no}.log"
        evidences = []
        any_failed = False
        for command in runnable:
            digest = command_spec_digest(command)
            cached = await self._repo.cached_check(
                run_id=run.id, tree_sha=tree_sha, command_digest=digest,
                policy_version=policy_version,
            )
            if cached is not None:
                await self._repo.create_check(
                    run_id=run.id, round=round_no, command_name=command.name,
                    kind=str(command.kind), tree_sha=tree_sha, command_digest=digest,
                    policy_version=policy_version, status="CACHED",
                    exit_code=cached.exit_code, duration_ms=cached.duration_ms,
                    failure_excerpt=cached.failure_excerpt,
                )
                if cached.status == "ERROR":
                    return _CheckRound(
                        outcome="error",
                        error_reason=cached.failure_excerpt or "check error",
                    )
                if cached.status == "FAILED":
                    any_failed = True
                    evidences.append(
                        extract_check_failure(
                            command.name,
                            _envelope("stdout", cached.failure_excerpt or ""),
                        )
                    )
                continue
            env, missing = _check_env(command, policy, worktree)
            if missing is not None:
                reason = f"missing environment: {missing}"
                await self._repo.create_check(
                    run_id=run.id, round=round_no, command_name=command.name,
                    kind=str(command.kind), tree_sha=tree_sha, command_digest=digest,
                    policy_version=policy_version, status="ERROR",
                    failure_excerpt=reason,
                )
                return _CheckRound(outcome="error", error_reason=reason)
            executed = await self._execute_check(run, command, worktree, env, log_path)
            if executed is None:
                return _CheckRound(outcome="cancelled")
            status, exit_code, duration_ms, stdout_text, stderr_text, timed_out = executed
            if status == "ERROR":
                await self._repo.create_check(
                    run_id=run.id, round=round_no, command_name=command.name,
                    kind=str(command.kind), tree_sha=tree_sha, command_digest=digest,
                    policy_version=policy_version, status="ERROR",
                    duration_ms=duration_ms, failure_excerpt=stderr_text,
                )
                return _CheckRound(outcome="error", error_reason=stderr_text)
            if timed_out:
                reason = (
                    f"check timed out after "
                    f"{self._settings.direct_check_timeout_seconds}s"
                )
                await self._repo.create_check(
                    run_id=run.id, round=round_no, command_name=command.name,
                    kind=str(command.kind), tree_sha=tree_sha, command_digest=digest,
                    policy_version=policy_version, status="ERROR",
                    duration_ms=duration_ms, failure_excerpt=reason,
                )
                return _CheckRound(outcome="error", error_reason=reason)
            evidence = merge_check_failures(
                extract_check_failure(command.name, _envelope("stdout", stdout_text)),
                extract_check_failure(command.name, _envelope("stderr", stderr_text)),
            )
            excerpt = build_failure_detail([evidence]) if status == "FAILED" else None
            await self._repo.create_check(
                run_id=run.id, round=round_no, command_name=command.name,
                kind=str(command.kind), tree_sha=tree_sha, command_digest=digest,
                policy_version=policy_version, status=status, exit_code=exit_code,
                duration_ms=duration_ms, failure_excerpt=excerpt,
            )
            if status == "FAILED":
                any_failed = True
                evidences.append(evidence)
        if any_failed:
            return _CheckRound(outcome="failed", detail=build_failure_detail(evidences))
        pass_note: str | None = f"skipped: {', '.join(skipped)}" if skipped else None
        return _CheckRound(outcome="passed", note=pass_note)

    async def _execute_check(
        self,
        run: DirectRunRecord,
        command: CommandSpec,
        worktree: Path,
        env: dict[str, str],
        log_path: Path,
    ) -> tuple[str, int | None, int, str, str, bool] | None:
        """Return (status, exit, ms, stdout, stderr, timed_out); None = cancelled."""

        started = monotonic()
        timeout = float(self._settings.direct_check_timeout_seconds)
        try:
            proc = await asyncio.create_subprocess_exec(
                *_spawn_argv(tuple(command.argv)),
                cwd=str(worktree),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=(os.name != "nt"),
            )
        except OSError as error:
            reason = f"check could not start: {type(error).__name__}"
            return ("ERROR", None, 0, "", reason, False)
        collected: dict[str, list[str]] = {"stdout": [], "stderr": []}

        async def _drain(stream: asyncio.StreamReader | None, name: str) -> None:
            if stream is None:
                return
            with log_path.open("a", encoding="utf-8") as handle:
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace")
                    redacted, _ = self._redactor.redact_text(text.rstrip("\n"))
                    handle.write(f"[{name}] {redacted}\n")
                    handle.flush()
                    collected[name].append(redacted)

        tasks = [
            asyncio.create_task(_drain(proc.stdout, "stdout")),
            asyncio.create_task(_drain(proc.stderr, "stderr")),
        ]
        timed_out = False
        cancelled = False
        while not all(task.done() for task in tasks) or proc.returncode is None:
            if monotonic() - started > timeout:
                timed_out = True
                await _kill_tree(proc)
                break
            if await self._repo.cancel_requested(run.id):
                cancelled = True
                await _kill_tree(proc)
                break
            await asyncio.sleep(0.1)
        await asyncio.gather(*tasks, return_exceptions=True)
        exit_code = await proc.wait()
        duration_ms = int((monotonic() - started) * 1000)
        if cancelled:
            return None
        status = "PASSED" if exit_code == 0 and not timed_out else "FAILED"
        return (
            status, exit_code, duration_ms,
            "\n".join(collected["stdout"]), "\n".join(collected["stderr"]), timed_out,
        )

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

async def _git_text(cwd: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(cwd), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", errors="replace")


async def _git_diff(worktree: Path, base_sha: str, head_sha: str) -> str:
    return await _git_text(worktree, "diff", f"{base_sha}..{head_sha}")


async def _porcelain(worktree: Path) -> dict[str, str]:
    text = await _git_text(worktree, "status", "--porcelain")
    out: dict[str, str] = {}
    for line in text.splitlines():
        if len(line) >= 3:
            out[line[3:].strip().strip('"')] = line[:2]
    return out


async def _revert_reviewer_changes(worktree: Path, before: dict[str, str]) -> bool:
    after = await _porcelain(worktree)
    if after == before:
        return False
    await _git_raw(str(worktree), "restore", "--", ".")
    for path, status in after.items():
        if path in before:
            continue
        if not status.startswith("??"):
            continue
        target = worktree / path.rstrip("/")
        try:
            if target.is_dir() and not target.is_symlink():
                import shutil
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists() or target.is_symlink():
                target.unlink()
        except OSError:
            pass
    return True


def _spawn_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Route Windows batch shims (e.g. muse.cmd) through cmd.exe.

    CreateProcess cannot execute .cmd/.bat files directly, and the
    manifest names muse.cmd for Muse on this platform.
    """

    if sys.platform == "win32" and argv and argv[0].lower().endswith((".cmd", ".bat")):
        return ("cmd.exe", "/c", *argv)
    return argv


def _envelope(stream: str, text: str) -> bytes:
    return json.dumps({"stream": stream, "text": text}).encode()


def _check_env(
    command: CommandSpec, policy: ProjectPolicy, worktree: Path
) -> tuple[dict[str, str], str | None]:
    """Worker env first, then declared keys from allowed env files only."""

    env = dict(os.environ)
    declared = set(command.environment_keys)
    if not declared:
        return env, None
    from_files: dict[str, str] = {}
    for relative in policy.allowed_environment_files:
        path = worktree / relative
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            if key in declared and key not in from_files:
                from_files[key] = value.strip().strip('"').strip("'")
    for key in command.environment_keys:
        if key in env:
            continue
        if key in from_files:
            env[key] = from_files[key]
            continue
        return env, key
    return env, None


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
