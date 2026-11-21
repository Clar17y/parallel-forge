"""DirectRunWorker integration tests for the direct epic shape."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.services.direct_runs import DirectRunService
from forge.domain.direct import DirectLaunch, DirectPrSnapshot
from forge.domain.policy import CommandSpec, ProjectPolicy, StepKind
from forge.persistence.models import Project, ProjectPolicyVersion
from forge.persistence.repositories.direct import PostgresDirectRepository
from forge.settings import Settings
from forge.worker.direct import DirectRunWorker


class _QuietPublisher:
    def __init__(self):
        self.pushed_branches = []
        self.opened_prs = []

    async def push(self, worktree, branch):
        self.pushed_branches.append(branch)

    async def open_pr(self, *, repo, base, head, title, body, worktree):
        assert len(body) <= 65536
        self.opened_prs.append({"repo": repo, "head": head, "title": title, "body": body})
        return 1, "https://example.test/pr/1"

    async def poll(self, *, repo, number):
        from datetime import UTC, datetime
        past = datetime(2000, 1, 1, tzinfo=UTC)
        return DirectPrSnapshot(has_activity=True, last_activity_at=past, created_at=past)

    async def reply(self, *, repo, number, feedback, body): pass
    async def resolve(self, *, repo, feedback): pass
    async def close_pr(self, *, repo, number): pass


EPIC_FAKE_CLI = '''
import json, subprocess, sys, time
from pathlib import Path

mode = sys.argv[1]
wt = Path.cwd()
prompt_text = Path(sys.argv[2]).read_text(encoding="utf-8") if len(sys.argv) > 2 else ""

if "DECOMPOSE: You are decomposing" in prompt_text:
    if mode == "decompose-fail":
        print("not json at all")
        sys.exit(0)
    elif mode == "decompose-blocked":
        print(json.dumps({"status":"blocked","summary":"cannot decompose","tasks":[]}))
        sys.exit(0)
    elif mode == "decompose-nonzero":
        print(json.dumps({"status":"done","summary":"invalid success","tasks":[{"task":"no launch","shape":"quick"}]}))
        sys.exit(2)
    elif mode == "decompose-empty":
        print('```json\\n{"status":"done","summary":"none","tasks":[]}\\n```')
        sys.exit(0)
    elif mode == "decompose-oversized":
        huge_tasks = [{"task": "x" * 1000, "shape": "quick"} for _ in range(21)]
        print('```json\\n' + json.dumps({"status": "done", "summary": "big", "tasks": huge_tasks}) + '\\n```')
        sys.exit(0)
    elif mode in {"decompose-edit", "decompose-commit"}:
        (wt / "dirty_decompose.txt").write_text("illegal edit\\n")
        if mode == "decompose-commit":
            subprocess.run(["git", "add", "dirty_decompose.txt"], check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "illegal decomposition commit"], check=True, capture_output=True)
        tasks = [
            {"task": "child one", "shape": "quick"},
            {"task": "child two", "shape": "planned"}
        ]
        print('```json\\n' + json.dumps({"status": "done", "summary": "decomposed", "tasks": tasks}) + '\\n```')
        sys.exit(0)
    elif mode == "decompose-decision":
        print('```json\\n{"status":"needs_decision","summary":"need path choice","question":"Which approach?","options":["monolith","micro"],"tasks":[]}\\n```')
        sys.exit(0)
    elif mode == "decompose-3":
        tasks = [
            {"task": "first step", "shape": "quick"},
            {"task": "second step", "shape": "quick"},
            {"task": "third step", "shape": "quick"}
        ]
        print('```json\\n' + json.dumps({"status": "done", "summary": "three steps", "tasks": tasks}) + '\\n```')
        sys.exit(0)
    else:
        tasks = [
            {"task": "child one", "shape": "quick"},
            {"task": "child two", "shape": "planned"}
        ]
        if mode in {"check-repair", "review-repair", "cancel-check"}:
            tasks.append({"task": "child three", "shape": "quick"})
        print('```json\\n' + json.dumps({"status": "done", "summary": "decomposed", "tasks": tasks}) + '\\n```')
        sys.exit(0)

if "PLAN: You are planning" in prompt_text:
    if mode == "plan-edit":
        (wt / "dirty_plan.txt").write_text("plan edit\\n")
    print('```json\\n{"status":"done","summary":"Plan for child: do the thing"}\\n```')
    sys.exit(0)

if "REVIEW:" in prompt_text:
    if mode == "review-repair" and "child two" in prompt_text and not (wt / "review-fixed").exists():
        print(json.dumps({"verdict":"blocking","summary":"fix second child","findings":[{"severity":"blocking","file":"work.txt","detail":"repair second child"}]}))
        sys.exit(0)
    print('```json\\n{"verdict":"clean","summary":"ok","findings":[]}\\n```')
    sys.exit(0)

# Repair sessions must commit a new candidate without replaying the child.
if "repair session" in prompt_text.lower() or "fix only these failures" in prompt_text.lower():
    (wt / "review-fixed").write_text("fixed")
    (wt / "check-fixed").write_text("fixed")
    print(json.dumps({"status":"done","summary":"repaired","question":None,"options":[]}))
    sys.exit(0)

# Implement phase
if "\\nTask:\\nchild two\\n" in prompt_text or "\\nTask:\\nsecond step\\n" in prompt_text:
    if mode == "fail-child-2":
        sys.exit(2)
    elif mode == "decision-child-2":
        print('```json\\n{"status":"needs_decision","summary":"child 2 needs input","question":"Proceed?","options":["yes","no"]}\\n```')
        sys.exit(0)
    elif mode == "sleep-child-2":
        (wt / "partial.txt").write_text("retained partial edit")
        print("sleeping", flush=True)
        time.sleep(120)
    elif mode == "noop-child-2":
        print('```json\\n{"status":"done","summary":"no changes made in child 2"}\\n```')
        sys.exit(0)

# Default implementation success
log_file = wt / "progress.log"
with log_file.open("a", encoding="utf-8") as f:
    if "\\nTask:\\nchild one\\n" in prompt_text or "\\nTask:\\nfirst step\\n" in prompt_text:
        f.write("step 1\\n")
    elif "\\nTask:\\nchild two\\n" in prompt_text or "\\nTask:\\nsecond step\\n" in prompt_text:
        f.write("step 2\\n")
    else:
        f.write("step other\\n")

(wt / "work.txt").write_text(f"done mode={mode}\\n")
print('```json\\n{"status":"done","summary":"implemented task successfully"}\\n```')
'''


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake_cli.py"
    script.write_text(EPIC_FAKE_CLI, encoding="utf-8")
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


def _worker(
    session_factory,
    settings,
    fake_cli: Path,
    mode: str,
    publisher: _QuietPublisher | None = None,
) -> DirectRunWorker:
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
        publisher=publisher if publisher is not None else _QuietPublisher(),
    )


def _service(session_factory, settings) -> DirectRunService:
    return DirectRunService(
        session_factory,
        settings,
        installations=(
            SimpleNamespace(client="muse_code", model="model-1", effort="high"),
        ),
    )


async def _setup(session_factory, tmp_path: Path):
    repo = _repo(tmp_path)
    pid = await _seed_project(session_factory, repo)
    settings = Settings(
        data_root=str(tmp_path / "data"),
        subscription_installations_path=str(tmp_path / "manifest.json"),
        direct_max_sessions_per_run=10,
        direct_review_policy="skip",
        direct_pr_poll_seconds=0,
        direct_pr_quiet_seconds=0,
    )
    repo_obj = PostgresDirectRepository(session_factory)
    service = _service(session_factory, settings)
    return repo_obj, pid, settings, service


@pytest.mark.integration
async def test_epic_sequential_execution_two_children(session_factory, tmp_path, fake_cli):
    _repo_obj, pid, settings, service = await _setup(session_factory, tmp_path)
    publisher = _QuietPublisher()
    run = await service.create(
        project_id=pid,
        task="build the big feature",
        client="muse_code",
        model="model-1",
        effort=None,
        shape="epic",
    )
    worker = _worker(session_factory, settings, fake_cli, "default", publisher=publisher)
    assert await worker.run_once() == run.id

    done = await service.get(run.id)
    assert done.state == "DONE"
    tasks = await service.tasks(run.id)
    assert len(tasks) == 2
    assert tasks[0].ordinal == 0
    assert tasks[0].task_text == "child one"
    assert tasks[0].shape == "quick"
    assert tasks[0].state == "DONE"
    assert tasks[0].head_sha is not None

    assert tasks[1].ordinal == 1
    assert tasks[1].task_text == "child two"
    assert tasks[1].shape == "planned"
    assert tasks[1].state == "DONE"
    assert tasks[1].plan_text is not None
    assert "Plan for child" in tasks[1].plan_text
    assert tasks[1].head_sha is not None
    assert tasks[1].head_sha != tasks[0].head_sha
    assert Path(done.worktree_path, "progress.log").read_text().splitlines() == ["step 1", "step 2"]
    assert _git(Path(done.worktree_path), "merge-base", tasks[0].head_sha, tasks[1].head_sha) == tasks[0].head_sha

    # Check that Child 2's prompt included Child 1's summary
    prompts = list(fake_cli.parent.glob("prompt-*.txt"))
    child2_implement_prompts = [
        p.read_text(encoding="utf-8") for p in prompts
        if "child two" in p.read_text(encoding="utf-8") and "PLAN:" not in p.read_text(encoding="utf-8")
    ]
    assert len(child2_implement_prompts) == 1
    assert "Previous completed tasks in this epic:" in child2_implement_prompts[0]
    assert "Task 1 (child one)" in child2_implement_prompts[0]

    # Verify single combined PR was published
    assert len(publisher.opened_prs) == 1
    assert "Completed tasks:" in publisher.opened_prs[0]["body"]
    assert "child one" in publisher.opened_prs[0]["body"]
    assert "child two" in publisher.opened_prs[0]["body"]


@pytest.mark.integration
@pytest.mark.parametrize("task", ["a" * 65536, "a" * 60000 + "🧭" * 1384], ids=["ascii", "unicode"])
async def test_epic_long_accepted_request_publishes_bounded_echo(session_factory, tmp_path, fake_cli, task):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(project_id=pid, task=task, client="muse_code", model="model-1", effort=None, shape="epic")
    publisher = _QuietPublisher()
    await _worker(session_factory, settings, fake_cli, "default", publisher).run_once()
    done = await service.get(run.id)
    assert done.state == "DONE" and done.task_text == task
    assert len(publisher.opened_prs) == 1
    body = publisher.opened_prs[0]["body"]
    assert len(body) <= 60000 and "[TRUNCATED" in body
    assert "Completed tasks:" in body
    assert body.endswith(f"Opened by Forge direct run {run.id}")


@pytest.mark.integration
async def test_epic_decomposition_session_failures(session_factory, tmp_path, fake_cli):
    _repo_obj, pid, settings, service = await _setup(session_factory, tmp_path)

    # 1. Malformed JSON
    run_malformed = await service.create(
        project_id=pid, task="epic malformed", client="muse_code", model="model-1", effort=None, shape="epic"
    )
    worker_malformed = _worker(session_factory, settings, fake_cli, "decompose-fail")
    await worker_malformed.run_once()
    record = await service.get(run_malformed.id)
    assert record.state == "FAILED"
    assert record.failure_reason == "malformed decomposition"
    tasks_malformed = await service.tasks(run_malformed.id)
    assert len(tasks_malformed) == 0

    # 2. Empty tasks
    run_empty = await service.create(
        project_id=pid, task="epic empty", client="muse_code", model="model-1", effort=None, shape="epic"
    )
    worker_empty = _worker(session_factory, settings, fake_cli, "decompose-empty")
    await worker_empty.run_once()
    record_empty = await service.get(run_empty.id)
    assert record_empty.state == "FAILED"
    assert record_empty.failure_reason == "malformed decomposition"
    tasks_empty = await service.tasks(run_empty.id)
    assert len(tasks_empty) == 0

    # 3. Oversized tasks
    run_oversized = await service.create(
        project_id=pid, task="epic oversized", client="muse_code", model="model-1", effort=None, shape="epic"
    )
    worker_oversized = _worker(session_factory, settings, fake_cli, "decompose-oversized")
    await worker_oversized.run_once()
    record_oversized = await service.get(run_oversized.id)
    assert record_oversized.state == "FAILED"
    assert record_oversized.failure_reason == "malformed decomposition"


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["decompose-edit", "decompose-commit"])
async def test_epic_decomposition_edits_are_reverted(session_factory, tmp_path, fake_cli, mode):
    _repo_obj, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid, task="epic dirty decomposer", client="muse_code", model="model-1", effort=None, shape="epic"
    )
    worker = _worker(session_factory, settings, fake_cli, mode)
    await worker.run_once()

    done = await service.get(run.id)
    assert done.state == "DONE"
    # Verify the worktree does not retain dirty_decompose.txt
    wt = Path(done.worktree_path)
    assert not (wt / "dirty_decompose.txt").exists()
    assert "illegal decomposition commit" not in _git(wt, "log", "--format=%s")


@pytest.mark.integration
async def test_epic_decomposition_review_approval_flow(session_factory, tmp_path, fake_cli):
    _repo_obj, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid,
        task="epic with decomposition review",
        client="muse_code",
        model="model-1",
        effort=None,
        shape="epic",
        review_plan_first=True,
    )
    worker = _worker(session_factory, settings, fake_cli, "decompose-3")

    # Pass 1: decompose, create 3 tasks, park in AWAITING_DECOMPOSITION_APPROVAL
    await worker.run_once()
    parked = await service.get(run.id)
    assert parked.state == "AWAITING_DECOMPOSITION_APPROVAL"
    tasks_parked = await service.tasks(run.id)
    assert len(tasks_parked) == 3
    assert [t.task_text for t in tasks_parked] == ["first step", "second step", "third step"]

    # Owner edits / reorders pending tasks: swap first and second, edit third
    new_tasks = [
        {"task": "second step revised", "shape": "planned"},
        {"task": "first step", "shape": "quick"},
        {"task": "third step finalized", "shape": "quick"},
    ]
    updated_tasks = await service.replace_tasks(run.id, new_tasks, expected_version=parked.control_version)
    assert len(updated_tasks) == 3
    assert updated_tasks[0].task_text == "second step revised"
    assert updated_tasks[0].shape == "planned"

    # Owner approves decomposition
    approved = await service.approve_decomposition(run.id, expected_version=(await service.get(run.id)).control_version)
    assert approved.state == "QUEUED"
    assert approved.plan_approved is True

    # Pass 2: worker runs to completion with the reordered tasks
    await worker.run_once()
    child_parked = await service.get(run.id)
    assert child_parked.state == "AWAITING_PLAN_APPROVAL"
    child_tasks = await service.tasks(run.id)
    await service.approve_plan(run.id, expected_version=child_parked.control_version, task_id=child_tasks[0].id)
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    tasks_done = await service.tasks(run.id)
    assert len(tasks_done) == 3
    assert [t.task_text for t in tasks_done] == [
        "second step revised", "first step", "third step finalized"
    ]
    assert all(t.state == "DONE" for t in tasks_done)


@pytest.mark.integration
async def test_epic_failure_or_decision_in_child2_retains_child1(session_factory, tmp_path, fake_cli):
    _repo_obj, pid, settings, service = await _setup(session_factory, tmp_path)

    # Test failure in child 2
    run_fail = await service.create(
        project_id=pid, task="epic fail on child 2", client="muse_code", model="model-1", effort=None, shape="epic"
    )
    worker_fail = _worker(session_factory, settings, fake_cli, "fail-child-2")
    await worker_fail.run_once()

    record_fail = await service.get(run_fail.id)
    assert record_fail.state == "FAILED"
    tasks_fail = await service.tasks(run_fail.id)
    assert tasks_fail[0].state == "DONE"
    assert tasks_fail[0].head_sha is not None
    assert tasks_fail[1].state == "FAILED"

    # Test needs_decision in child 2
    run_decision = await service.create(
        project_id=pid, task="epic decision on child 2", client="muse_code", model="model-1", effort=None, shape="epic"
    )
    worker_decision = _worker(session_factory, settings, fake_cli, "decision-child-2")
    await worker_decision.run_once()

    record_decision = await service.get(run_decision.id)
    assert record_decision.state == "NEEDS_DECISION"
    assert record_decision.question == "Proceed?"
    assert record_decision.options == ("yes", "no")
    tasks_decision = await service.tasks(run_decision.id)
    assert tasks_decision[0].state == "DONE"
    assert tasks_decision[0].head_sha is not None
    assert tasks_decision[1].state == "NEEDS_DECISION"


@pytest.mark.integration
async def test_epic_cancellation_while_parked(session_factory, tmp_path, fake_cli):
    _repo_obj, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid,
        task="epic to cancel while parked",
        client="muse_code",
        model="model-1",
        effort=None,
        shape="epic",
        review_plan_first=True,
    )
    worker = _worker(session_factory, settings, fake_cli, "default")
    await worker.run_once()

    parked = await service.get(run.id)
    assert parked.state == "AWAITING_DECOMPOSITION_APPROVAL"

    cancelled = await service.cancel(run.id)
    assert cancelled.state == "CANCELLED"

    # Worker running again does nothing
    assert await worker.run_once() is None


@pytest.mark.integration
async def test_epic_noop_child_accepted(session_factory, tmp_path, fake_cli):
    _repo_obj, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(
        project_id=pid, task="epic with noop child 2", client="muse_code", model="model-1", effort=None, shape="epic"
    )
    worker = _worker(session_factory, settings, fake_cli, "noop-child-2")
    await worker.run_once()

    done = await service.get(run.id)
    assert done.state == "DONE"
    tasks_done = await service.tasks(run.id)
    assert tasks_done[0].state == "DONE"
    assert tasks_done[1].state == "DONE"
    # Child 2 head_sha equals child 1 head_sha because noop
    assert tasks_done[1].head_sha == tasks_done[0].head_sha


@pytest.mark.integration
async def test_epic_session_budget_exhaustion(session_factory, tmp_path, fake_cli):
    _repo_obj, pid, settings, service = await _setup(session_factory, tmp_path)
    # Set max sessions per run to 2 (1 for decompose, 1 for child 1 implement -> child 2 plan budget exhausted)
    settings_tight = Settings(
        data_root=settings.data_root,
        subscription_installations_path=settings.subscription_installations_path,
        direct_max_sessions_per_run=2,
        direct_review_policy="skip",
        direct_pr_poll_seconds=0,
        direct_pr_quiet_seconds=0,
    )
    run = await service.create(
        project_id=pid, task="epic budget test", client="muse_code", model="model-1", effort=None, shape="epic"
    )
    worker = _worker(session_factory, settings_tight, fake_cli, "default")
    await worker.run_once()

    record = await service.get(run.id)
    assert record.state == "FAILED"
    assert record.failure_reason == "direct run session budget exhausted"
    tasks_record = await service.tasks(run.id)
    assert tasks_record[0].state == "DONE"
    assert tasks_record[1].state == "FAILED"


async def _install_checks(session_factory, pid, repo_path, code):
    (repo_path / "epic_check.py").write_text(code)
    _git(repo_path, "add", "epic_check.py")
    _git(repo_path, "commit", "-m", "install test check")
    policy = ProjectPolicy(
        id=pid, version=1, repository_path=str(repo_path),
        github_repository=f"o/r-{pid}", default_branch="main",
        commands=(CommandSpec(name="smoke", kind=StepKind.TEST,
                              argv=(sys.executable, "epic_check.py"), timeout_seconds=30),),
    )
    async with session_factory() as session:
        session.add(ProjectPolicyVersion(project_id=pid, version=1, policy_digest="a" * 64,
                                        document_schema_version=1, document=policy.model_dump(mode="json")))
        await session.flush()
        project = await session.get(Project, pid)
        project.current_policy_version = 1
        await session.commit()


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["check-repair", "review-repair"])
async def test_epic_repairs_settle_before_next_child(session_factory, tmp_path, fake_cli, mode):
    repo, pid, settings, service = await _setup(session_factory, tmp_path)
    settings = settings.model_copy(update={"direct_review_policy": "cross_provider", "direct_max_review_rounds": 2})
    code = ("from pathlib import Path\nassert len(Path('progress.log').read_text().splitlines()) != 2 or Path('check-fixed').exists()" if mode == "check-repair"
            else "print('passed')")
    await _install_checks(session_factory, pid, tmp_path / "canonical", code)
    run = await service.create(project_id=pid, task="repair epic", client="muse_code", model="model-1", effort=None, shape="epic")
    publisher = _QuietPublisher()
    await _worker(session_factory, settings, fake_cli, mode, publisher).run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    children = await service.tasks(run.id)
    sessions = await service.sessions(run.id)
    reviews = await service.reviews(run.id)
    checks = await repo.checks(run.id)
    assert all(c.state == "DONE" for c in children)
    assert len(publisher.opened_prs) == 1
    assert "repair epic" in publisher.opened_prs[0]["body"]
    assert all(s.task_id is not None for s in sessions if s.kind != "decompose")
    assert all(c.task_id in {t.id for t in children} for c in checks)
    assert all(r.task_id in {t.id for t in children} for r in reviews)
    if mode == "check-repair":
        assert [c.status for c in checks] == ["PASSED", "FAILED", "PASSED", "PASSED"]
        assert [s.kind for s in sessions] == ["decompose", "implement", "review", "plan", "implement", "repair", "review", "implement", "review"]
        assert [c.task_id for c in checks] == [children[0].id, children[1].id, children[1].id, children[2].id]
    else:
        assert [r.verdict for r in reviews] == ["clean", "blocking", "clean", "clean"]
        assert [r.round for r in reviews] == [0, 1, 2, 3]
        assert [r.task_id for r in reviews] == [children[0].id, children[1].id, children[1].id, children[2].id]
        assert [s.kind for s in sessions] == ["decompose", "implement", "review", "plan", "implement", "review", "repair", "review", "implement", "review"]
        assert [c.status for c in checks] == ["PASSED", "PASSED", "PASSED", "PASSED"]
        assert children[1].plan_text in next(p.read_text() for p in fake_cli.parent.glob("prompt-*.txt") if "repair session" in p.read_text().lower())
    assert [s.task_id for s in sessions if s.kind == "repair"] == [children[1].id]
    assert Path(done.worktree_path, "progress.log").read_text().splitlines() == ["step 1", "step 2", "step other"]
    assert _git(Path(done.worktree_path), "merge-base", children[1].head_sha, children[2].head_sha) == children[1].head_sha


@pytest.mark.integration
async def test_epic_child_plan_approval_resumes_after_done_child(session_factory, tmp_path, fake_cli):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(project_id=pid, task="review epic", client="muse_code", model="model-1", effort=None, shape="epic", review_plan_first=True)
    publisher = _QuietPublisher()
    worker = _worker(session_factory, settings, fake_cli, "default", publisher)
    await worker.run_once()
    decomposed = await service.get(run.id)
    await service.approve_decomposition(run.id, expected_version=decomposed.control_version)
    await worker.run_once()
    parked = await service.get(run.id)
    children = await service.tasks(run.id)
    assert parked.state == "AWAITING_PLAN_APPROVAL"
    assert children[0].state == "DONE"
    assert children[1].state == "AWAITING_PLAN_APPROVAL"
    assert parked.approval_task_id == children[1].id
    assert publisher.opened_prs == []
    with pytest.raises(ValueError, match="stale"):
        await service.approve_plan(run.id, expected_version=decomposed.control_version, task_id=children[1].id)
    with pytest.raises(ValueError, match="stale child"):
        await service.approve_plan(run.id, expected_version=parked.control_version, task_id=children[0].id)
    await service.approve_plan(run.id, expected_version=parked.control_version, task_id=children[1].id)
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    assert done.worktree_path == parked.worktree_path
    assert [s.kind for s in await service.sessions(run.id)] == ["decompose", "implement", "plan", "implement"]
    assert (await service.tasks(run.id))[0] == children[0]
    assert Path(done.worktree_path, "progress.log").read_text().splitlines() == ["step 1", "step 2"]
    assert len(publisher.opened_prs) == 1


@pytest.mark.integration
@pytest.mark.parametrize("mode,state", [("decompose-decision", "NEEDS_DECISION"), ("decompose-blocked", "FAILED"), ("decompose-nonzero", "FAILED")])
async def test_epic_decomposition_non_success_never_creates_children(session_factory, tmp_path, fake_cli, mode, state):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(project_id=pid, task="stop epic", client="muse_code", model="model-1", effort=None, shape="epic")
    publisher = _QuietPublisher()
    await _worker(session_factory, settings, fake_cli, mode, publisher).run_once()
    assert (await service.get(run.id)).state == state
    assert await service.tasks(run.id) == ()
    assert len(await service.sessions(run.id)) == 1
    assert publisher.opened_prs == []


@pytest.mark.integration
@pytest.mark.parametrize("payload,state,mode", [
    ('{"status":"blocked","summary":"final blocked","tasks":[]}', "FAILED", "default"),
    ('{"status":"blocked","summary":"missing tasks"}', "FAILED", "default"),
    ('{"status":"needs_decision","summary":"choose","question":"Which?","tasks":[]}', "NEEDS_DECISION", "default"),
    ('{"status":"done", malformed}', "FAILED", "default"),
    ('', "FAILED", "default"),
    ('```json\n{"status":"blocked","summary":"final blocked","tasks":[]}\n```', "FAILED", "default"),
    ('{"result":"```json\\n{\\"status\\":\\"blocked\\",\\"summary\\":\\"final blocked\\",\\"tasks\\":[]}\\n```"}', "FAILED", "default"),
    ('{"response":{"status":"needs_decision","summary":"choose","question":"Which?","tasks":[]}}', "NEEDS_DECISION", "default"),
    ('{"status":"done","summary":"success","tasks":[{"task":"no launch","shape":"quick"}]}', "FAILED", "decompose-nonzero"),
    ('{"status":"done","summary":"success","tasks":[{"task":"no launch","shape":"quick"}]}', "CANCELLED", "cancelled"),
], ids=["blocked", "missing-tasks", "decision", "malformed", "empty", "fenced", "event-text", "event-object", "nonzero-precedence", "cancel-precedence"])
async def test_epic_final_file_cannot_be_overridden_by_earlier_stdout(session_factory, tmp_path, fake_cli, payload, state, mode, monkeypatch):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(project_id=pid, task="final answer epic", client="muse_code", model="model-1", effort=None, shape="epic")
    publisher = _QuietPublisher()
    worker = _worker(session_factory, settings, fake_cli, mode, publisher)
    factory = worker._argv_factory

    def with_final_file(installation, ctx):
        if "DECOMPOSE: You are decomposing" in ctx.prompt:
            ctx.final_file.write_text(payload, encoding="utf-8")
        return factory(installation, ctx)

    worker._argv_factory = with_final_file
    if mode == "cancelled":
        from unittest.mock import AsyncMock

        monkeypatch.setattr(worker, "_run_session", AsyncMock(return_value=("cancelled", None, "")))
    await worker.run_once()
    assert (await service.get(run.id)).state == state
    assert await service.tasks(run.id) == ()
    assert len(await service.sessions(run.id)) == 1
    assert publisher.opened_prs == []


@pytest.mark.integration
@pytest.mark.parametrize("provider,outcome,via_final_file", [
    (provider, outcome, via_file)
    for provider in ("antigravity", "claude", "codex", "muse", "legacy")
    for outcome in ("done", "blocked", "malformed")
    for via_file in (False, True)
] + [
    ("plain", outcome, via_file)
    for outcome in ("null", "bool", "string", "number")
    for via_file in (False, True)
])
async def test_epic_wire_records_and_final_files(session_factory, tmp_path, provider, outcome, via_final_file):
    import json

    from apps.orchestrator.tests.domain.test_direct_epic_domain import _provider_wire

    _, pid, settings, service = await _setup(session_factory, tmp_path)
    task = 'Document literal ```json and ``` examples with escaped "quotes".'
    payload = {"status": outcome, "summary": "wire " + outcome, "tasks": [{"task": task, "shape": "quick"}] if outcome == "done" else []}
    raw = json.dumps(payload, indent=2) if outcome != "malformed" else '{"status":"done", malformed}'
    primitives = {"null": None, "bool": False, "string": "not a role object", "number": 42}
    if outcome in primitives:
        raw = json.dumps(primitives[outcome])
    wire = raw if provider == "plain" else json.dumps(_provider_wire(f"```json\n{raw}\n```", provider))
    earlier = json.dumps({"status": "done", "summary": "earlier", "tasks": [{"task": "must not launch", "shape": "quick"}]})
    stdout = earlier + ("\n" + wire if not via_final_file else "") + '\n{"type":"completed"}'
    script = tmp_path / "wire_cli.py"
    script.write_text('''import json, sys
from pathlib import Path
prompt = Path(sys.argv[1]).read_text(encoding="utf-8")
if "DECOMPOSE: You are decomposing" in prompt:
    print(sys.argv[2])
else:
    Path("wire-work.txt").write_text("implemented")
    print(json.dumps({"status":"done","summary":"implemented"}))
''', encoding="utf-8")
    run = await service.create(project_id=pid, task="wire epic", client="muse_code", model="model-1", effort=None, shape="epic")
    publisher = _QuietPublisher()

    def factory(installation, ctx):
        if via_final_file and "DECOMPOSE: You are decomposing" in ctx.prompt:
            ctx.final_file.write_text(wire, encoding="utf-8")
        return DirectLaunch(argv=(sys.executable, str(script), str(ctx.prompt_file), stdout), stdin_data=b"")

    worker = DirectRunWorker(session_factory, settings, argv_factory=factory,
                             installation_resolver=lambda run: SimpleNamespace(client=run.client, model=run.model),
                             publisher=publisher)
    await worker.run_once()
    settled = await service.get(run.id)
    children = await service.tasks(run.id)
    if outcome == "done":
        assert settled.state == "DONE"
        assert len(children) == 1 and children[0].task_text == task and children[0].state == "DONE"
        assert Path(settled.worktree_path, "wire-work.txt").read_text() == "implemented"
        assert [s.kind for s in await service.sessions(run.id)] == ["decompose", "implement"]
        assert len(publisher.opened_prs) == 1
    else:
        assert settled.state == "FAILED"
        assert settled.failure_reason == ("wire blocked" if outcome == "blocked" else "malformed decomposition")
        assert children == ()
        assert [s.kind for s in await service.sessions(run.id)] == ["decompose"]
        assert publisher.opened_prs == []


@pytest.mark.integration
async def test_epic_cancel_second_child_preserves_first_and_partial_edit(session_factory, tmp_path, fake_cli):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(project_id=pid, task="cancel epic", client="muse_code", model="model-1", effort=None, shape="epic")
    publisher = _QuietPublisher()
    worker = _worker(session_factory, settings, fake_cli, "sleep-child-2", publisher)
    running = asyncio.create_task(worker.run_once())
    try:
        for _ in range(200):
            current = await service.get(run.id)
            if current.worktree_path and Path(current.worktree_path, "partial.txt").exists():
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("second child never started")
        await service.cancel(run.id)
        await asyncio.wait_for(running, timeout=30)
    finally:
        if not running.done():
            await service.cancel(run.id)
            await asyncio.wait_for(running, timeout=30)
    cancelled = await service.get(run.id)
    children = await service.tasks(run.id)
    assert cancelled.state == "CANCELLED"
    assert [t.state for t in children] == ["DONE", "CANCELLED"]
    assert Path(cancelled.worktree_path, "partial.txt").read_text() == "retained partial edit"
    assert _git(Path(cancelled.worktree_path), "rev-parse", "HEAD") == children[0].head_sha
    assert publisher.opened_prs == []


@pytest.mark.integration
async def test_epic_decomposer_restore_failure_never_accepts_tasks(session_factory, tmp_path, fake_cli, monkeypatch):
    from unittest.mock import AsyncMock

    _, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(project_id=pid, task="restore failure", client="muse_code", model="model-1", effort=None, shape="epic")
    publisher = _QuietPublisher()
    monkeypatch.setattr("forge.worker.direct._revert_reviewer_changes", AsyncMock(return_value=(True, False)))
    await _worker(session_factory, settings, fake_cli, "decompose-edit", publisher).run_once()
    stopped = await service.get(run.id)
    assert stopped.state == "FAILED"
    assert stopped.failure_reason == "decomposer modified worktree; candidate could not be restored"
    assert await service.tasks(run.id) == ()
    assert [s.state for s in await service.sessions(run.id)] == ["FAILED"]
    assert publisher.opened_prs == []


@pytest.mark.integration
async def test_epic_orphan_retains_completed_child_and_partial_work(session_factory, tmp_path, fake_cli):
    repo, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(project_id=pid, task="interrupted epic", client="muse_code", model="model-1", effort=None, shape="epic", review_plan_first=True)
    publisher = _QuietPublisher()
    worker = _worker(session_factory, settings, fake_cli, "default", publisher)
    await worker.run_once()
    await service.approve_decomposition(run.id, expected_version=(await service.get(run.id)).control_version)
    await worker.run_once()
    parked = await service.get(run.id)
    children = await service.tasks(run.id)
    await service.approve_plan(run.id, expected_version=parked.control_version, task_id=children[1].id)
    claimed = await repo.claim_next()
    assert claimed is not None
    await repo.update_task(children[1].id, state="RUNNING")
    Path(parked.worktree_path, "partial.txt").write_text("retained")
    session = await repo.create_session(run_id=run.id, run=claimed, task_id=children[1].id)
    provider = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)",
        cwd=parked.worktree_path, start_new_session=(sys.platform != "win32"),
    )
    try:
        await repo.update_session(session.id, pid=provider.pid)
        assert await worker.run_once() is None
        assert await asyncio.wait_for(provider.wait(), timeout=5) != 0
    finally:
        if provider.returncode is None:
            with suppress(ProcessLookupError):
                provider.kill()
            await provider.wait()
    recovered_session = next(row for row in await repo.sessions(run.id) if row.id == session.id)
    assert recovered_session.state == "FAILED"
    assert recovered_session.failure_reason == "worker interrupted"
    assert (await service.get(run.id)).failure_reason == "worker interrupted"
    final = await service.tasks(run.id)
    assert final[0] == children[0]
    assert final[1].state == "FAILED"
    assert Path(parked.worktree_path, "partial.txt").read_text() == "retained"
    assert publisher.opened_prs == []


@pytest.mark.integration
async def test_epic_cancel_during_second_child_check_stops_third(session_factory, tmp_path, fake_cli):
    _repo_obj, pid, settings, service = await _setup(session_factory, tmp_path)
    await _install_checks(session_factory, pid, tmp_path / "canonical", "from pathlib import Path\nimport time\nif len(Path('progress.log').read_text().splitlines()) == 2:\n    Path('checking-second').touch()\n    time.sleep(120)\n")
    run = await service.create(project_id=pid, task="cancel check epic", client="muse_code", model="model-1", effort=None, shape="epic")
    publisher = _QuietPublisher()
    running = asyncio.create_task(_worker(session_factory, settings, fake_cli, "cancel-check", publisher).run_once())
    try:
        for _ in range(300):
            current = await service.get(run.id)
            if current.worktree_path and Path(current.worktree_path, "checking-second").exists():
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("second child check did not start")
    finally:
        await service.cancel(run.id)
        await asyncio.wait_for(running, timeout=30)
    assert (await service.get(run.id)).state == "CANCELLED"
    assert [t.state for t in await service.tasks(run.id)] == ["DONE", "CANCELLED", "PENDING"]
    assert [s.kind for s in await service.sessions(run.id)] == ["decompose", "implement", "plan", "implement"]
    assert publisher.opened_prs == []


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["check-repair", "review-repair"])
async def test_epic_second_child_round_exhaustion_prevents_third(session_factory, tmp_path, fake_cli, mode):
    _, pid, settings, service = await _setup(session_factory, tmp_path)
    settings = settings.model_copy(update={"direct_review_policy": "cross_provider", "direct_max_repair_rounds": 0, "direct_max_review_rounds": 1})
    code = ("from pathlib import Path\nassert len(Path('progress.log').read_text().splitlines()) != 2" if mode == "check-repair" else "print('passed')")
    await _install_checks(session_factory, pid, tmp_path / "canonical", code)
    run = await service.create(project_id=pid, task="bounded epic", client="muse_code", model="model-1", effort=None, shape="epic")
    publisher = _QuietPublisher()
    await _worker(session_factory, settings, fake_cli, mode, publisher).run_once()
    failed = await service.get(run.id)
    assert failed.state == "FAILED"
    assert "checks still failing" in failed.failure_reason if mode == "check-repair" else "review still blocking" in failed.failure_reason
    assert [t.state for t in await service.tasks(run.id)] == ["DONE", "FAILED", "PENDING"]
    assert all(s.kind != "repair" for s in await service.sessions(run.id))
    assert Path(failed.worktree_path, "progress.log").read_text().splitlines() == ["step 1", "step 2"]
    assert publisher.opened_prs == []


@pytest.mark.integration
async def test_epic_cumulative_duration_budget_after_completed_child(session_factory, tmp_path, fake_cli, monkeypatch):
    from datetime import timedelta

    repo, pid, settings, service = await _setup(session_factory, tmp_path)
    run = await service.create(project_id=pid, task="time bounded epic", client="muse_code", model="model-1", effort=None, shape="epic")
    publisher = _QuietPublisher()
    worker = _worker(session_factory, settings, fake_cli, "default", publisher)
    original = worker._session_budget_exhausted

    async def exhausted_after_first(run_id):
        children = await service.tasks(run_id)
        if children and children[0].state == "DONE":
            # Model elapsed recorded usage without an actual four-hour session.
            first = (await service.sessions(run_id))[0]
            await repo.update_session(first.id, ended_at=first.started_at + timedelta(seconds=settings.direct_max_total_session_seconds))
        return await original(run_id)

    monkeypatch.setattr(worker, "_session_budget_exhausted", exhausted_after_first)
    await worker.run_once()
    failed = await service.get(run.id)
    assert failed.failure_reason == "direct run session budget exhausted"
    assert [t.state for t in await service.tasks(run.id)] == ["DONE", "FAILED"]
    assert [s.kind for s in await service.sessions(run.id)] == ["decompose", "implement"]
    assert Path(failed.worktree_path, "progress.log").read_text().splitlines() == ["step 1"]
    assert publisher.opened_prs == []


@pytest.mark.integration
async def test_decomposition_capture_preserves_stdout_beyond_redactor_limit(session_factory, tmp_path):
    import json

    from apps.orchestrator.tests.domain.test_direct_epic_domain import _provider_wire

    _, pid, settings, service = await _setup(session_factory, tmp_path)
    tasks = [{"task": "x" * 6000 + str(i), "shape": "quick"} for i in range(2)]
    payload = {"status": "done", "summary": "large valid decomposition", "tasks": tasks}
    wire = json.dumps(_provider_wire(json.dumps(payload), "muse")).encode()
    assert 8192 < len(wire) < 262144
    data = tmp_path / "wire.json"
    data.write_bytes(wire)
    script = tmp_path / "capture_cli.py"
    script.write_text("import sys\nfrom pathlib import Path\nsys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())\n")
    run = await service.create(project_id=pid, task="large capture", client="muse_code", model="model-1", effort=None, shape="epic", review_plan_first=True)
    worker = DirectRunWorker(session_factory, settings,
                             argv_factory=lambda installation, ctx: DirectLaunch(argv=(sys.executable, str(script), str(data)), stdin_data=b""),
                             installation_resolver=lambda run: SimpleNamespace(client=run.client, model=run.model))
    await worker.run_once()
    assert (await service.get(run.id)).state == "AWAITING_DECOMPOSITION_APPROVAL"
    assert [t.task_text for t in await service.tasks(run.id)] == [t["task"] for t in tasks]


async def _capture_case(session_factory, tmp_path, stdout, *, final=None, secret=None, exit_code=0, sleep=False):
    from forge.observability.redaction import RedactionPolicy, Redactor

    _, pid, settings, service = await _setup(session_factory, tmp_path)
    updates = {"direct_max_transcript_bytes": 4096}
    if sleep:
        updates["direct_session_timeout_seconds"] = 1
    settings = settings.model_copy(update=updates)
    data = tmp_path / "capture-input.json"
    data.write_bytes(stdout)
    script = tmp_path / "bounded_capture_cli.py"
    script.write_text("import sys, time\nfrom pathlib import Path\nsys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())\nsys.stdout.buffer.flush()\ntime.sleep(120 if sys.argv[3] == 'sleep' else 0)\nsys.exit(int(sys.argv[2]))\n")
    run = await service.create(project_id=pid, task="bounded capture", client="muse_code", model="model-1", effort=None, shape="epic", review_plan_first=True)

    def factory(installation, ctx):
        if final is not None:
            ctx.final_file.write_bytes(final)
        return DirectLaunch(argv=(sys.executable, str(script), str(data), str(exit_code), "sleep" if sleep else "exit"), stdin_data=b"")

    worker = DirectRunWorker(session_factory, settings, argv_factory=factory,
                             installation_resolver=lambda run: SimpleNamespace(client=run.client, model=run.model))
    if secret:
        worker._redactor = Redactor(secrets=(secret,), policy=RedactionPolicy(max_string_bytes=8192))
    await worker.run_once()
    return await service.get(run.id), await service.tasks(run.id), await service.sessions(run.id)


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["antigravity", "claude", "codex", "muse", "legacy"])
async def test_decomposition_capture_max_escaped_unicode_tasks(session_factory, tmp_path, provider):
    import json

    from apps.orchestrator.tests.domain.test_direct_epic_domain import _provider_wire

    tasks = [{"task": "🧭" * 7998 + f"{i:02}", "shape": "quick"} for i in range(20)]
    wire = json.dumps(_provider_wire(json.dumps({"status": "done", "summary": "maximum tasks", "tasks": tasks}), provider)).encode()
    assert len(wire) > 262144
    parked, children, sessions = await _capture_case(session_factory, tmp_path, wire)
    assert parked.state == "AWAITING_DECOMPOSITION_APPROVAL"
    assert [t.task_text for t in children] == [t["task"] for t in tasks]
    assert len(sessions) == 1
    assert Path(sessions[0].transcript_path).stat().st_size <= 4096 + 128


@pytest.mark.integration
@pytest.mark.parametrize("case", ["single-line", "multi-line", "valid-file", "oversized-file", "empty-file", "malformed-file", "nonzero", "timeout"])
async def test_decomposition_capture_overflow_and_source_authority(session_factory, tmp_path, case):
    import json

    limit = 8 * 1024 * 1024
    valid = json.dumps({"status": "done", "summary": "accepted final", "tasks": [{"task": "final task", "shape": "quick"}]}).encode()
    oversized = valid + b"\n" + b"x" * (limit + 1) + b"\n" + valid
    stdout = oversized
    final = None
    if case == "multi-line":
        stdout = valid + b"\n" + (b"x" * (512 * 1024) + b"\n") * 17 + valid
    elif case in {"valid-file", "nonzero", "timeout"}:
        final = valid
    elif case in {"oversized-file", "empty-file", "malformed-file"}:
        stdout = valid
        final = {"oversized-file": oversized, "empty-file": b"", "malformed-file": b"{bad}"}[case]
    settled, children, sessions = await _capture_case(session_factory, tmp_path, stdout, final=final,
                                                      exit_code=3 if case == "nonzero" else 0, sleep=case == "timeout")
    if case == "valid-file":
        assert settled.state == "AWAITING_DECOMPOSITION_APPROVAL"
        assert [t.task_text for t in children] == ["final task"]
    else:
        assert settled.state == "FAILED" and children == ()
        reason = ("malformed decomposition" if case in {"empty-file", "malformed-file"} else
                  "session exited with code 3" if case == "nonzero" else
                  "session timed out" if case == "timeout" else "decomposition output exceeded capture limit")
        assert settled.failure_reason == reason
    assert len(sessions) == 1


@pytest.mark.integration
@pytest.mark.parametrize("via_file", [False, True])
async def test_decomposition_capture_persistence_redacts_configured_secret(session_factory, tmp_path, via_file):
    import json

    secret = "configured-test-" + uuid4().hex
    wire = json.dumps({"status": "done", "summary": "summary " + secret,
                       "tasks": [{"task": "task " + secret, "shape": "quick"}]}).encode()
    parked, children, sessions = await _capture_case(session_factory, tmp_path, wire, final=wire if via_file else None, secret=secret)
    assert parked.state == "AWAITING_DECOMPOSITION_APPROVAL"
    persisted = str((parked.summary, [t.task_text for t in children]))
    persisted += "".join(p.read_text(encoding="utf-8") for p in Path(sessions[0].transcript_path).parent.iterdir() if p.is_file())
    if secret in persisted:
        pytest.fail("configured secret leaked into durable decomposition evidence")
    assert "[REDACTED]" in parked.summary
    assert "[REDACTED]" in children[0].task_text


@pytest.mark.integration
@pytest.mark.parametrize("via_file", [False, True])
async def test_malformed_decomposition_redacts_before_slicing_error_tail(session_factory, tmp_path, via_file):
    secret = "configured-tail-" + uuid4().hex + uuid4().hex
    wire = ("invalid output " + secret + "z" * 1960).encode()
    settled, children, _sessions = await _capture_case(session_factory, tmp_path, wire,
                                                       final=wire if via_file else None, secret=secret)
    assert settled.state == "FAILED" and children == ()
    if secret[-16:] in settled.summary:
        pytest.fail("configured secret suffix leaked across error-tail slicing")
