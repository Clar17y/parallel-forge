"""Slice D: PR open + autonomous PR-comment loop (fake publisher, no gh)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.services.direct_runs import DirectRunService
from forge.domain.direct import DirectLaunch, DirectPrFeedback, DirectPrSnapshot
from forge.domain.policy import CommandSpec, ProjectPolicy, StepKind
from forge.persistence.models import Project, ProjectPolicyVersion
from forge.settings import Settings
from forge.worker.direct import DirectRunWorker, PublisherError

PAST = datetime.now(UTC) - timedelta(hours=1)

FAKE_CLI = '''
import sys
from pathlib import Path
wt = Path.cwd()
prompt = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else ""
mode = sys.argv[2]
if "REVIEW" in prompt:
    print('```json'); print('{"verdict":"clean","summary":"ok","findings":[]}'); print('```', flush=True)
elif "PR comment" in prompt:
    if mode == "pr_repair_breaks_checks":
        (wt / "BROKEN.txt").write_text("broken\\n")
    else:
        (wt / f"pr-repair-{len(list(wt.glob('pr-repair-*.txt')))+1}.txt").write_text("fixed\\n")
    print('```json'); print('{"status":"done","summary":"repaired pr","question":null,"options":[]}'); print('```', flush=True)
else:
    (wt / "changed.txt").write_text("changed\\n")
    (wt / "FIXED.txt").write_text("fixed\\n")
    print('```json'); print('{"status":"done","summary":"did the thing","question":null,"options":[]}'); print('```', flush=True)
'''

CHECK_PY = '''
from pathlib import Path
import sys
sys.exit(1 if Path("BROKEN.txt").exists() else 0)
'''


def snap(*, threads=(), changes=(), checks="SUCCESS", activity=True, created=PAST, last=PAST):
    return DirectPrSnapshot(state="OPEN", mergeable="MERGEABLE", checks_status=checks,
                            unresolved_threads=tuple(threads), changes_requested=tuple(changes),
                            last_activity_at=last if activity else None, has_activity=activity,
                            created_at=created)


def thread(tid, body="please fix this", author="chatgpt-codex-connector"):
    return DirectPrFeedback(id=tid, thread_id=tid, comment_id=f"c-{tid}", body=body, author=author, updated_at=PAST)


def changes_requested(rid, author="chatgpt-codex-connector", updated=None):
    return DirectPrFeedback(id=rid, body="please change", author=author, updated_at=updated)


class FakePublisher:
    def __init__(self, script=None, *, push_fails=False, poll_fails=False, dynamic_threads=False):
        self.script = list(script or [snap()])
        self.push_fails = push_fails
        self.poll_fails = poll_fails
        self.dynamic_threads = dynamic_threads
        self.pushes: list = []
        self.creates: list = []
        self.replies: list = []
        self.resolves: list = []
        self.closes: list = []
        self.polls = 0

    async def push(self, worktree, branch):
        self.pushes.append((str(worktree), branch))
        if self.push_fails:
            raise PublisherError("push failed")

    async def open_pr(self, *, repo, base, head, title, body, worktree):
        self.creates.append({"repo": repo, "base": base, "head": head, "title": title, "body": body})
        return 42, "https://example.test/pr/42"

    async def poll(self, *, repo, number):
        self.polls += 1
        if self.poll_fails:
            raise PublisherError("gh failed")
        if self.dynamic_threads:
            return snap(threads=[thread(f"t{self.polls}")])
        return self.script[min(self.polls - 1, len(self.script) - 1)]

    async def reply(self, *, repo, number, feedback, body):
        self.replies.append((feedback.id, body))

    async def resolve(self, *, repo, feedback):
        self.resolves.append(feedback.id)

    async def close_pr(self, *, repo, number):
        self.closes.append(number)


class QuietPublisher(FakePublisher):
    def __init__(self):
        super().__init__([snap()])


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


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


async def _setup(session_factory, tmp_path, *, mode="ok", publisher, github="o/r", settings_extra=None, with_policy=False):
    repo = _repo(tmp_path)
    pid = uuid4()
    github_repo = f"{github}-{pid}" if github else ""
    async with session_factory() as session:
        project = Project(id=pid, name="P", canonical_path=str(repo), canonical_path_key=str(repo).lower(), github_repository=github_repo, default_branch="main")
        session.add(project)
        if with_policy:
            await session.flush()
            policy = ProjectPolicy(id=pid, version=1, repository_path=str(repo), github_repository=github_repo, default_branch="main",
                                   commands=(CommandSpec(kind=StepKind.TEST, name="smoke", argv=(sys.executable, "check.py"), timeout_seconds=60),))
            session.add(ProjectPolicyVersion(project_id=pid, version=1, policy_digest="a" * 64, document_schema_version=1, document=policy.model_dump(mode="json")))
            await session.flush()
            project.current_policy_version = 1
        await session.commit()
    extra = {"direct_pr_poll_seconds": 0, "direct_pr_quiet_seconds": 0, "direct_pr_watch_timeout_seconds": 30}
    extra.update(settings_extra or {})
    settings = Settings(data_root=tmp_path / "data", subscription_installations_path=None, **extra)
    service = DirectRunService(session_factory, settings, installations=(SimpleNamespace(client="muse_code", model="model-1", effort="high"), SimpleNamespace(client="codex_app_server", model="review-model", effort=None)))
    run = await service.create(project_id=pid, task="do it", client="muse_code", model="model-1", effort=None)
    fake_cli = tmp_path / "fake_cli.py"
    fake_cli.write_text(FAKE_CLI)

    def factory(installation, ctx):
        prompt_file = tmp_path / f"prompt-{ctx.session_id}.txt"
        prompt_file.write_text(ctx.prompt, encoding="utf-8")
        return DirectLaunch(argv=(sys.executable, str(fake_cli), str(prompt_file), mode), stdin_data=b"")

    worker = DirectRunWorker(session_factory, settings, argv_factory=factory,
                             installation_resolver=lambda run: SimpleNamespace(client=run.client, executable=sys.executable, model=run.model, effort=run.effort),
                             review_resolver=lambda run, impl: SimpleNamespace(client="codex_app_server", executable=sys.executable, model="review-model", effort=None),
                             publisher=publisher)
    return run, service, worker


@pytest.mark.integration
async def test_quiet_done(session_factory, tmp_path):
    pub = FakePublisher([snap()])
    run, service, worker = await _setup(session_factory, tmp_path, publisher=pub)
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    assert done.pr_url == "https://example.test/pr/42" and done.pr_number == 42
    assert pub.replies == [] and pub.resolves == [] and pub.closes == []
    assert len(pub.pushes) == 1 and len(pub.creates) == 1


@pytest.mark.integration
async def test_thread_repair_push_reply_resolve_quiet_done(session_factory, tmp_path):
    pub = FakePublisher([snap(threads=[thread("t1")]), snap()])
    run, service, worker = await _setup(session_factory, tmp_path, publisher=pub)
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE" and done.pr_rounds == 1
    assert [r[0] for r in pub.replies] == ["t1"]
    assert pub.resolves == ["t1"]
    assert len(pub.pushes) == 2


@pytest.mark.integration
async def test_always_new_threads_rounds1_failed(session_factory, tmp_path):
    pub = FakePublisher(dynamic_threads=True)
    run, service, worker = await _setup(session_factory, tmp_path, publisher=pub, settings_extra={"direct_max_pr_rounds": 1})
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "FAILED"
    assert "pr comments still open after 1 rounds" in (done.failure_reason or "")
    assert done.pr_url == "https://example.test/pr/42" and done.pr_rounds == 1


@pytest.mark.integration
async def test_push_failure_failed_before_pr(session_factory, tmp_path):
    pub = FakePublisher(push_fails=True)
    run, service, worker = await _setup(session_factory, tmp_path, publisher=pub)
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "FAILED" and done.pr_url is None
    assert pub.creates == []


@pytest.mark.integration
async def test_checks_fail_after_pr_repair_failed(session_factory, tmp_path):
    pub = FakePublisher([snap(threads=[thread("t1")]), snap()])
    run, service, worker = await _setup(session_factory, tmp_path, mode="pr_repair_breaks_checks", publisher=pub, with_policy=True)
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "FAILED"
    assert done.failure_reason == "checks failing after pr repair"
    assert pub.replies == [] and pub.resolves == []


@pytest.mark.integration
async def test_no_github_repo_done_skipped_publisher_untouched(session_factory, tmp_path):
    pub = FakePublisher()
    run, service, worker = await _setup(session_factory, tmp_path, publisher=pub, github="")
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    assert "publication skipped: no GitHub repository" in (done.summary or "")
    assert pub.pushes == [] and pub.creates == [] and pub.polls == 0


@pytest.mark.integration
async def test_cancel_during_watch_cancelled_pr_left_open(session_factory, tmp_path):
    pub = FakePublisher([snap(checks="PENDING", activity=False, created=datetime.now(UTC), last=None)])
    run, service, worker = await _setup(session_factory, tmp_path, publisher=pub, settings_extra={"direct_pr_poll_seconds": 1, "direct_pr_quiet_seconds": 180})
    task = asyncio.create_task(worker.run_once())
    for _ in range(100):
        current = await service.get(run.id)
        if current.state == "PR_OPEN":
            break
        await asyncio.sleep(0.1)
    await service.cancel(run.id)
    assert await asyncio.wait_for(task, timeout=30) == run.id
    done = await service.get(run.id)
    assert done.state == "CANCELLED"
    assert pub.closes == []


@pytest.mark.integration
async def test_gh_poll_failures_x3_failed(session_factory, tmp_path):
    pub = FakePublisher(poll_fails=True)
    run, service, worker = await _setup(session_factory, tmp_path, publisher=pub)
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "FAILED" and done.failure_reason == "pr watch failed"
    assert done.pr_url == "https://example.test/pr/42"


def _watch_log(tmp_path, run_id):
    path = tmp_path / "data" / "direct-transcripts" / str(run_id) / "pr-watch.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.mark.integration
async def test_untrusted_thread_no_repair_and_done(session_factory, tmp_path):
    pub = FakePublisher([snap(threads=[thread("t1", author="stranger")])])
    run, service, worker = await _setup(session_factory, tmp_path, publisher=pub)
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    assert done.pr_rounds == 0
    assert pub.replies == [] and pub.resolves == []
    assert len(pub.pushes) == 1
    assert "ignored untrusted pr feedback from stranger" in _watch_log(tmp_path, run.id)


@pytest.mark.integration
async def test_untrusted_thread_logged_once_not_per_poll(session_factory, tmp_path):
    pub = FakePublisher([
        snap(threads=[thread("t1", author="stranger")], checks="PENDING"),
        snap(threads=[thread("t1", author="stranger")], checks="PENDING"),
        snap(),
    ])
    run, service, worker = await _setup(session_factory, tmp_path, publisher=pub)
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    assert _watch_log(tmp_path, run.id).count("ignored untrusted pr feedback from stranger") == 1


@pytest.mark.integration
@pytest.mark.parametrize("author", ["o", "CHATGPT-Codex-Connector", "dependabot[bot]"])
async def test_trusted_thread_authors_trigger_repair(session_factory, tmp_path, author):
    pub = FakePublisher([snap(threads=[thread("t1", author=author)]), snap()])
    run, service, worker = await _setup(session_factory, tmp_path, publisher=pub)
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE" and done.pr_rounds == 1
    assert [r[0] for r in pub.replies] == ["t1"]
    assert pub.resolves == ["t1"]


@pytest.mark.integration
async def test_trusted_configured_author_triggers_repair(session_factory, tmp_path):
    pub = FakePublisher([snap(threads=[thread("t1", author="Alice")]), snap()])
    run, service, worker = await _setup(
        session_factory, tmp_path, publisher=pub,
        settings_extra={"direct_trusted_pr_authors": "bob, alice"},
    )
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE" and done.pr_rounds == 1


@pytest.mark.integration
async def test_empty_author_thread_is_untrusted(session_factory, tmp_path):
    pub = FakePublisher([snap(threads=[thread("t1", author="")])])
    run, service, worker = await _setup(session_factory, tmp_path, publisher=pub)
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE" and done.pr_rounds == 0
    assert pub.replies == [] and pub.resolves == []


@pytest.mark.integration
async def test_untrusted_changes_requested_does_not_block_quiet(session_factory, tmp_path):
    future = datetime.now(UTC) + timedelta(hours=1)
    pub = FakePublisher([snap(changes=[changes_requested("r1", author="stranger", updated=future)])])
    run, service, worker = await _setup(session_factory, tmp_path, publisher=pub)
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE"
    assert done.pr_rounds == 0
    assert pub.replies == [] and pub.resolves == []
    assert "ignored untrusted pr feedback from stranger" in _watch_log(tmp_path, run.id)


@pytest.mark.integration
async def test_trusted_changes_requested_triggers_repair(session_factory, tmp_path):
    future = datetime.now(UTC) + timedelta(hours=1)
    pub = FakePublisher([
        snap(changes=[changes_requested("r1", updated=future)]),
        snap(),
    ])
    run, service, worker = await _setup(session_factory, tmp_path, publisher=pub)
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "DONE" and done.pr_rounds == 1


@pytest.mark.integration
async def test_checks_pending_timeout_failed_with_pr_url(session_factory, tmp_path):
    pub = FakePublisher([snap(checks="PENDING", activity=False, created=datetime.now(UTC), last=None)])
    run, service, worker = await _setup(session_factory, tmp_path, publisher=pub, settings_extra={"direct_pr_watch_timeout_seconds": 1, "direct_pr_quiet_seconds": 180})
    await worker.run_once()
    done = await service.get(run.id)
    assert done.state == "FAILED" and done.failure_reason == "pr watch timed out"
    assert done.pr_url == "https://example.test/pr/42"
