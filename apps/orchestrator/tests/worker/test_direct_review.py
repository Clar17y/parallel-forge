"""Slice C: cross-provider review loop (fake two-client)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.services.direct_runs import DirectRunService
from forge.domain.direct import DirectLaunch, DirectRunState
from forge.persistence.models import Project
from forge.persistence.repositories.direct import PostgresDirectRepository
from forge.settings import Settings
from forge.worker.direct import DirectRunWorker

FAKE_CLI = '''
import sys
from pathlib import Path
wt = Path.cwd()
prompt = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else ""
mode = sys.argv[2]
if "REVIEW" in prompt:
    counter = Path(sys.argv[1]).parent / "review-counter.txt"
    n = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(n))
    if mode == "dirty":
        (wt / "changed.txt").write_text("tampered\\n")
        (wt / "reviewer-junk.txt").write_text("junk\\n")
        print('```json')
        print('{"verdict":"clean","summary":"looks fine","findings":[]}')
        print('```', flush=True)
    elif mode == "always_blocking":
        print('```json')
        print('{"verdict":"blocking","summary":"bad","findings":[{"severity":"blocking","file":"changed.txt","detail":"still broken"}]}')
        print('```', flush=True)
    elif mode == "blocking_then_clean":
        if n == 1:
            print('```json')
            print('{"verdict":"blocking","summary":"bad","findings":[{"severity":"blocking","file":"changed.txt","detail":"fix this bug"},{"severity":"note","file":null,"detail":"a note"}]}')
            print('```', flush=True)
        else:
            print('```json')
            print('{"verdict":"clean","summary":"ok now","findings":[]}')
            print('```', flush=True)
    elif mode == "unparseable":
        print("I reviewed it and it seems fine, no json here", flush=True)
    elif mode == "notes_only":
        print('```json')
        print('{"verdict":"clean","summary":"ok","findings":[{"severity":"note","file":"changed.txt","detail":"consider renaming"}]}')
        print('```', flush=True)
    else:
        print('```json')
        print('{"verdict":"clean","summary":"looks fine","findings":[]}')
        print('```', flush=True)
elif "repair session" in prompt.lower():
    files = list(wt.glob("review-repair-*.txt"))
    (wt / f"review-repair-{len(files)+1}.txt").write_text("fixed\\n")
    print('```json')
    print('{"status":"done","summary":"repaired review","question":null,"options":[]}')
    print('```', flush=True)
else:
    (wt / "changed.txt").write_text("changed\\n")
    print('```json')
    print('{"status":"done","summary":"did the thing","question":null,"options":[]}')
    print('```', flush=True)
'''


def test_slice_c_state_and_setting() -> None:
    assert DirectRunState.REVIEWING == "REVIEWING"
    assert Settings().direct_max_review_rounds == 3


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


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


async def _setup(session_factory, tmp_path, *, mode, review_resolver_mode="distinct", settings_extra=None):
    repo = _repo(tmp_path)
    pid = uuid4()
    async with session_factory() as session:
        session.add(Project(id=pid, name="P", canonical_path=str(repo), canonical_path_key=str(repo).lower(), github_repository=f"o/r-{pid}", default_branch="main"))
        await session.commit()
    settings = Settings(data_root=tmp_path / "data", subscription_installations_path=None, **(settings_extra or {}))
    service = DirectRunService(session_factory, settings, installations=(SimpleNamespace(client="muse_code", model="model-1", effort="high"), SimpleNamespace(client="codex_app_server", model="review-model", effort=None)))
    run = await service.create(project_id=pid, task="do it", client="muse_code", model="model-1", effort=None)
    fake_cli = tmp_path / "fake_cli.py"
    fake_cli.write_text(FAKE_CLI)

    def factory(installation, ctx):
        prompt_file = tmp_path / f"prompt-{ctx.session_id}.txt"
        prompt_file.write_text(ctx.prompt, encoding="utf-8")
        return DirectLaunch(argv=(sys.executable, str(fake_cli), str(prompt_file), mode), stdin_data=b"")

    if review_resolver_mode == "none":
        resolver = lambda run, impl: None
    else:
        resolver = lambda run, impl: SimpleNamespace(client="codex_app_server", executable=sys.executable, model="review-model", effort=None)
    worker = DirectRunWorker(session_factory, settings, argv_factory=factory, installation_resolver=lambda run: SimpleNamespace(client=run.client, executable=sys.executable, model=run.model, effort=run.effort), review_resolver=resolver)
    return run, service, worker


@pytest.mark.integration
async def test_clean_review_done_distinct(session_factory, tmp_path):
    run, service, worker = await _setup(session_factory, tmp_path, mode="clean")
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    reviews = await PostgresDirectRepository(session_factory).reviews(run.id)
    assert len(reviews) == 1
    assert reviews[0].verdict == "clean"
    assert reviews[0].client == "codex_app_server"
    assert reviews[0].client != done.client
    sessions = await service.sessions(run.id)
    assert [s.kind for s in sessions] == ["implement", "review"]


@pytest.mark.integration
async def test_blocking_repair_checks_clean_done(session_factory, tmp_path):
    run, service, worker = await _setup(session_factory, tmp_path, mode="blocking_then_clean")
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    reviews = await PostgresDirectRepository(session_factory).reviews(run.id)
    assert [r.verdict for r in reviews] == ["blocking", "clean"]
    sessions = await service.sessions(run.id)
    assert [s.kind for s in sessions] == ["implement", "review", "repair", "review"]


@pytest.mark.integration
async def test_always_blocking_rounds1_failed(session_factory, tmp_path):
    run, service, worker = await _setup(session_factory, tmp_path, mode="always_blocking", settings_extra={"direct_max_review_rounds": 1})
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "FAILED"
    assert "review still blocking after 1 rounds" in (done.failure_reason or "")
    reviews = await PostgresDirectRepository(session_factory).reviews(run.id)
    assert len(reviews) == 1 and reviews[0].findings[0]["detail"] == "still broken"


@pytest.mark.integration
async def test_reviewer_dirty_reverted(session_factory, tmp_path):
    run, service, worker = await _setup(session_factory, tmp_path, mode="dirty")
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    wt = Path(done.worktree_path)
    assert (wt / "changed.txt").read_text() == "changed\n"
    assert not (wt / "reviewer-junk.txt").exists()
    reviews = await PostgresDirectRepository(session_factory).reviews(run.id)
    assert reviews[0].reviewer_modified_worktree is True
    head_after = _git(wt, "rev-parse", "HEAD")
    assert head_after == done.head_sha
    assert _git(wt, "status", "--porcelain") == ""


@pytest.mark.integration
async def test_unparseable_review_failed(session_factory, tmp_path):
    run, service, worker = await _setup(session_factory, tmp_path, mode="unparseable")
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "FAILED"
    assert done.failure_reason == "review result unparseable"


@pytest.mark.integration
async def test_single_route_no_review_session(session_factory, tmp_path):
    run, service, worker = await _setup(session_factory, tmp_path, mode="clean", review_resolver_mode="none")
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "FAILED"
    assert done.failure_reason == "no distinct reviewer route"
    sessions = await service.sessions(run.id)
    assert [s.kind for s in sessions] == ["implement"]


@pytest.mark.integration
async def test_notes_only_done_no_repair(session_factory, tmp_path):
    run, service, worker = await _setup(session_factory, tmp_path, mode="notes_only")
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    sessions = await service.sessions(run.id)
    assert [s.kind for s in sessions] == ["implement", "review"]
    reviews = await PostgresDirectRepository(session_factory).reviews(run.id)
    assert reviews[0].findings[0]["severity"] == "note"
