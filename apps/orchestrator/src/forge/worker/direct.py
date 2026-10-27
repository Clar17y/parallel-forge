"""DirectRunWorker: one autonomous CLI session per claimed direct run."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sys
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Protocol
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
    REVIEW_SCHEMA,
    DirectFinding,
    DirectLaunch,
    DirectPrFeedback,
    DirectPrSnapshot,
    DirectResult,
    DirectReviewResult,
    DirectRunRecord,
    build_direct_launch,
    build_direct_plan_prompt,
    build_direct_pr_repair_prompt,
    build_direct_prompt,
    build_direct_repair_prompt,
    build_direct_review_prompt,
    build_direct_review_repair_prompt,
    format_blocking_findings,
    format_pr_feedback,
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
_MAX_LINE_BYTES = 262144
_TAIL_BYTES = 262144
_TRUNCATED_LINE = b" ...[line truncated]"
_OUTPUT_TRUNCATED = "[output truncated]"


class _CommitFailure(RuntimeError):
    """A candidate commit (or the git probes around it) failed."""


class _SessionBudgetExhausted(RuntimeError):
    """The run's session budget was exhausted before a launch."""


def _is_trusted_pr_author(author: str, repo: str, configured: str) -> bool:
    login = (author or "").strip()
    if not login:
        return False
    lowered = login.lower()
    if lowered.endswith("[bot]"):
        return True
    owner = repo.split("/", 1)[0].strip() if "/" in repo else ""
    if owner and lowered == owner.lower():
        return True
    configured_logins = {
        part.strip().lower() for part in configured.split(",") if part.strip()
    }
    return lowered in configured_logins


async def _iter_lines(stream: asyncio.StreamReader, *, max_line_bytes: int = _MAX_LINE_BYTES) -> AsyncIterator[bytes]:
    """Yield lines from chunked reads; no single line can exhaust the reader.

    Lines longer than ``max_line_bytes`` are yielded once, truncated with a
    marker, and the remainder of that line is discarded.
    """

    buffer = bytearray()
    discarding = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            if buffer and not discarding:
                yield bytes(buffer)
            return
        buffer.extend(chunk)
        while True:
            index = buffer.find(b"\n")
            if index < 0:
                if len(buffer) > max_line_bytes:
                    if not discarding:
                        yield bytes(buffer[:max_line_bytes]) + _TRUNCATED_LINE + b"\n"
                    discarding = True
                    buffer.clear()
                break
            line = bytes(buffer[: index + 1])
            del buffer[: index + 1]
            if discarding:
                discarding = False
                continue
            if len(line) > max_line_bytes:
                yield line[:max_line_bytes] + _TRUNCATED_LINE + b"\n"
            else:
                yield line


class _BoundedTail:
    """Byte-budgeted tail of decoded lines for failure-evidence extraction."""

    def __init__(self, max_bytes: int = _TAIL_BYTES) -> None:
        self._max = max_bytes
        self._parts: list[str] = []
        self._size = 0

    def append(self, text: str) -> None:
        self._parts.append(text)
        self._size += len(text.encode("utf-8", errors="replace"))
        while self._size > self._max and len(self._parts) > 1:
            dropped = self._parts.pop(0)
            self._size -= len(dropped.encode("utf-8", errors="replace"))
        if self._size > self._max and self._parts:
            encoded = self._parts[0].encode("utf-8", errors="replace")[-self._max :]
            self._parts[0] = encoded.decode("utf-8", errors="replace")
            self._size = len(encoded)

    def text(self) -> str:
        return "\n".join(self._parts)


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


class PublisherError(RuntimeError):
    """A git push or gh operation failed (details never carry env values)."""


class DirectPublisher(Protocol):
    async def push(self, worktree: Path, branch: str) -> None: ...
    async def open_pr(self, *, repo: str, base: str, head: str, title: str, body: str, worktree: Path) -> tuple[int, str]: ...
    async def poll(self, *, repo: str, number: int) -> DirectPrSnapshot: ...
    async def reply(self, *, repo: str, number: int, feedback: DirectPrFeedback, body: str) -> None: ...
    async def resolve(self, *, repo: str, feedback: DirectPrFeedback) -> None: ...
    async def close_pr(self, *, repo: str, number: int) -> None: ...


