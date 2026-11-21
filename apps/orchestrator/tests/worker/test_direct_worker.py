"""DirectRunWorker integration tests with a fake CLI (no real providers)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.services.direct_runs import DirectRunService
from forge.domain.direct import DirectLaunch, DirectPrSnapshot


class _QuietPublisher:
    async def push(self, worktree, branch): pass
    async def open_pr(self, *, repo, base, head, title, body, worktree): return 1, "https://example.test/pr/1"
    async def poll(self, *, repo, number):
        from datetime import UTC, datetime
        past = datetime(2000, 1, 1, tzinfo=UTC)
        return DirectPrSnapshot(has_activity=True, last_activity_at=past, created_at=past)
    async def reply(self, *, repo, number, feedback, body): pass
    async def resolve(self, *, repo, feedback): pass
    async def close_pr(self, *, repo, number): pass
from forge.persistence.models import Project
from forge.persistence.repositories.direct import PostgresDirectRepository
from forge.settings import Settings
from forge.worker.direct import DirectRunWorker, _spawn_argv

FAKE_CLI = '''
import sys, time
from pathlib import Path
mode = sys.argv[1]
wt = Path.cwd()
prompt_text = Path(sys.argv[2]).read_text() if len(sys.argv) > 2 else ""
if "REVIEW" in prompt_text:
    print('```json')
    print('{"verdict":"clean","summary":"ok","findings":[]}')
    print('```', flush=True)
    sys.exit(0)
if "PLAN: You are planning" in prompt_text:
    if mode == "plan-edit":
        (wt / "planned.txt").write_text("planner edit\\n")
    print('```json')
    print('{"status":"done","summary":"PLAN-TEXT: edit changed.txt then run tests","question":null,"options":[]}')
    print('```', flush=True)
    sys.exit(0)
if mode == "sleep":
    print("starting", flush=True)
    time.sleep(120)
elif mode == "exit3":
    print("boom", flush=True)
    sys.exit(3)
elif mode == "decision":
    (wt / "draft.txt").write_text("draft")
    print('```json')
    print('{"status":"needs_decision","summary":"need input","question":"Which way?","options":["a","b"]}')
    print('```', flush=True)
elif mode == "empty":
    print('```json')
    print('{"status":"done","summary":"nothing to do","question":null,"options":[]}')
    print('```', flush=True)
else:
    (wt / "changed.txt").write_text("changed\\n")
    print("password=direct-fake-secret-123 working", flush=True)
    print('```json')
    print('{"status":"done","summary":"did the thing","question":null,"options":[]}')
    print('```', flush=True)
'''


def test_spawn_argv_wraps_windows_batch_shims_only_on_windows():
    argv = (r"C:\Programs\muse\muse.cmd", "exec", "--json")
    if sys.platform == "win32":
        assert _spawn_argv(argv) == ("cmd.exe", "/c", *argv)
        assert _spawn_argv(("tool.exe", "x")) == ("tool.exe", "x")
    else:
        assert _spawn_argv(argv) == argv


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI)
    return script


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "canonical"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


async def _seed_project(session_factory, repo: Path):
    pid = uuid4()
    async with session_factory() as session:
        session.add(
            Project(
                id=pid,
                name="P",
                canonical_path=str(repo),
                canonical_path_key=str(repo).lower(),
                github_repository=f"o/r-{pid}",
                default_branch="main",
            )
        )
        await session.commit()
    return pid


def _worker(session_factory, settings, fake_cli: Path, mode: str) -> DirectRunWorker:
    def factory(installation, ctx):
        prompt_file = fake_cli.parent / f"prompt-{ctx.session_id}.txt"
        prompt_file.write_text(ctx.prompt, encoding="utf-8")
        return DirectLaunch(
            argv=(sys.executable, str(fake_cli), mode, str(prompt_file)), stdin_data=b""
        )

    return DirectRunWorker(
        session_factory,
        settings,
        argv_factory=factory,
        installation_resolver=lambda run: SimpleNamespace(
            client=run.client, executable=sys.executable, model=run.model, effort=run.effort
        ),
        review_resolver=lambda run, impl: SimpleNamespace(
            client="codex_app_server", executable=sys.executable, model="review-model", effort=None
        ),
        publisher=_QuietPublisher(),
    )


def _service(session_factory, settings) -> DirectRunService:
    return DirectRunService(
        session_factory,
        settings,
        installations=(
            SimpleNamespace(client="muse_code", model="model-1", effort="high"),
        ),
    )


async def _setup(session_factory, tmp_path, *, mode_settings=None):
    repo = _repo(tmp_path)
    pid = await _seed_project(session_factory, repo)
    settings = Settings(
        data_root=tmp_path / "data",
        subscription_installations_path=None,
        direct_pr_poll_seconds=0,
        direct_pr_quiet_seconds=0,
        **(mode_settings or {}),
    )
    service = _service(session_factory, settings)
    return repo, pid, settings, service


@pytest.mark.integration
async def test_happy_path_commits_and_done(session_factory, tmp_path, fake_cli):
    repo, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid, task="do it", client="muse_code", model="model-1", effort=None
    )
    worker = _worker(session_factory, settings, fake_cli, "happy")
    assert await worker.run_once() == run.id
    done = await service.get(run.id)
    assert done.state == "DONE"
    # Slice B appends an honest no-checks note when the project has no policy.
    assert done.summary.startswith("did the thing")
    assert done.head_sha and done.head_sha != done.base_sha
    wt = Path(done.worktree_path)
    assert (wt / "changed.txt").exists()
    assert _git(wt, "log", "--oneline", "-1")
    assert _git(wt, "status", "--porcelain") == ""
    sessions = await service.sessions(run.id)
    implement = next(s for s in sessions if s.kind == "implement")
    transcript = await service.transcript(run.id, implement.id)
    assert "direct-fake-secret-123" not in transcript
    assert "[REDACTED]" in transcript
    # Canonical checkout untouched.
    assert not (repo / "changed.txt").exists()


@pytest.mark.integration
async def test_empty_diff_done_at_base(session_factory, tmp_path, fake_cli):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid, task="noop", client="muse_code", model="model-1", effort=None
    )
    await _worker(session_factory, settings, fake_cli, "empty").run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    assert done.head_sha == done.base_sha


@pytest.mark.integration
async def test_needs_decision_no_commit(session_factory, tmp_path, fake_cli):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid, task="decide", client="muse_code", model="model-1", effort=None
    )
    await _worker(session_factory, settings, fake_cli, "decision").run_once()
    done = await service.get(run.id)
    assert done.state == "NEEDS_DECISION"
    assert done.question == "Which way?"
    assert done.options == ("a", "b")
    assert done.head_sha is None
    wt = Path(done.worktree_path)
    assert _git(wt, "status", "--porcelain") != ""  # left uncommitted


@pytest.mark.integration
async def test_nonzero_exit_failed(session_factory, tmp_path, fake_cli):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid, task="fail", client="muse_code", model="model-1", effort=None
    )
    await _worker(session_factory, settings, fake_cli, "exit3").run_once()
    done = await service.get(run.id)
    assert done.state == "FAILED"
    assert done.failure_reason


@pytest.mark.integration
async def test_silence_watchdog_kills(session_factory, tmp_path, fake_cli):
    _, pid, settings, service = await _setup(
        session_factory,
        tmp_path,
        mode_settings={"direct_silence_timeout_seconds": 1, "direct_session_timeout_seconds": 60},
    )
    run = await service.create(
        project_id=pid, task="sleep", client="muse_code", model="model-1", effort=None
    )
    await _worker(session_factory, settings, fake_cli, "sleep").run_once()
    done = await service.get(run.id)
    assert done.state == "FAILED"
    sessions = await service.sessions(run.id)
    assert sessions[0].state == "TIMED_OUT"


@pytest.mark.integration
async def test_cancel_while_running(session_factory, tmp_path, fake_cli):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid, task="sleep", client="muse_code", model="model-1", effort=None
    )
    worker = _worker(session_factory, settings, fake_cli, "sleep")
    task = asyncio.create_task(worker.run_once())
    # Wait until the session is running.
    for _ in range(100):
        sessions = await service.sessions(run.id)
        if sessions and sessions[0].pid:
            break
        await asyncio.sleep(0.1)
    await service.cancel(run.id)
    assert await asyncio.wait_for(task, timeout=30) == run.id
    done = await service.get(run.id)
    assert done.state == "CANCELLED"
    sessions = await service.sessions(run.id)
    assert sessions[0].state == "KILLED"


@pytest.mark.integration
async def test_cancel_queued_spawns_nothing(session_factory, tmp_path, fake_cli):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid, task="x", client="muse_code", model="model-1", effort=None
    )
    cancelled = await service.cancel(run.id)
    assert cancelled.state == "CANCELLED"
    assert await _worker(session_factory, settings, fake_cli, "happy").run_once() is None
    assert await service.sessions(run.id) == ()


@pytest.mark.integration
async def test_unknown_route_rejected(session_factory, tmp_path):
    _, pid, _settings, service = await _setup(session_factory, tmp_path)
    with pytest.raises(ValueError):
        await service.create(
            project_id=pid, task="x", client="muse_code", model="nope", effort=None
        )


@pytest.mark.integration
async def test_orphaned_running_settled_failed(session_factory, tmp_path, fake_cli):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    repo = PostgresDirectRepository(session_factory)
    run = await service.create(
        project_id=pid, task="x", client="muse_code", model="model-1", effort=None
    )
    claimed = await repo.claim_next()
    assert claimed is not None
    # Second claim must not double-claim.
    assert await repo.claim_next() is None
    session = await repo.create_session(run_id=run.id, run=claimed)
    await repo.update_session(session.id, pid=4194304)
    assert await _worker(session_factory, settings, fake_cli, "happy").run_once() is None
    done = await service.get(run.id)
    assert done.state == "FAILED"
    assert "interrupt" in (done.failure_reason or "")


@pytest.mark.integration
async def test_routes_hide_executable(session_factory, tmp_path):
    _, _, _settings, service = await _setup(session_factory, tmp_path)
    routes = service.routes()
    assert routes == ({"client": "muse_code", "model": "model-1", "effort": "high"},)


@pytest.mark.integration
async def test_planned_run_auto_continues_with_plan(session_factory, tmp_path, fake_cli):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid, task="planned work", client="muse_code", model="model-1",
        effort=None, shape="planned",
    )
    assert run.shape == "planned"
    await _worker(session_factory, settings, fake_cli, "happy").run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    assert done.plan_text and "PLAN-TEXT" in done.plan_text
    sessions = await service.sessions(run.id)
    kinds = [s.kind for s in sessions]
    assert kinds.index("plan") < kinds.index("implement")
    implement = next(s for s in sessions if s.kind == "implement")
    prompt = (fake_cli.parent / f"prompt-{implement.id}.txt").read_text(encoding="utf-8")
    assert "PLAN-TEXT" in prompt


@pytest.mark.integration
async def test_planned_run_waits_for_plan_approval(session_factory, tmp_path, fake_cli):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid, task="gate me", client="muse_code", model="model-1",
        effort=None, shape="planned", review_plan_first=True,
    )
    worker = _worker(session_factory, settings, fake_cli, "happy")
    await worker.run_once()
    parked = await service.get(run.id)
    assert parked.state == "AWAITING_PLAN_APPROVAL"
    assert parked.plan_text and "PLAN-TEXT" in parked.plan_text
    assert parked.plan_approved is False
    # No implementation happened while parked, and no worker claims it.
    assert [s.kind for s in await service.sessions(run.id)] == ["plan"]
    assert await worker.run_once() is None
    approved = await service.approve_plan(run.id)
    assert approved.state == "QUEUED"
    assert approved.plan_approved is True
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    kinds = [s.kind for s in await service.sessions(run.id)]
    assert kinds.count("plan") == 1
    assert "implement" in kinds


@pytest.mark.integration
async def test_plan_session_edits_are_reverted(session_factory, tmp_path, fake_cli):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid, task="edit while planning", client="muse_code",
        model="model-1", effort=None, shape="planned",
    )
    await _worker(session_factory, settings, fake_cli, "plan-edit").run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    wt = Path(done.worktree_path)
    assert not (wt / "planned.txt").exists()
    assert (wt / "changed.txt").exists()


@pytest.mark.integration
async def test_cancel_awaiting_plan_approval(session_factory, tmp_path, fake_cli):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid, task="gate me", client="muse_code", model="model-1",
        effort=None, shape="planned", review_plan_first=True,
    )
    await _worker(session_factory, settings, fake_cli, "happy").run_once()
    assert (await service.get(run.id)).state == "AWAITING_PLAN_APPROVAL"
    cancelled = await service.cancel(run.id)
    assert cancelled.state == "CANCELLED"
    with pytest.raises(ValueError):
        await service.approve_plan(run.id)


@pytest.mark.integration
async def test_invalid_shape_rejected(session_factory, tmp_path):
    _, pid, _settings, service = await _setup(session_factory, tmp_path)
    with pytest.raises(ValueError):
        await service.create(
            project_id=pid, task="x", client="muse_code", model="model-1",
            effort=None, shape="bogus",
        )


@pytest.mark.integration
async def test_diff_bounded(session_factory, tmp_path, fake_cli):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid, task="do", client="muse_code", model="model-1", effort=None
    )
    await _worker(session_factory, settings, fake_cli, "happy").run_once()
    diff = await service.diff(run.id)
    assert "changed.txt" in diff
    assert len(diff.encode()) <= 65536 + 64


@pytest.mark.parametrize("failure", ["get", "tasks", "update_task", "cleanup_cancel", "execute_cancel"])
async def test_run_once_releases_active_ownership_when_cleanup_fails(failure):
    from unittest.mock import AsyncMock

    run = SimpleNamespace(id=uuid4(), shape="epic", state="FAILED")
    repo = SimpleNamespace(
        running_runs=AsyncMock(return_value=()), claim_next=AsyncMock(return_value=run),
        get=AsyncMock(return_value=run), tasks=AsyncMock(return_value=(SimpleNamespace(id=uuid4(), state="RUNNING"),)),
        update_task=AsyncMock(), update_run=AsyncMock(),
    )
    if failure in {"get", "tasks", "update_task"}:
        getattr(repo, failure).side_effect = RuntimeError("cleanup unavailable")
    elif failure == "cleanup_cancel":
        repo.get.side_effect = asyncio.CancelledError()
    worker = DirectRunWorker(None, Settings())
    worker._repo = repo
    worker._execute = AsyncMock(side_effect=asyncio.CancelledError() if failure == "execute_cancel" else None)
    caught = None
    try:
        await worker.run_once()
    except (RuntimeError, asyncio.CancelledError) as error:
        caught = error
    assert worker._active_run_id is None
    if failure.endswith("cancel"):
        assert isinstance(caught, asyncio.CancelledError)
    else:
        assert caught is None


async def test_cleanup_failure_does_not_skip_next_orphan_sweep():
    from unittest.mock import AsyncMock

    run = SimpleNamespace(id=uuid4(), shape="epic", state="RUNNING")
    worker = DirectRunWorker(None, Settings())
    worker._repo = SimpleNamespace(
        running_runs=AsyncMock(side_effect=[(), (run,)]), claim_next=AsyncMock(return_value=run),
        get=AsyncMock(side_effect=RuntimeError("cleanup unavailable")), sessions=AsyncMock(return_value=()),
        tasks=AsyncMock(return_value=()), update_run=AsyncMock(), settle_orphan_run=AsyncMock(return_value=None),
    )
    worker._execute = AsyncMock()
    try:
        await worker.run_once()
    except RuntimeError:
        pass
    await worker._settle_orphans()
    worker._repo.sessions.assert_awaited_once_with(run.id)
    worker._repo.settle_orphan_run.assert_awaited_once_with(run.id, stopped_sessions={})


async def test_orphan_sweep_leaves_unstopped_run_retryable_while_other_runs_progress(monkeypatch):
    from unittest.mock import AsyncMock, call

    from forge.worker import direct

    active, blocked, other = (SimpleNamespace(id=uuid4()) for _ in range(3))
    running = SimpleNamespace(id=uuid4(), pid=11111, state="RUNNING")
    done = SimpleNamespace(id=uuid4(), pid=22222, state="DONE")
    unstarted = SimpleNamespace(id=uuid4(), pid=None, state="RUNNING")
    worker = DirectRunWorker(None, Settings())
    worker._active_run_id = active.id
    worker._repo = SimpleNamespace(
        running_runs=AsyncMock(side_effect=[(active, blocked, other), (blocked,)]),
        sessions=AsyncMock(side_effect=[(running, done), (unstarted,), (running, done)]),
        settle_orphan_run=AsyncMock(return_value=None),
    )
    stop = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(direct, "_kill_pid_tree", stop)

    await worker._settle_orphans()
    await worker._settle_orphans()

    assert stop.await_args_list == [call(running.pid), call(running.pid)]
    assert worker._repo.settle_orphan_run.await_args_list == [
        call(blocked.id, stopped_sessions={}),
        call(other.id, stopped_sessions={unstarted.id: None}),
        call(blocked.id, stopped_sessions={running.id: running.pid}),
    ]


async def test_child_cleanup_attempts_remaining_tasks_after_one_write_fails():
    from unittest.mock import AsyncMock, call

    run = SimpleNamespace(id=uuid4(), shape="epic", state="FAILED")
    tasks = [SimpleNamespace(id=uuid4(), state=state) for state in ("RUNNING", "DONE", "CHECKING")]
    worker = DirectRunWorker(None, Settings())
    worker._repo = SimpleNamespace(running_runs=AsyncMock(return_value=()), claim_next=AsyncMock(return_value=run),
                                   get=AsyncMock(return_value=run), tasks=AsyncMock(return_value=tasks),
                                   update_task=AsyncMock(side_effect=[RuntimeError("unavailable"), None]))
    worker._execute = AsyncMock()
    assert await worker.run_once() == run.id
    assert worker._active_run_id is None
    assert worker._repo.update_task.await_args_list == [call(tasks[0].id, state="FAILED"), call(tasks[2].id, state="FAILED")]


@pytest.mark.integration
async def test_orphan_unconfirmed_persists_warning_once_and_settles_on_eventual_stop(
    session_factory, tmp_path, fake_cli, monkeypatch
):
    from forge.worker import direct

    _, pid, settings, service = await _setup(session_factory, tmp_path)
    repo = PostgresDirectRepository(session_factory)
    run = await service.create(
        project_id=pid, task="stuck", client="muse_code", model="model-1", effort=None
    )
    claimed = await repo.claim_next()
    assert claimed is not None
    unconfirmed = await repo.create_session(run_id=run.id, run=claimed)
    await repo.update_session(unconfirmed.id, pid=11111)
    confirmed = await repo.create_session(run_id=run.id, run=claimed)
    await repo.update_session(confirmed.id, pid=22222)

    attempts = {11111: 0, 22222: 0}

    async def fake_kill_pid_tree(target_pid: int) -> bool:
        attempts[target_pid] = attempts.get(target_pid, 0) + 1
        if target_pid == 22222:
            return True
        return attempts[target_pid] >= 4

    monkeypatch.setattr(direct, "_kill_pid_tree", fake_kill_pid_tree)
    worker = _worker(session_factory, settings, fake_cli, "happy")

    # Sweep 1: 22222 settles FAILED, 11111 stays RUNNING with warning persisted once.
    await worker._settle_orphans()
    after_first = await service.get(run.id)
    assert after_first.state == "RUNNING"
    assert after_first.failure_reason is not None
    assert "11111" in after_first.failure_reason
    assert "retry" in after_first.failure_reason.lower()
    assert "cancel" in after_first.failure_reason.lower()
    sessions_1 = {s.id: s for s in await service.sessions(run.id)}
    assert sessions_1[unconfirmed.id].state == "RUNNING"
    assert sessions_1[unconfirmed.id].pid == 11111
    assert sessions_1[unconfirmed.id].ended_at is None
    assert sessions_1[confirmed.id].state == "FAILED"
    assert sessions_1[confirmed.id].ended_at is not None

    # Sweep 2: identical unconfirmed result does not churn control_version.
    await worker._settle_orphans()
    after_second = await service.get(run.id)
    assert after_second.state == "RUNNING"
    assert after_second.failure_reason == after_first.failure_reason
    assert after_second.control_version == after_first.control_version

    # Even with the same warning, a changed session needs a new owner snapshot.
    new_session = await repo.create_session(run_id=run.id, run=claimed)
    await worker._settle_orphans()
    after_third = await service.get(run.id)
    assert after_third.failure_reason == after_second.failure_reason
    assert after_third.control_version == after_second.control_version + 1
    assert next(s for s in await service.sessions(run.id) if s.id == new_session.id).state == "FAILED"

    await worker._settle_orphans()
    final = await service.get(run.id)
    assert final.state == "FAILED" and final.failure_reason == "worker interrupted"
    final_sessions = {s.id: s for s in await service.sessions(run.id)}
    assert final_sessions[unconfirmed.id].state == "FAILED"
    assert final_sessions[unconfirmed.id].ended_at is not None


@pytest.mark.integration
async def test_orphan_unconfirmed_owner_cancel_retains_session_and_epic_work(
    session_factory, tmp_path, fake_cli, monkeypatch
):
    from forge.worker import direct

    _, pid, settings, service = await _setup(session_factory, tmp_path)
    repo = PostgresDirectRepository(session_factory)
    run = await service.create(
        project_id=pid, task="epic", client="muse_code", model="model-1", effort=None, shape="epic"
    )
    claimed = await repo.claim_next()
    assert claimed is not None
    done_task = await repo.create_task(run_id=run.id, ordinal=0, task_text="done part")
    await repo.update_task(done_task.id, state="DONE")
    running_task = await repo.create_task(run_id=run.id, ordinal=1, task_text="active part")
    await repo.update_task(running_task.id, state="RUNNING")
    pending_task = await repo.create_task(run_id=run.id, ordinal=2, task_text="later part")

    done_session = await repo.create_session(run_id=run.id, run=claimed, task_id=done_task.id)
    await repo.update_session(done_session.id, state="SUCCEEDED", pid=44444)
    unresolved = await repo.create_session(run_id=run.id, run=claimed, task_id=running_task.id)
    await repo.update_session(unresolved.id, pid=33333)

    seen_pids: list[int] = []

    async def fake_kill_pid_tree(target_pid: int) -> bool:
        seen_pids.append(target_pid)
        return False

    monkeypatch.setattr(direct, "_kill_pid_tree", fake_kill_pid_tree)
    worker = _worker(session_factory, settings, fake_cli, "happy")

    await worker._settle_orphans()
    pending_run = await service.get(run.id)
    assert pending_run.state == "RUNNING"
    expected_warning = pending_run.failure_reason
    assert expected_warning is not None

    actor_id = uuid4()
    cancel_requested = await service.cancel(run.id, actor_id=actor_id)
    assert cancel_requested.cancel_requested is True
    assert cancel_requested.owner_actions[-1]["action"] == "cancel"
    assert cancel_requested.owner_actions[-1]["actor_id"] == str(actor_id)
    assert cancel_requested.owner_actions[-1]["snapshot_version"] == pending_run.control_version
    assert expected_warning in cancel_requested.owner_actions[-1]["warnings"]

    await worker._settle_orphans()
    cancelled = await service.get(run.id)
    assert cancelled.state == "CANCELLED"
    assert cancelled.failure_reason is not None
    assert "33333" in cancelled.failure_reason
    assert "manual" in cancelled.failure_reason.lower()

    sessions = {s.id: s for s in await service.sessions(run.id)}
    assert sessions[unresolved.id].state == "RUNNING"
    assert sessions[unresolved.id].pid == 33333
    assert sessions[unresolved.id].ended_at is None
    assert sessions[done_session.id].state == "SUCCEEDED"

    tasks = {t.id: t for t in await repo.tasks(run.id)}
    assert tasks[done_task.id].state == "DONE"
    assert tasks[running_task.id].state == "CANCELLED"
    assert tasks[pending_task.id].state == "PENDING"

    # Cancelled run is never swept or resumed again.
    seen_before = len(seen_pids)
    assert await worker.run_once() is None
    assert len(seen_pids) == seen_before
    with pytest.raises(ValueError):
        await repo.update_run(run.id, state="RUNNING")


@pytest.mark.integration
@pytest.mark.parametrize("stop_confirmed", [True, False])
async def test_orphan_cancel_during_stop_respects_owner_decision(
    session_factory, tmp_path, fake_cli, monkeypatch, stop_confirmed
):
    from forge.worker import direct

    _, pid, settings, service = await _setup(session_factory, tmp_path)
    repo = PostgresDirectRepository(session_factory)
    run = await service.create(
        project_id=pid, task="race", client="muse_code", model="model-1", effort=None
    )
    claimed = await repo.claim_next()
    assert claimed is not None
    sess = await repo.create_session(run_id=run.id, run=claimed)
    await repo.update_session(sess.id, pid=55555)

    actor_id = uuid4()

    async def fake_kill_pid_tree(_pid: int) -> bool:
        await service.cancel(run.id, actor_id=actor_id)
        return stop_confirmed

    monkeypatch.setattr(direct, "_kill_pid_tree", fake_kill_pid_tree)
    worker = _worker(session_factory, settings, fake_cli, "happy")
    await worker._settle_orphans()

    final = await service.get(run.id)
    assert final.state == "CANCELLED"
    assert final.owner_actions[-1]["action"] == "cancel"
    assert final.owner_actions[-1]["actor_id"] == str(actor_id)
    sessions = await service.sessions(run.id)
    if stop_confirmed:
        assert sessions[0].state == "FAILED"
        assert sessions[0].failure_reason == "worker interrupted"
        assert sessions[0].ended_at is not None
    else:
        assert sessions[0].state == "RUNNING"
        assert sessions[0].pid == 55555
        assert sessions[0].ended_at is None
        assert final.failure_reason is not None and "55555" in final.failure_reason


@pytest.mark.integration
async def test_orphan_stop_evidence_is_bound_to_the_observed_pid(
    session_factory, tmp_path, fake_cli, monkeypatch
):
    from forge.worker import direct

    _, project_id, settings, service = await _setup(session_factory, tmp_path)
    repo = PostgresDirectRepository(session_factory)
    run = await service.create(
        project_id=project_id, task="interrupted", client="muse_code", model="model-1", effort=None
    )
    claimed = await repo.claim_next()
    session = await repo.create_session(run_id=run.id, run=claimed)
    await repo.update_session(session.id, pid=55555)

    async def stop_observed_pid(pid):
        assert pid == 55555
        await repo.update_session(session.id, pid=55556)
        return True

    monkeypatch.setattr(direct, "_kill_pid_tree", stop_observed_pid)
    await _worker(session_factory, settings, fake_cli, "happy")._settle_orphans()
    assert (await service.get(run.id)).state == "RUNNING"
    [retained] = await repo.sessions(run.id)
    assert (retained.pid, retained.state, retained.ended_at) == (55556, "RUNNING", None)
