"""Slice B: Forge-run checks + repair loop (fake CLI + fake policy)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.services.direct_runs import DirectRunService
from forge.domain.direct import DirectLaunch, DirectRunState
from forge.domain.policy import CommandSpec, ProjectPolicy, StepKind
from forge.persistence.models import Project, ProjectPolicyVersion
from forge.persistence.repositories.direct import PostgresDirectRepository
from forge.settings import Settings
from forge.worker.direct import DirectRunWorker

CHECK_PY = '''
import os, sys, time
from pathlib import Path
counter = Path(os.environ["CHECK_COUNTER"])
counter.write_text(str(int(counter.read_text() or "0") + 1) if counter.exists() else "1")
mode = os.environ.get("CHECK_MODE", "normal")
if mode == "sleep":
    print("sleeping", flush=True)
    time.sleep(120)
if mode == "secret":
    print("password=check-fake-secret-999 FAILED tests/test_smoke.py::test_it", flush=True)
    sys.exit(1)
if Path("FIXED.txt").exists():
    print("1 passed", flush=True)
    sys.exit(0)
print("FAILED tests/test_smoke.py::test_it", flush=True)
sys.exit(1)
'''

FAKE_CLI = '''
import subprocess, sys
from pathlib import Path
wt = Path.cwd()
prompt = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else ""
if "REVIEW" in prompt:
    print('```json')
    print('{"verdict":"clean","summary":"ok","findings":[]}')
    print('```', flush=True)
    sys.exit(0)
is_repair = "fix only these failures" in prompt.lower() or "repair session" in prompt.lower()
mode = sys.argv[2]
if is_repair:
    if mode == "repair_fixes":
        (wt / "FIXED.txt").write_text("fixed\\n")
    elif mode == "repair_no_change":
        pass
    elif mode == "repair_empty_commit":
        subprocess.run(["git", "-c", "user.email=f@l", "-c", "user.name=F",
                        "commit", "--allow-empty", "-m", "empty"], cwd=wt, check=True,
                       capture_output=True)
    else:
        n = len(list(wt.glob("repair-*.txt"))) + 1
        (wt / f"repair-{n}.txt").write_text("try\\n")
    print('```json')
    print('{"status":"done","summary":"repaired","question":null,"options":[]}')
    print('```', flush=True)
else:
    if mode in {"repair_fixes", "repair_no_change", "repair_empty_commit", "always_fail"}:
        (wt / "changed.txt").write_text("changed\\n")
    elif mode == "all_pass":
        (wt / "FIXED.txt").write_text("fixed\\n")
        (wt / "changed.txt").write_text("changed\\n")
    elif mode == "missing_env":
        (wt / "changed.txt").write_text("changed\\n")
    print('```json')
    print('{"status":"done","summary":"did the thing","question":null,"options":[]}')
    print('```', flush=True)
'''


def test_slice_b_states_and_settings() -> None:
    assert DirectRunState.CHECKING == "CHECKING"
    assert DirectRunState.REPAIRING == "REPAIRING"
    settings = Settings()
    assert settings.direct_max_repair_rounds == 3
    assert settings.direct_check_timeout_seconds == 1800


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "canonical"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True, check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    (repo / "check.py").write_text(CHECK_PY)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


def _policy_doc(pid, repo: Path, counter: Path, *, env_keys=(), check_mode="normal"):
    policy = ProjectPolicy(
        id=pid,
        version=1,
        repository_path=str(repo),
        github_repository=f"o/r-{pid}",
        default_branch="main",
        commands=(
            CommandSpec(
                kind=StepKind.TEST,
                name="smoke",
                argv=(sys.executable, "check.py"),
                timeout_seconds=60,
                environment_keys=tuple(env_keys),
            ),
            CommandSpec(
                kind=StepKind.INSTALL,
                name="setup",
                argv=(sys.executable, "--version"),
                timeout_seconds=60,
            ),
        ),
    )
    return policy.model_dump(mode="json")


async def _seed(session_factory, repo: Path, pid, *, with_policy=True, env_keys=()):
    async with session_factory() as session:
        project = Project(
            id=pid,
            name="P",
            canonical_path=str(repo),
            canonical_path_key=str(repo).lower(),
            github_repository=f"o/r-{pid}",
            default_branch="main",
            current_policy_version=None,
        )
        session.add(project)
        if with_policy:
            # Composite FK cycle: policy row first (needs project), then
            # point the project at it.
            await session.flush()
            session.add(
                ProjectPolicyVersion(
                    project_id=pid,
                    version=1,
                    policy_digest="a" * 64,
                    document_schema_version=1,
                    document=_policy_doc(pid, repo, repo, env_keys=env_keys),
                )
            )
            await session.flush()
            project.current_policy_version = 1
        await session.commit()


def _service(session_factory, settings) -> DirectRunService:
    return DirectRunService(
        session_factory,
        settings,
        installations=(SimpleNamespace(client="muse_code", model="model-1", effort="high"),),
    )


async def _setup(session_factory, tmp_path, *, mode, settings_extra=None, env_keys=(),
                 with_policy=True, check_mode="normal"):
    repo = _repo(tmp_path)
    pid = uuid4()
    await _seed(session_factory, repo, pid, with_policy=with_policy, env_keys=env_keys)
    counter = tmp_path / "counter.txt"
    settings = Settings(
        data_root=tmp_path / "data",
        subscription_installations_path=None,
        **(settings_extra or {}),
    )
    service = _service(session_factory, settings)
    run = await service.create(
        project_id=pid, task="do it", client="muse_code", model="model-1", effort=None
    )
    fake_cli = tmp_path / "fake_cli.py"
    fake_cli.write_text(FAKE_CLI)
    env = {
        **os.environ,
        "CHECK_COUNTER": str(counter),
        "CHECK_MODE": check_mode,
    }

    def factory(installation, ctx):
        prompt_file = tmp_path / f"prompt-{ctx.session_id}.txt"
        prompt_file.write_text(ctx.prompt, encoding="utf-8")
        return DirectLaunch(
            argv=(sys.executable, str(fake_cli), str(prompt_file), mode), stdin_data=b""
        )

    worker = DirectRunWorker(
        session_factory,
        settings,
        argv_factory=factory,
        installation_resolver=lambda run: SimpleNamespace(
            client=run.client, executable=sys.executable, model=run.model, effort=run.effort
        ),
        review_resolver=lambda run, impl: SimpleNamespace(
            client="codex_app_server", executable=sys.executable, model="review-model", effort=None
        ),
    )
    # Check subprocesses inherit the worker env; tests point the counter via env.
    old = os.environ.get("CHECK_COUNTER"), os.environ.get("CHECK_MODE")
    os.environ["CHECK_COUNTER"] = str(counter)
    os.environ["CHECK_MODE"] = check_mode
    return run, service, worker, counter, env, old


def _restore_env(old) -> None:
    for key, value in zip(("CHECK_COUNTER", "CHECK_MODE"), old, strict=True):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.mark.integration
async def test_fail_repair_pass_done(session_factory, tmp_path):
    run, service, worker, counter, _env, old = await _setup(
        session_factory, tmp_path, mode="repair_fixes"
    )
    try:
        assert await worker.run_once() == run.id
    finally:
        _restore_env(old)
    done = await service.get(run.id)
    assert done.state == "DONE"
    checks = await PostgresDirectRepository(session_factory).checks(run.id)
    assert [(c.round, c.status) for c in checks] == [(0, "FAILED"), (1, "PASSED")]
    assert "tests/test_smoke.py::test_it" in (checks[0].failure_excerpt or "")
    sessions = await service.sessions(run.id)
    assert [s.kind for s in sessions] == ["implement", "repair", "review"]
    assert counter.read_text() == "2"


@pytest.mark.integration
async def test_always_fail_exhausts_one_round(session_factory, tmp_path):
    run, service, worker, _c, _e, old = await _setup(
        session_factory, tmp_path, mode="always_fail",
        settings_extra={"direct_max_repair_rounds": 1},
    )
    try:
        await worker.run_once()
    finally:
        _restore_env(old)
    done = await service.get(run.id)
    assert done.state == "FAILED"
    assert "checks still failing after 1 repair rounds" in (done.failure_reason or "")
    assert "test_it" in (done.summary or "")
    checks = await PostgresDirectRepository(session_factory).checks(run.id)
    assert [c.status for c in checks] == ["FAILED", "FAILED"]


@pytest.mark.integration
async def test_identical_tree_served_cached(session_factory, tmp_path):
    run, service, worker, counter, _e, old = await _setup(
        session_factory, tmp_path, mode="repair_empty_commit",
        settings_extra={"direct_max_repair_rounds": 1},
    )
    try:
        await worker.run_once()
    finally:
        _restore_env(old)
    done = await service.get(run.id)
    assert done.state == "FAILED"
    checks = await PostgresDirectRepository(session_factory).checks(run.id)
    assert [c.status for c in checks] == ["FAILED", "CACHED"]
    assert counter.read_text() == "1"


@pytest.mark.integration
async def test_repair_no_changes_failed(session_factory, tmp_path):
    run, service, worker, _c, _e, old = await _setup(
        session_factory, tmp_path, mode="repair_no_change"
    )
    try:
        await worker.run_once()
    finally:
        _restore_env(old)
    done = await service.get(run.id)
    assert done.state == "FAILED"
    assert done.failure_reason == "repair made no changes"


@pytest.mark.integration
async def test_missing_env_error_no_repair(session_factory, tmp_path):
    run, service, worker, counter, _e, old = await _setup(
        session_factory, tmp_path, mode="missing_env",
        env_keys=("FORGE_DIRECT_DEFINITELY_MISSING_KEY",),
    )
    os.environ.pop("FORGE_DIRECT_DEFINITELY_MISSING_KEY", None)
    try:
        await worker.run_once()
    finally:
        _restore_env(old)
    done = await service.get(run.id)
    assert done.state == "FAILED"
    assert "missing environment: FORGE_DIRECT_DEFINITELY_MISSING_KEY" in (
        done.failure_reason or ""
    )
    checks = await PostgresDirectRepository(session_factory).checks(run.id)
    assert [c.status for c in checks] == ["ERROR"]
    assert len(await service.sessions(run.id)) == 1
    assert not counter.exists()


@pytest.mark.integration
async def test_first_pass_all_green_done(session_factory, tmp_path):
    run, service, worker, _c, _e, old = await _setup(
        session_factory, tmp_path, mode="all_pass"
    )
    try:
        await worker.run_once()
    finally:
        _restore_env(old)
    done = await service.get(run.id)
    assert done.state == "DONE"
    checks = await PostgresDirectRepository(session_factory).checks(run.id)
    assert [(c.round, c.status) for c in checks] == [(0, "PASSED")]
    assert [s.kind for s in await service.sessions(run.id)] == ["implement", "review"]


@pytest.mark.integration
async def test_no_policy_still_done_with_note(session_factory, tmp_path):
    run, service, worker, _c, _e, old = await _setup(
        session_factory, tmp_path, mode="all_pass", with_policy=False
    )
    try:
        await worker.run_once()
    finally:
        _restore_env(old)
    done = await service.get(run.id)
    assert done.state == "DONE"
    assert "no checks" in (done.summary or "").lower()


@pytest.mark.integration
async def test_check_output_redacted_in_excerpt(session_factory, tmp_path):
    run, _service, worker, _c, _e, old = await _setup(
        session_factory, tmp_path, mode="always_fail", check_mode="secret",
        settings_extra={"direct_max_repair_rounds": 0},
    )
    try:
        await worker.run_once()
    finally:
        _restore_env(old)
    checks = await PostgresDirectRepository(session_factory).checks(run.id)
    assert checks and "check-fake-secret-999" not in (checks[0].failure_excerpt or "")
    assert "[REDACTED]" in (checks[0].failure_excerpt or "")


@pytest.mark.integration
async def test_check_timeout_is_error_failed(session_factory, tmp_path):
    run, service, worker, _c, _e, old = await _setup(
        session_factory, tmp_path, mode="all_pass", check_mode="sleep",
        settings_extra={"direct_check_timeout_seconds": 1},
    )
    started = time.monotonic()
    try:
        await worker.run_once()
    finally:
        _restore_env(old)
    assert time.monotonic() - started < 60
    done = await service.get(run.id)
    assert done.state == "FAILED"
    checks = await PostgresDirectRepository(session_factory).checks(run.id)
    assert [c.status for c in checks] == ["ERROR"]


@pytest.mark.integration
async def test_cancel_during_checking_settles_cancelled(session_factory, tmp_path):
    run, service, worker, counter, _e, old = await _setup(
        session_factory, tmp_path, mode="all_pass", check_mode="sleep",
        settings_extra={"direct_check_timeout_seconds": 1800},
    )
    try:
        task = asyncio.create_task(worker.run_once())
        for _ in range(200):
            if counter.exists():
                break
            await asyncio.sleep(0.05)
        await service.cancel(run.id)
        assert await asyncio.wait_for(task, timeout=60) == run.id
    finally:
        _restore_env(old)
    done = await service.get(run.id)
    assert done.state == "CANCELLED"