async def _run_publisher_argv(argv: tuple[str, ...], cwd: Path | None = None) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(*argv, cwd=str(cwd) if cwd else None, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _err = await proc.communicate()
    except OSError as error:
        raise PublisherError(f"publisher command could not start: {type(error).__name__}") from None
    if proc.returncode != 0:
        raise PublisherError(f"publisher command failed with exit code {proc.returncode}")
    return out.decode("utf-8", errors="replace")


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class GhPublisher:
    """Default publisher: git push in the worktree, gh on the trusted host."""

    async def push(self, worktree: Path, branch: str) -> None:
        await _run_publisher_argv(("git", "-C", str(worktree), "push", "-u", "origin", branch))

    async def open_pr(self, *, repo: str, base: str, head: str, title: str, body: str, worktree: Path) -> tuple[int, str]:
        body_file: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
                handle.write(body)
                body_file = Path(handle.name)
            out = await _run_publisher_argv(("gh", "pr", "create", "--repo", repo, "--base", base, "--head", head, "--title", title[:200], "--body-file", str(body_file)), cwd=worktree)
        finally:
            if body_file is not None:
                body_file.unlink(missing_ok=True)
        url = ""
        for token in out.split():
            if token.startswith("https://") and "/pull/" in token:
                url = token.strip()
                break
        if url:
            match = re.search(r"/pull/(\d+)", url)
            if match:
                return int(match.group(1)), url[:1024]
        # Fallback: ask gh for the PR just created for this head.
        view = await _run_publisher_argv(("gh", "pr", "view", head, "--repo", repo, "--json", "number,url"), cwd=worktree)
        try:
            data = json.loads(view)
            return int(data["number"]), str(data["url"])[:1024]
        except (ValueError, KeyError, TypeError) as error:
            raise PublisherError("pr creation output unparseable") from error

    async def poll(self, *, repo: str, number: int) -> DirectPrSnapshot:
        view_out = await _run_publisher_argv(("gh", "pr", "view", str(number), "--repo", repo, "--json", "state,mergeable,reviewDecision,statusCheckRollup,url,createdAt,updatedAt"))
        try:
            view = json.loads(view_out)
        except ValueError as error:
            raise PublisherError("pr view output unparseable") from error
        if not isinstance(view, dict):
            raise PublisherError("pr view output unparseable")
        checks_status = "NONE"
        rollup = view.get("statusCheckRollup")
        if isinstance(rollup, list) and rollup:
            states: list[str] = []
            for item in rollup:
                if not isinstance(item, dict):
                    continue
                conclusion = str(item.get("conclusion") or item.get("state") or "")
                status = str(item.get("status") or "")
                if conclusion in {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"}:
                    states.append("FAILURE")
                elif status and status != "COMPLETED":
                    states.append("PENDING")
                elif conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
                    states.append("SUCCESS")
                else:
                    states.append("PENDING")
            if "FAILURE" in states:
                checks_status = "FAILURE"
            elif "PENDING" in states:
                checks_status = "PENDING"
            elif states:
                checks_status = "SUCCESS"
        threads: list[DirectPrFeedback] = []
        changes: list[DirectPrFeedback] = []
        activity_times: list[datetime] = []
        has_activity = False
        if "/" in repo:
            owner, name = repo.split("/", 1)
            query = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){nodes{id isResolved comments(first:20){nodes{id databaseId body author{login} path createdAt updatedAt}}}} reviews(first:100){nodes{id state body author{login} submittedAt}}}}}"
            gql_out = await _run_publisher_argv(("gh", "api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}"))
            try:
                gql = json.loads(gql_out)
                pr = gql["data"]["repository"]["pullRequest"]
                if not isinstance(pr, dict):
                    raise PublisherError("pr graphql output unparseable")
                for node in pr.get("reviewThreads", {}).get("nodes", []):
                    if not isinstance(node, dict):
                        continue
                    has_activity = True
                    comments = node.get("comments", {}).get("nodes", []) if isinstance(node.get("comments"), dict) else []
                    first = comments[0] if comments and isinstance(comments[0], dict) else {}
                    stamp = _parse_dt(first.get("updatedAt") or first.get("createdAt"))
                    if stamp:
                        activity_times.append(stamp)
                    if node.get("isResolved"):
                        continue
                    author_value = first.get("author")
                    author = author_value if isinstance(author_value, dict) else {}
                    threads.append(DirectPrFeedback(id=str(node.get("id", "")), thread_id=str(node.get("id", "")), comment_id=str(first["databaseId"]) if isinstance(first.get("databaseId"), int) else None, body=str(first.get("body") or "")[:4000], author=str(author.get("login") or ""), path=str(first.get("path")) if first.get("path") else None, updated_at=stamp))
                for node in pr.get("reviews", {}).get("nodes", []):
                    if not isinstance(node, dict):
                        continue
                    has_activity = True
                    stamp = _parse_dt(node.get("submittedAt"))
                    if stamp:
                        activity_times.append(stamp)
                    if node.get("state") == "CHANGES_REQUESTED":
                        author_value = node.get("author")
                        author = author_value if isinstance(author_value, dict) else {}
                        changes.append(DirectPrFeedback(id=str(node.get("id", "")), body=str(node.get("body") or "")[:4000], author=str(author.get("login") or ""), updated_at=stamp))
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise PublisherError("pr graphql output unparseable") from error
        updated = _parse_dt(view.get("updatedAt"))
        if updated and has_activity:
            activity_times.append(updated)
        return DirectPrSnapshot(state=str(view.get("state") or "OPEN"), mergeable=str(view.get("mergeable") or ""), checks_status=checks_status, unresolved_threads=tuple(threads), changes_requested=tuple(changes), last_activity_at=max(activity_times) if activity_times else None, has_activity=has_activity, created_at=_parse_dt(view.get("createdAt")))

    async def reply(self, *, repo: str, number: int, feedback: DirectPrFeedback, body: str) -> None:
        if feedback.comment_id:
            await _run_publisher_argv(("gh", "api", f"repos/{repo}/pulls/{number}/comments/{feedback.comment_id}/replies", "-f", f"body={body[:1000]}"))
        else:
            await _run_publisher_argv(("gh", "pr", "comment", str(number), "--repo", repo, "--body", body[:1000]))

    async def resolve(self, *, repo: str, feedback: DirectPrFeedback) -> None:
        if not feedback.thread_id:
            return
        mutation = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id}}}"
        await _run_publisher_argv(("gh", "api", "graphql", "-f", f"query={mutation}", "-F", f"id={feedback.thread_id}"))

    async def close_pr(self, *, repo: str, number: int) -> None:
        await _run_publisher_argv(("gh", "pr", "close", str(number), "--repo", repo))


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
        publisher: DirectPublisher | None = None,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._repo = PostgresDirectRepository(factory)
        self._argv_factory = argv_factory
        self._installation_resolver = installation_resolver
        self._review_resolver = review_resolver
        self._publisher = publisher
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
                if current.state in {"PLANNING", "RUNNING", "FINALIZING", "CHECKING", "REVIEWING", "REPAIRING", "PUBLISHING", "PR_OPEN"}:
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
            # Single sequential worker: a non-active run has no live
            # supervisor, so a still-live recorded pid is reclaimed here.
            for session in sessions:
                if session.state == "RUNNING" and session.pid and _pid_alive(session.pid):
                    await _kill_pid_tree(session.pid)
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
        # A run resumed after plan approval already has its worktree recorded;
        # reuse it instead of recreating branch and worktree.
        resumed = bool(
            run.worktree_path and run.base_sha and run.branch
            and Path(run.worktree_path).exists()
        )
        if resumed:
            worktree = Path(run.worktree_path or "")
            branch = run.branch or ""
            base_sha = run.base_sha or ""
        else:
            worktree = data_root / "direct-worktrees" / str(run.id)
            branch = f"forge/direct-{str(run.id)[:8]}"
            _base_code, _base_out = await _run_git(canonical, "rev-parse", default_branch)
            resolved_base = _base_out.strip() if _base_code == 0 else None
            if resolved_base is None:
                await self._repo.update_run(
                    run.id, state="FAILED", failure_reason="base branch unavailable"
                )
                return
            base_sha = resolved_base
            worktree.parent.mkdir(parents=True, exist_ok=True)
            created, _created_out = await _run_git(
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
        plan_text = run.plan_text
        if run.shape == "planned" and not plan_text:
            await self._repo.update_run(run.id, state="PLANNING")
            planned = await self._launch_and_settle_plan(
                run, installation,
                prompt=build_direct_plan_prompt(run.task_text, has_agents_md=has_agents_md),
                worktree=worktree, transcript_dir=transcript_dir,
            )
            if planned is None:
                return
            plan_text = planned
            run = await self._repo.update_run(run.id, plan_text=plan_text)
            if run.review_plan_first and not run.plan_approved:
                await self._repo.update_run(run.id, state="AWAITING_PLAN_APPROVAL")
                return
            await self._repo.update_run(run.id, state="RUNNING")
        prompt = build_direct_prompt(
            run.task_text, has_agents_md=has_agents_md, plan=plan_text
        )
        result = await self._launch_and_settle_session(
            run, installation, kind="implement", prompt=prompt,
            worktree=worktree, transcript_dir=transcript_dir,
        )
        if result is None:
            return
        head_sha = await self._commit_and_record(run, worktree, base_sha, result)
        if head_sha is None:
            return
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
                    run.task_text, detail, has_agents_md=has_agents_md, plan=plan_text
                )
                repaired = await self._execute_repair_step(
                    run, installation, prompt=repair_prompt, worktree=worktree,
                    transcript_dir=transcript_dir, head_sha=head_sha, summary=summary,
                )
                if repaired is None:
                    return
                head_sha, summary = repaired
            # Slice C: cross-provider review after every passed CHECKING.
            if self._settings.direct_review_policy == "skip":
                await self._publish_and_watch(run, worktree=worktree, branch=branch, head_sha=head_sha, summary=f"{summary}{check_note} (review skipped by owner policy)", installation=installation, transcript_dir=transcript_dir, has_agents_md=has_agents_md, check_round=check_round)
                return
            await self._repo.update_run(run.id, state="REVIEWING", head_sha=head_sha)
            reviewer = self._resolve_review_installation(run, installation)
            if reviewer is None and self._settings.direct_review_policy == "same_provider":
                reviewer = installation
            if reviewer is None:
                await self._repo.update_run(
                    run.id, state="FAILED", head_sha=head_sha,
                    failure_reason="no distinct reviewer route",
                )
                return
            diff_text = await _git_diff(worktree, base_sha, head_sha)
            review_prompt = build_direct_review_prompt(
                run.task_text, diff_text, base_sha=base_sha, head_sha=head_sha,
                has_agents_md=has_agents_md, plan=plan_text,
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
            if review.verdict == "clean" and not blocking:
                await self._publish_and_watch(run, worktree=worktree, branch=branch, head_sha=head_sha, summary=f"{summary}{check_note}", installation=installation, transcript_dir=transcript_dir, has_agents_md=has_agents_md, check_round=check_round)
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
                has_agents_md=has_agents_md, plan=plan_text,
            )
            repaired = await self._execute_repair_step(
                run, installation, prompt=review_repair_prompt, worktree=worktree,
                transcript_dir=transcript_dir, head_sha=head_sha, summary=summary,
            )
            if repaired is None:
                return
            head_sha, summary = repaired

    def _log_pr_watch(self, transcript_dir: Path, message: str) -> None:
        try:
            transcript_dir.mkdir(parents=True, exist_ok=True)
            redacted, _ = self._redactor.redact_text(message[:2000])
            with (transcript_dir / "pr-watch.log").open("a", encoding="utf-8") as handle:
                handle.write(f"[{datetime.now(UTC).isoformat()}] {redacted}\n")
        except OSError:
            pass

    async def _pr_body(self, run: DirectRunRecord, summary: str) -> str:
        lines = [summary[:4000], "", "Checks:"]
        try:
            checks = await self._repo.checks(run.id)
        except Exception:  # noqa: BLE001
            checks = ()
        if checks:
            for check in checks[-32:]:
                lines.append(f"- {check.command_name}: {check.status} (round {check.round})")
        else:
            lines.append("- no checks recorded")
        lines.append("")
        lines.append("Reviews:")
        try:
            reviews = await self._repo.reviews(run.id)
        except Exception:  # noqa: BLE001
            reviews = ()
        if reviews:
            for review in reviews[-16:]:
                lines.append(f"- {review.client}/{review.model}: {review.verdict} (round {review.round})")
        else:
            lines.append("- no reviews recorded")
        lines.append("")
        lines.append(f"Opened by Forge direct run {run.id}")
        body, _ = self._redactor.redact_text("\n".join(lines))
        return body[:8000]

    async def _publish_and_watch(self, run: DirectRunRecord, *, worktree: Path, branch: str, head_sha: str, summary: str, installation: object, transcript_dir: Path, has_agents_md: bool, check_round: int) -> None:
        github_repo, default_branch = await self._repo.publication_for(run.id)
        if not github_repo.strip():
            await self._repo.update_run(run.id, state="DONE", head_sha=head_sha, summary=f"{summary}\npublication skipped: no GitHub repository")
            return
        publisher: DirectPublisher = self._publisher if self._publisher is not None else GhPublisher()
        await self._repo.update_run(run.id, state="PUBLISHING", head_sha=head_sha)
        try:
            await publisher.push(worktree, branch)
        except Exception:  # noqa: BLE001
            self._log_pr_watch(transcript_dir, "push failed")
            await self._repo.update_run(run.id, state="FAILED", head_sha=head_sha, failure_reason="pr push failed")
            return
        title_raw = run.task_text.strip().splitlines()[0] if run.task_text.strip() else ""
        title_redacted, _ = self._redactor.redact_text(title_raw)
        title = title_redacted.strip()[:200] or f"Forge direct run {str(run.id)[:8]}"
        body = await self._pr_body(run, summary)
        try:
            pr_number, pr_url = await publisher.open_pr(repo=github_repo, base=default_branch, head=branch, title=title, body=body, worktree=worktree)
        except Exception:  # noqa: BLE001
            self._log_pr_watch(transcript_dir, "pr creation failed")
            await self._repo.update_run(run.id, state="FAILED", head_sha=head_sha, failure_reason="pr creation failed")
            return
        if not pr_url or pr_number is None:
            await self._repo.update_run(run.id, state="FAILED", head_sha=head_sha, failure_reason="pr creation failed")
            return
        pr_url = pr_url[:1024]
        await self._repo.update_run(run.id, state="PR_OPEN", head_sha=head_sha, pr_number=int(pr_number), pr_url=pr_url)
        self._log_pr_watch(transcript_dir, f"pr opened {pr_url}")
        started = monotonic()
        pr_created_at = datetime.now(UTC)
        last_push_at = pr_created_at
        handled: set[str] = set()
        ignored_untrusted: set[str] = set()
        consecutive_failures = 0
        pr_rounds = 0
        seen_activity = False
        last_activity_at: datetime | None = None
        current_summary = summary
        while True:
            if await self._repo.cancel_requested(run.id):
                await self._repo.update_run(run.id, state="CANCELLED", head_sha=head_sha)
                return
            if monotonic() - started > float(self._settings.direct_pr_watch_timeout_seconds):
                await self._repo.update_run(run.id, state="FAILED", head_sha=head_sha, failure_reason="pr watch timed out")
                return
            try:
                snapshot = await publisher.poll(repo=github_repo, number=int(pr_number))
            except Exception:  # noqa: BLE001
                consecutive_failures += 1
                self._log_pr_watch(transcript_dir, f"poll failure {consecutive_failures}")
                if consecutive_failures >= 3:
                    await self._repo.update_run(run.id, state="FAILED", head_sha=head_sha, failure_reason="pr watch failed")
                    return
                await asyncio.sleep(float(self._settings.direct_pr_poll_seconds))
                continue
            consecutive_failures = 0
            self._log_pr_watch(transcript_dir, f"poll state={snapshot.state} mergeable={snapshot.mergeable} checks={snapshot.checks_status} threads={len(snapshot.unresolved_threads)} changes_requested={len(snapshot.changes_requested)}")
            if snapshot.created_at is not None:
                pr_created_at = snapshot.created_at if snapshot.created_at.tzinfo else snapshot.created_at.replace(tzinfo=UTC)
            if snapshot.has_activity:
                seen_activity = True
                if snapshot.last_activity_at is not None:
                    stamp = snapshot.last_activity_at if snapshot.last_activity_at.tzinfo else snapshot.last_activity_at.replace(tzinfo=UTC)
                    last_activity_at = stamp if last_activity_at is None else max(last_activity_at, stamp)
            trusted_threads: list[DirectPrFeedback] = []
            trusted_changes: list[DirectPrFeedback] = []
            for feedback in snapshot.unresolved_threads:
                if _is_trusted_pr_author(feedback.author, github_repo, self._settings.direct_trusted_pr_authors):
                    trusted_threads.append(feedback)
                else:
                    key = f"thread:{feedback.thread_id or feedback.id}"
                    if key not in ignored_untrusted:
                        ignored_untrusted.add(key)
                        self._log_pr_watch(transcript_dir, f"ignored untrusted pr feedback from {feedback.author}")
            for feedback in snapshot.changes_requested:
                if _is_trusted_pr_author(feedback.author, github_repo, self._settings.direct_trusted_pr_authors):
                    trusted_changes.append(feedback)
                else:
                    key = f"review:{feedback.id}:{feedback.updated_at.isoformat() if feedback.updated_at else ''}"
                    if key not in ignored_untrusted:
                        ignored_untrusted.add(key)
                        self._log_pr_watch(transcript_dir, f"ignored untrusted pr feedback from {feedback.author}")
            batch: list[tuple[str, DirectPrFeedback]] = []
            for feedback in trusted_threads:
                key = f"thread:{feedback.thread_id or feedback.id}"
                if key not in handled:
                    batch.append((key, feedback))
            for feedback in trusted_changes:
                if feedback.updated_at is not None:
                    stamp = feedback.updated_at if feedback.updated_at.tzinfo else feedback.updated_at.replace(tzinfo=UTC)
                    if stamp <= last_push_at:
                        continue
                key = f"review:{feedback.id}:{feedback.updated_at.isoformat() if feedback.updated_at else ''}"
                if key not in handled:
                    batch.append((key, feedback))
            if batch:
                if pr_rounds >= self._settings.direct_max_pr_rounds:
                    await self._repo.update_run(run.id, state="FAILED", head_sha=head_sha, failure_reason=f"pr comments still open after {self._settings.direct_max_pr_rounds} rounds")
                    return
                pr_rounds += 1
                await self._repo.update_run(run.id, state="REPAIRING", pr_rounds=pr_rounds)
                feedback_tuple = tuple(item for _key, item in batch)
                formatted = format_pr_feedback(feedback_tuple)
                redacted_detail, _ = self._redactor.redact_text(formatted)
                repair_prompt = build_direct_pr_repair_prompt(run.task_text, redacted_detail, has_agents_md=has_agents_md, plan=run.plan_text)
                repaired = await self._execute_repair_step(run, installation, prompt=repair_prompt, worktree=worktree, transcript_dir=transcript_dir, head_sha=head_sha, summary=current_summary)
                if repaired is None:
                    return
                head_sha, current_summary = repaired
                await self._repo.update_run(run.id, state="CHECKING", head_sha=head_sha)
                check_result = await self._run_checks(run, worktree, head_sha, round_no=check_round)
                check_round += 1
                if check_result.outcome == "cancelled":
                    await self._repo.update_run(run.id, state="CANCELLED", head_sha=head_sha)
                    return
                if check_result.outcome == "failed":
                    await self._repo.update_run(run.id, state="FAILED", head_sha=head_sha, failure_reason="checks failing after pr repair")
                    return
                if check_result.outcome == "error":
                    await self._repo.update_run(run.id, state="FAILED", head_sha=head_sha, failure_reason=(check_result.error_reason or "check error")[:512])
                    return
                await self._repo.update_run(run.id, state="PUBLISHING", head_sha=head_sha)
                try:
                    await publisher.push(worktree, branch)
                except Exception:  # noqa: BLE001
                    await self._repo.update_run(run.id, state="FAILED", head_sha=head_sha, failure_reason="pr push failed")
                    return
                last_push_at = datetime.now(UTC)
                await self._repo.update_run(run.id, state="PR_OPEN", head_sha=head_sha)
                reply_text, _ = self._redactor.redact_text(f"Addressed in {head_sha}: {current_summary}")
                try:
                    for _key, feedback in batch:
                        await publisher.reply(repo=github_repo, number=int(pr_number), feedback=feedback, body=reply_text[:1000])
                        if feedback.thread_id:
                            await publisher.resolve(repo=github_repo, feedback=feedback)
                except Exception:  # noqa: BLE001
                    await self._repo.update_run(run.id, state="FAILED", head_sha=head_sha, failure_reason="pr watch failed")
                    return
                handled.update(key for key, _item in batch)
                continue
            if snapshot.state != "OPEN":
                await self._repo.update_run(run.id, state="FAILED", head_sha=head_sha, failure_reason="pr watch failed")
                return
            has_newer_changes = False
            for feedback in trusted_changes:
                if feedback.updated_at is None:
                    has_newer_changes = True
                else:
                    stamp = feedback.updated_at if feedback.updated_at.tzinfo else feedback.updated_at.replace(tzinfo=UTC)
                    if stamp > last_push_at:
                        has_newer_changes = True
            if snapshot.mergeable == "MERGEABLE" and snapshot.checks_status in {"SUCCESS", "NONE"} and not trusted_threads and not has_newer_changes:
                now_dt = datetime.now(UTC)
                quiet_seconds = float(self._settings.direct_pr_quiet_seconds)
                if seen_activity:
                    reference = last_activity_at or pr_created_at
                    reference = max(reference, last_push_at)
                    if (now_dt - reference).total_seconds() >= quiet_seconds:
                        await self._repo.update_run(run.id, state="DONE", head_sha=head_sha, summary=current_summary)
                        return
                elif (now_dt - pr_created_at).total_seconds() >= quiet_seconds:
                    await self._repo.update_run(run.id, state="DONE", head_sha=head_sha, summary=current_summary)
                    return
            await asyncio.sleep(float(self._settings.direct_pr_poll_seconds))


    async def _execute_repair_step(
        self, run: DirectRunRecord, installation: object, *, prompt: str,
        worktree: Path, transcript_dir: Path, head_sha: str, summary: str,
    ) -> tuple[str, str] | None:
        repair = await self._launch_and_settle_session(
            run, installation, kind="repair", prompt=prompt,
            worktree=worktree, transcript_dir=transcript_dir,
        )
        if repair is None:
            return None
        previous_head = head_sha
        new_head = await self._commit_and_record(run, worktree, previous_head, repair)
        if new_head is None:
            return None
        new_summary = repair.summary or summary
        if new_head == previous_head:
            await self._repo.update_run(
                run.id, state="FAILED", head_sha=new_head,
                failure_reason="repair made no changes",
            )
            return None
        return new_head, new_summary

    async def _commit_and_record(
        self, run: DirectRunRecord, worktree: Path, previous_head: str, result: DirectResult
    ) -> str | None:
        await self._repo.update_run(
            run.id, summary=result.summary,
            result_degraded=result.degraded,
        )
        try:
            head_sha = await _commit_worktree(worktree, previous_head, result.summary, run.id)
        except _CommitFailure:
            await self._repo.update_run(
                run.id, state="FAILED", failure_reason="commit failed"
            )
            return None
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

        try:
            session_id, outcome, final_file = await self._launch_raw_session(
                run, installation, kind=kind, prompt=prompt, worktree=worktree,
                transcript_dir=transcript_dir,
            )
        except _SessionBudgetExhausted:
            return None
        return await self._settle_session(run, session_id, outcome, final_file)

    async def _session_budget_exhausted(self, run_id: UUID) -> bool:
        sessions = await self._repo.sessions(run_id)
        if len(sessions) >= self._settings.direct_max_sessions_per_run:
            return True
        total_seconds = 0.0
        for session in sessions:
            started = getattr(session, "started_at", None)
            ended = getattr(session, "ended_at", None)
            if started is None or ended is None:
                continue
            try:
                total_seconds += (ended - started).total_seconds()
            except TypeError:
                continue
        return total_seconds >= float(self._settings.direct_max_total_session_seconds)

    async def _launch_raw_session(
        self,
        run: DirectRunRecord,
        installation: object,
        *,
        kind: str,
        prompt: str,
        worktree: Path,
        transcript_dir: Path,
    ) -> tuple[UUID, tuple[str, int | None, str], Path]:
        if await self._session_budget_exhausted(run.id):
            await self._repo.update_run(
                run.id, state="FAILED",
                failure_reason="direct run session budget exhausted",
            )
            raise _SessionBudgetExhausted
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
        schema_file.write_text(json.dumps(REVIEW_SCHEMA if kind == "review" else RESULT_SCHEMA), encoding="utf-8")
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
        return session.id, outcome, final_file

    async def _launch_and_settle_plan(
        self, run: DirectRunRecord, installation: object, *, prompt: str,
        worktree: Path, transcript_dir: Path,
    ) -> str | None:
        """Run the read-only plan session; return its plan text or None."""

        launched = await self._launch_read_only_session(
            run, installation, kind="plan", noun="planner", prompt=prompt,
            worktree=worktree, transcript_dir=transcript_dir,
        )
        if launched is None:
            return None
        session_id, outcome, final_file, modified = launched
        result = await self._settle_session(run, session_id, outcome, final_file)
        if result is None:
            return None
        plan_text, _ = self._redactor.redact_text(result.summary)
        plan_text = plan_text[:8000]
        if modified:
            plan_text += "\n(planner modified the worktree; changes were reverted)"
        return plan_text

    async def _launch_read_only_session(
        self, run: DirectRunRecord, installation: object, *, kind: str, noun: str,
        prompt: str, worktree: Path, transcript_dir: Path,
    ) -> tuple[UUID, tuple[str, int | None, str], Path, bool] | None:
        """Launch a read-only session and revert any edits before returning.

        Returns (session_id, outcome, final_file, modified), or None when the
        run was settled terminally (budget exhausted, unrestorable edits).
        """

        before_head, before = await _snapshot_worktree(worktree)
        try:
            session_id, outcome, final_file = await self._launch_raw_session(
                run, installation, kind=kind, prompt=prompt, worktree=worktree,
                transcript_dir=transcript_dir,
            )
        except _SessionBudgetExhausted:
            return None
        # Post-hoc read-only enforcement before any result is applied.
        modified, restored = await _revert_reviewer_changes(worktree, before_head, before)
        if modified and not restored:
            reason = f"{noun} modified worktree; candidate could not be restored"
            await self._repo.update_session(
                session_id, state="FAILED", ended_at=datetime.now(UTC),
                failure_reason=reason,
            )
            await self._repo.update_run(run.id, state="FAILED", failure_reason=reason)
            return None
        return session_id, outcome, final_file, modified

    async def _launch_and_settle_review(
        self, run: DirectRunRecord, reviewer: object, *, prompt: str,
        worktree: Path, transcript_dir: Path, round_no: int,
    ) -> DirectReviewResult | None:
        launched = await self._launch_read_only_session(
            run, reviewer, kind="review", noun="reviewer", prompt=prompt,
            worktree=worktree, transcript_dir=transcript_dir,
        )
        if launched is None:
            return None
        session_id, outcome, final_file, modified = launched
        return await self._settle_review(
            run, session_id, outcome, final_file, reviewer=reviewer,
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
        if review.verdict == "blocking" and not any(
            finding.severity == "blocking" for finding in review.findings
        ):
            review = DirectReviewResult(
                verdict=review.verdict, summary=review.summary,
                findings=(
                    *review.findings,
                    DirectFinding(
                        severity="blocking", file=None,
                        detail=review.summary or "reviewer returned a blocking verdict without findings",
                    ),
                ),
                degraded=review.degraded,
            )
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
        _tree_code, _tree_out = await _run_git(worktree, "rev-parse", "HEAD^{tree}")
        tree_sha = (_tree_out.strip() if _tree_code == 0 else None) or head_sha
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
        tails: dict[str, _BoundedTail] = {"stdout": _BoundedTail(), "stderr": _BoundedTail()}
        file_bytes = 0
        file_truncated = False

        async def _drain(stream: asyncio.StreamReader | None, name: str) -> None:
            nonlocal file_bytes, file_truncated
            if stream is None:
                return
            with log_path.open("a", encoding="utf-8") as handle:
                async for line in _iter_lines(stream):
                    text = line.decode("utf-8", errors="replace")
                    redacted, _ = self._redactor.redact_text(text.rstrip("\n"))
                    tails[name].append(redacted)
                    if file_truncated:
                        continue
                    entry = f"[{name}] {redacted}\n"
                    if file_bytes + len(entry.encode("utf-8")) > self._settings.direct_max_check_output_bytes:
                        handle.write(f"{_OUTPUT_TRUNCATED}\n")
                        handle.flush()
                        file_truncated = True
                        continue
                    handle.write(entry)
                    handle.flush()
                    file_bytes += len(entry.encode("utf-8"))

        tasks = [
            asyncio.create_task(_drain(proc.stdout, "stdout")),
            asyncio.create_task(_drain(proc.stderr, "stderr")),
        ]
        timed_out = False
        cancelled = False
        try:
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
        finally:
            if proc.returncode is None:
                try:
                    await asyncio.shield(_kill_tree(proc))
                except Exception:  # noqa: BLE001, S110
                    pass
                try:
                    await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=10)
                except Exception:  # noqa: BLE001, S110
                    pass
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        duration_ms = int((monotonic() - started) * 1000)
        if cancelled:
            return None
        status = "PASSED" if exit_code == 0 and not timed_out else "FAILED"
        return (
            status, exit_code, duration_ms,
            tails["stdout"].text(), tails["stderr"].text(), timed_out,
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
        reader: asyncio.Task[None] | None = None
        try:
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
            transcript_bytes = 0
            transcript_truncated = False
            last_cancel_check = 0.0
            last_progress_update = 0.0
            collected = _BoundedTail()
            kind = "exited"
            stream_eof = False
            assert proc.stdout is not None
            stdout_stream = proc.stdout
            queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=128)

            async def _reader() -> None:
                try:
                    async for line in _iter_lines(stdout_stream):
                        await queue.put(line)
                finally:
                    try:
                        queue.put_nowait(None)
                    except asyncio.QueueFull:
                        pass

            reader = asyncio.create_task(_reader())
            with transcript_path.open("a", encoding="utf-8") as handle:
                while True:
                    now = monotonic()
                    if now - last_output > silence or now - started > duration_limit:
                        kind = "timeout"
                        await _kill_tree(proc)
                        break
                    if now - last_cancel_check >= 0.5:
                        last_cancel_check = now
                        if await self._repo.cancel_requested(run.id):
                            kind = "cancelled"
                            await _kill_tree(proc)
                            break
                    if stream_eof:
                        if proc.returncode is not None:
                            break
                        await asyncio.sleep(0.05)
                        continue
                    try:
                        line = await asyncio.wait_for(queue.get(), timeout=0.25)
                    except TimeoutError:
                        if reader.done() and queue.empty():
                            stream_eof = True
                        continue
                    if line is None:
                        stream_eof = True
                        continue
                    last_output = monotonic()
                    text = line.decode("utf-8", errors="replace")
                    redacted, _ = self._redactor.redact_text(text.rstrip("\n"))
                    output_bytes += len(line)
                    collected.append(redacted)
                    if not transcript_truncated:
                        entry = f"[{datetime.now(UTC).isoformat()}] {redacted}\n"
                        entry_bytes = len(entry.encode("utf-8"))
                        if transcript_bytes + entry_bytes > self._settings.direct_max_transcript_bytes:
                            handle.write(f"[{datetime.now(UTC).isoformat()}] {_OUTPUT_TRUNCATED}\n")
                            handle.flush()
                            transcript_truncated = True
                        else:
                            handle.write(entry)
                            handle.flush()
                            transcript_bytes += entry_bytes
                    if monotonic() - last_progress_update >= 2 or output_bytes % 65536 < len(line):
                        last_progress_update = monotonic()
                        await self._repo.update_session(
                            session_id, last_output_at=datetime.now(UTC),
                            output_bytes=output_bytes,
                        )
            exit_code = await proc.wait()
            await self._repo.update_session(
                session_id, last_output_at=datetime.now(UTC), output_bytes=output_bytes,
                exit_code=exit_code,
            )
            return kind, exit_code, collected.text()[-262144:]
        finally:
            if reader is not None and not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if proc.returncode is None:
                try:
                    await asyncio.shield(_kill_tree(proc))
                except Exception:  # noqa: BLE001, S110
                    pass
                try:
                    await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=10)
                except Exception:  # noqa: BLE001, S110
                    pass


