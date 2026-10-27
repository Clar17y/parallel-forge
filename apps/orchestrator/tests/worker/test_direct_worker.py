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
            effort=None, shape="epic",
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
