from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.jev import JevResult
from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.worktrees import (
    GitCandidateDiff,
    GitDiff,
    GitSnapshotFile,
    GitWorkingTreeSnapshot,
    ManagedWorktree,
)
from forge.application.services.jev_review import (
    REVIEW_QUESTIONS,
    review_focus_scores,
    safe_diff_hunks,
)
from forge.application.services.review import ReviewService
from forge.application.services.subscription_requests import SubscriptionRequestBuilder
from forge.domain.policy import JevPolicy, ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.domain.subscription import SpecialistPurpose
from forge.observability.redaction import Redactor
from forge.tools.repository import RepositoryReader


def test_review_hunks_never_egress_excluded_paths(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("new = True\n", encoding="utf-8")
    (tmp_path / "private.txt").write_text("secret\n", encoding="utf-8")
    diff = (
        "diff --git a/code.py b/code.py\n@@ -1 +1 @@\n-old\n+new = True\n"
        "diff --git a/private.txt b/private.txt\n@@ -1 +1 @@\n-old\n+secret\n"
    )
    hunks, complete = safe_diff_hunks(
        RepositoryReader(tmp_path, secret_paths=("private.txt",)),
        diff,
        changed_paths=("code.py", "private.txt"),
    )
    assert complete is False
    assert len(hunks) == 1
    assert hunks[0]["path"] == "code.py"
    assert "secret" not in repr(hunks)


def test_review_questions_and_scores_survive_context_redaction() -> None:
    assert len(REVIEW_QUESTIONS) == 5
    assert all(isinstance(value, dict) for value in Redactor().redact(REVIEW_QUESTIONS).values())
    answers = {key: {"score": 2, "confidence": 0.8} for key in REVIEW_QUESTIONS}
    scores = Redactor().redact(review_focus_scores(answers))
    assert len(scores) == 5
    assert all(isinstance(item["score"], dict) for item in scores)
    assert {item["topic"] for item in scores} == {
        "credentials", "authorization", "concurrency", "compatibility", "data_integrity"
    }


def _case(tmp_path, mode):
    run_id, project_id = uuid4(), uuid4()
    run = RunSnapshot(id=run_id, project_id=project_id, task_id=uuid4(),
                      state=RunState.IMPLEMENTING, policy_version=1,
                      branch_name="forge/jev-test", worktree_path=str(tmp_path), base_sha="a" * 40)
    policy = ProjectPolicy(id=project_id, version=1, repository_path=str(tmp_path),
                           github_repository="test/repository", default_branch="main",
                           jev=JevPolicy(mode=mode, allow_remote=True), secret_paths=("private.txt",))
    tree = ManagedWorktree(identity=WorktreeIdentity.for_run(project_id, run_id, run.branch_name, False),
                           path=tmp_path, base_sha=run.base_sha)
    (tmp_path / "code.py").write_text("new = True\n", encoding="utf-8")
    (tmp_path / "private.txt").write_text("excluded_secret\n", encoding="utf-8")
    reader = RepositoryReader(tmp_path, secret_paths=policy.secret_paths)
    return run, policy, tree, reader


class _Jev:
    def __init__(self, mutate=lambda: None, work=None, status="succeeded"):
        self.requests = []
        self.mutate, self.work = mutate, work
        self.status = status

    async def evaluate(self, request, *, policy):
        assert self.work is None or not self.work.active
        assert "excluded_secret" not in repr(request.state)
        self.requests.append(request)
        self.mutate()
        answers = {key: {"score": 1.0, "confidence": 0.8} for key in request.questions}
        return JevResult(status=self.status, answers=answers if self.status == "succeeded" else {})


def _candidate(tmp_path):
    text = "".join(f"diff --git a/{name} b/{name}\n@@ -1 +1 @@\n-old\n+{(tmp_path / name).read_text(encoding='utf-8')}"
                   for name in ("code.py", "private.txt"))
    return GitCandidateDiff(head_sha="b" * 40,
                            diff=GitDiff(text=text, original_byte_count=len(text.encode()), truncated=False),
                            changed_paths=("code.py", "private.txt"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["on", "shadow"])
@pytest.mark.parametrize("change_source", [False, True])
async def test_api_review_focus_is_advisory_mode_and_candidate_bound(tmp_path, mode, change_source):
    run, policy, tree, reader = _case(tmp_path, mode)
    candidate = _candidate(tmp_path)
    mutate = (lambda: (tmp_path / "code.py").write_text("later = True\n", encoding="utf-8")) if change_source else lambda: None
    jev = _Jev(mutate)
    service = ReviewService(None, None, None, None, None, lambda *_: reader, jev_service=jev)
    git = SimpleNamespace(candidate_diff=lambda _: _candidate(tmp_path))

    focus, status = await service._review_focus(SimpleNamespace(run=run, policy=policy), tree, candidate, git, jev=policy.jev)

    assert len(jev.requests) == 1
    if mode == "shadow":
        assert focus is None and status is None
    elif change_source:
        assert focus is None and status == "stale_candidate"
    else:
        assert focus is not None and status == "succeeded"
        payload = Redactor().redact(json.loads(focus.content))
        assert payload["advisory_only"] is True
        assert payload["coverage_complete"] is False
        assert len(payload["scores"]) == 5
        assert "never" in payload["guidance"] and "absence" in payload["guidance"]


@pytest.mark.parametrize("mode", ["on", "shadow"])
@pytest.mark.parametrize("outcome", ["missing_provider", "no_sources", "unknown"])
async def test_api_review_focus_fallback_preserves_shadow_and_never_attaches_unknown_scores(
    tmp_path, mode, outcome
):
    run, policy, tree, reader = _case(tmp_path, mode)
    candidate = _candidate(tmp_path)
    if outcome == "no_sources":
        reader = RepositoryReader(tmp_path, secret_paths=("code.py", "private.txt"))
    jev = None if outcome == "missing_provider" else _Jev(status="unknown")
    service = ReviewService(None, None, None, None, None, lambda *_: reader, jev_service=jev)
    git = SimpleNamespace(candidate_diff=lambda _: candidate)

    focus, status = await service._review_focus(SimpleNamespace(run=run, policy=policy), tree, candidate, git, jev=policy.jev)

    assert focus is None
    expected = {"missing_provider": "unavailable", "no_sources": "no_eligible_hunks", "unknown": "unknown"}
    assert status == (expected[outcome] if mode == "on" else None)


@dataclass(frozen=True)
class _Request:
    task: object
    untrusted_context: dict


class _Work:
    active = False

    def __init__(self, run, selection, *, revoked=False):
        self.runs = SimpleNamespace(get_for_update=AsyncMock(return_value=run))
        self.subscription_decisions = SimpleNamespace(review_selection_context=AsyncMock(return_value=selection))
        self.subscription_execution = SimpleNamespace(invocation_context=AsyncMock(
            side_effect=ValueError("lease revoked") if revoked else None))
        self.rollback = AsyncMock()

    async def __aenter__(self):
        self.active = True
        return self

    async def __aexit__(self, *args):
        self.active = False


def _snapshot(tmp_path):
    files = []
    for name in ("code.py", "private.txt"):
        wire = (tmp_path / name).read_bytes()
        files.append(GitSnapshotFile(path=name, mode="100644", content_digest=sha256(wire).hexdigest(), byte_count=len(wire)))
    return GitWorkingTreeSnapshot(head_sha="b" * 40, base_sha="a" * 40, files=tuple(files),
                                   changed_paths=("code.py", "private.txt"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["on", "shadow"])
@pytest.mark.parametrize(
    "change", ["none", "source", "selection", "lease", "source_and_lease", "unknown"]
)
async def test_subscription_review_focus_rechecks_candidate_and_authority(tmp_path, mode, change):
    run, policy, _, reader = _case(tmp_path, mode)
    snapshot = _snapshot(tmp_path)
    selection = {"candidate_epoch": 3, "observation": CandidateInspection.from_snapshot(snapshot).payload()}
    work = _Work(run, selection, revoked=change in {"lease", "source_and_lease"})
    request = _Request(SimpleNamespace(purpose=SpecialistPurpose.INDEPENDENT_REVIEW, task_id=uuid4()),
                       {"review_selection": selection})

    def mutate():
        if change in {"source", "source_and_lease"}:
            (tmp_path / "code.py").write_text("later = True\n", encoding="utf-8")
        elif change == "selection":
            work.subscription_decisions.review_selection_context.return_value = {**selection, "candidate_epoch": 4}

    jev = _Jev(mutate, work, status="unknown" if change == "unknown" else "succeeded")
    builder = SubscriptionRequestBuilder(lambda: work, jev_service=jev,
                                         snapshot=lambda *_: _snapshot(tmp_path), reader=lambda *_: reader)
    if change in {"lease", "source_and_lease"}:
        with pytest.raises(ValueError, match="lease revoked"):
            await builder._with_review_focus(request, run, policy, selection, SimpleNamespace(candidate_epoch=3), jev=policy.jev)
        assert len(jev.requests) == 1
        return
    result = await builder._with_review_focus(request, run, policy, selection, SimpleNamespace(candidate_epoch=3), jev=policy.jev)

    assert len(jev.requests) == 1
    assert request.untrusted_context == {"review_selection": selection}
    if mode == "shadow":
        assert result is request
    elif change == "none":
        focus = Redactor().redact(result.untrusted_context["review_focus"])
        assert focus["advisory_only"] is True
        assert focus["observation"] == selection["observation"]
        assert focus["coverage_complete"] is False
        assert len(focus["scores"]) == 5
    else:
        assert "scores" not in result.untrusted_context["review_focus"]
        assert result.untrusted_context["review_focus"]["status"] != "succeeded"