async def _run_git(cwd: Path | str, *args: str) -> tuple[int | None, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(cwd), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode("utf-8", errors="replace")


async def _git_diff(worktree: Path, base_sha: str, head_sha: str) -> str:
    _code, text = await _run_git(worktree, "diff", f"{base_sha}..{head_sha}")
    return text


async def _porcelain(worktree: Path) -> dict[str, str]:
    _code, text = await _run_git(worktree, "status", "--porcelain")
    out: dict[str, str] = {}
    for line in text.splitlines():
        if len(line) >= 3:
            out[line[3:].strip().strip('"')] = line[:2]
    return out


async def _snapshot_worktree(worktree: Path) -> tuple[str | None, dict[str, str]]:
    _code, out = await _run_git(worktree, "rev-parse", "HEAD")
    head = out.strip() if _code == 0 and out.strip() else None
    return head, await _porcelain(worktree)


def _remove_worktree_path(worktree: Path, relative: str) -> None:
    target = worktree / relative.rstrip("/")
    try:
        if target.is_dir() and not target.is_symlink():
            import shutil

            shutil.rmtree(target, ignore_errors=True)
        elif target.exists() or target.is_symlink():
            target.unlink()
    except OSError:
        pass


async def _revert_reviewer_changes(
    worktree: Path, before_head: str | None, before: dict[str, str]
) -> tuple[bool, bool]:
    """Return (modified, restored) for a read-only review session."""

    after_head, after = await _snapshot_worktree(worktree)
    modified = after != before or after_head != before_head
    if not modified:
        return False, True
    if before_head is None:
        return True, False
    reset_code, _reset_out = await _run_git(worktree, "reset", "--hard", before_head)
    if reset_code != 0:
        return True, False
    after_restore = await _porcelain(worktree)
    for path, status in after_restore.items():
        if path in before:
            continue
        if not status.startswith("??"):
            continue
        _remove_worktree_path(worktree, path)
    restored_head, restored_status = await _snapshot_worktree(worktree)
    restored = restored_head == before_head and restored_status == before
    return True, restored


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


_CHECK_ENV_BASE_WINDOWS = (
    "PATH", "PATHEXT", "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
)
_CHECK_ENV_BASE_POSIX = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "SHELL",
)


def _check_env(
    command: CommandSpec, policy: ProjectPolicy, worktree: Path
) -> tuple[dict[str, str], str | None]:
    """Minimal platform base plus exactly the command's declared keys."""

    base_names = _CHECK_ENV_BASE_WINDOWS if os.name == "nt" else _CHECK_ENV_BASE_POSIX
    env = {name: os.environ[name] for name in base_names if name in os.environ}
    # Windows environment lookups are case-insensitive; PATH may be "Path".
    if os.name == "nt" and "PATH" not in env:
        for key, value in os.environ.items():
            if key.upper() == "PATH":
                env["PATH"] = value
                break
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
        if key in os.environ:
            env[key] = os.environ[key]
            continue
        if key in from_files:
            env[key] = from_files[key]
            continue
        return env, key
    return env, None


async def _commit_worktree(worktree: Path, base_sha: str, summary: str | None, run_id: UUID) -> str:
    add_code, _add_out = await _run_git(worktree, "add", "-A")
    if add_code != 0:
        raise _CommitFailure("commit failed: git add failed")
    _status_code, _status_out = await _run_git(worktree, "status", "--porcelain")
    if _status_code != 0:
        raise _CommitFailure("commit failed: git status failed")
    status = _status_out.strip()
    if not status:
        _head_code, _head_out = await _run_git(worktree, "rev-parse", "HEAD")
        head = _head_out.strip() if _head_code == 0 else None
        if not head:
            raise _CommitFailure("commit failed: git rev-parse failed")
        return head
    subject = (summary or "").strip().splitlines()[0][:200] if summary else ""
    if not subject:
        subject = f"Direct run {str(run_id)[:8]}"
    commit_code, _commit_out = await _run_git(
        worktree,
        "-c", "user.email=forge@localhost", "-c", "user.name=Forge",
        "commit", "-m", subject,
    )
    if commit_code != 0:
        raise _CommitFailure("commit failed: git commit failed")
    _head_code, _head_out = await _run_git(worktree, "rev-parse", "HEAD")
    head = _head_out.strip() if _head_code == 0 else None
    if not head:
        raise _CommitFailure("commit failed: git rev-parse failed")
    return head


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


async def _kill_pid_tree(pid: int) -> None:
    """Kill a process tree by pid (orphaned sessions are not our children)."""

    if pid <= 0:
        return
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            os.killpg(pid, signal.SIGTERM)
            for _ in range(20):
                if not _pid_alive(pid):
                    return
                await asyncio.sleep(0.05)
            os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED
        if not handle:
            return False
        try:
            # A terminated process object stays openable until reaped;
            # liveness requires the STILL_ACTIVE (259) exit code.
            exit_code = ctypes.c_ulong(0)
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            ):
                return False
            return exit_code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


__all__ = ["DirectLaunchContext", "DirectPublisher", "DirectRunWorker", "GhPublisher", "PublisherError"]
