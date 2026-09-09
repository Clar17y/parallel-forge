"""Deterministic loopback fake GitHub service with RPC ports for read and write.

Retains fake external GitHub state across worker process restarts, without live credentials
or live network calls.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import socket
import subprocess
import threading
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from forge.domain.github import (
    CheckSnapshot,
    GitHubIssue,
    MergeProtection,
    PullRequestSnapshot,
    ReviewSnapshot,
)
from forge.domain.operation import canonical_digest
from forge.domain.release import GitHubPullRequest
from forge.release.github_write import GitHubWriteError

_GIT_IDENTITY_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Forge fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Forge fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class FakeGitHubState:
    def __init__(self, bare_remote_path: Path | None = None) -> None:
        self.bare_remote_path = bare_remote_path
        self.issues: dict[tuple[str, int], GitHubIssue] = {}
        self.pull_requests: dict[tuple[str, int], GitHubPullRequest] = {}
        self.read_pull_requests: dict[tuple[str, int], PullRequestSnapshot] = {}
        self.checks: dict[tuple[str, str], list[CheckSnapshot]] = {}
        self.reviews: dict[tuple[str, int], list[ReviewSnapshot]] = {}
        self.bases: dict[tuple[str, str], str] = {}
        self.merge_protections: dict[tuple[str, str], MergeProtection] = {}
        self.branch_shas: dict[tuple[str, str], str] = {}
        self.effect_counts: dict[str, int] = {
            "pushes": 0,
            "prs_created": 0,
            "branch_updates": 0,
            "merge_attempts": 0,
            "merge_conflicts": 0,
            "merges": 0,
        }
        self.conflict_on_merge: bool = False
        self._next_number: int = 1
        self._lock = threading.Lock()

    def create_pr(
        self,
        repository: str,
        head_repository: str,
        head_ref: str,
        base_ref: str,
        title: str,
        body: str | None,
    ) -> GitHubPullRequest:
        with self._lock:
            repo_key = repository.casefold()
            head_repo_key = head_repository.casefold()
            number = self._next_number
            self._next_number += 1
            head_sha = self.branch_shas.get((head_repo_key, head_ref), "0" * 40)
            base_sha = self.branch_shas.get(
                (repo_key, base_ref), self.bases.get((repo_key, base_ref), "0" * 40)
            )
            pr = GitHubPullRequest(
                number=number,
                node_id=f"PR_{number}",
                url=f"https://github.com/{repository}/pull/{number}",
                head_repository=head_repository,
                head_ref=head_ref,
                head_sha=head_sha,
                base_repository=repository,
                base_ref=base_ref,
                base_sha=base_sha,
                state="open",
                merged=False,
                merge_sha=None,
            )
            self.pull_requests[(repo_key, number)] = pr
            self.read_pull_requests[(repo_key, number)] = PullRequestSnapshot(
                number=number,
                state="open",
                head_sha=head_sha,
                head_ref=head_ref,
                base_sha=base_sha,
                base_ref=base_ref,
                draft=False,
                mergeable=True,
            )
            self.effect_counts["prs_created"] += 1
            return pr

    def update_pr_branch(
        self, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> GitHubPullRequest:
        with self._lock:
            repo_key = repository.casefold()
            key = (repo_key, pull_request_number)
            if key not in self.pull_requests:
                raise KeyError("pull request not found")
            pr = self.pull_requests[key]
            if pr.head_sha != expected_head_sha:
                raise ValueError("stale")
            new_base_sha = self.bases.get((repo_key, pr.base_ref), pr.base_sha)
            new_head_sha = self.branch_shas.get(
                (pr.head_repository.casefold(), pr.head_ref), pr.head_sha
            )
            if (
                new_base_sha != pr.base_sha
                and self.bare_remote_path is not None
                and self.bare_remote_path.exists()
            ):
                # Synthesize a real merge commit in the bare remote with both parents
                tree_res = subprocess.run(
                    ["git", "-C", str(self.bare_remote_path), "rev-parse", f"{pr.head_sha}^{{tree}}"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                tree_sha = tree_res.stdout.strip()
                commit_res = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.bare_remote_path),
                        "commit-tree",
                        tree_sha,
                        "-p",
                        pr.head_sha,
                        "-p",
                        new_base_sha,
                        "-m",
                        f"Merge branch '{pr.base_ref}' into {pr.head_ref}",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    env=_GIT_IDENTITY_ENV,
                )
                new_head_sha = commit_res.stdout.strip()
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.bare_remote_path),
                        "update-ref",
                        f"refs/heads/{pr.head_ref}",
                        new_head_sha,
                    ],
                    check=True,
                )
                self.branch_shas[(pr.head_repository.casefold(), pr.head_ref)] = new_head_sha
            updated = replace(pr, head_sha=new_head_sha, base_sha=new_base_sha)
            self.pull_requests[key] = updated
            if key in self.read_pull_requests:
                self.read_pull_requests[key] = replace(
                    self.read_pull_requests[key], head_sha=new_head_sha, base_sha=new_base_sha
                )
            self.effect_counts["branch_updates"] += 1
            return updated

    def merge_pr(
        self,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
        merge_method: str,
    ) -> GitHubPullRequest:
        with self._lock:
            self.effect_counts["merge_attempts"] += 1
            if self.conflict_on_merge:
                self.effect_counts["merge_conflicts"] += 1
                raise ValueError("stale")
            repo_key = repository.casefold()
            key = (repo_key, pull_request_number)
            if key not in self.pull_requests:
                raise KeyError("pull request not found")
            pr = self.pull_requests[key]
            if pr.head_sha != expected_head_sha or pr.state != "open":
                self.effect_counts["merge_conflicts"] += 1
                raise ValueError("stale")
            merge_sha = canonical_digest(
                {
                    "repository": repository,
                    "number": pr.number,
                    "head": pr.head_sha,
                    "method": merge_method,
                }
            )[:40]
            updated = replace(pr, state="closed", merged=True, merge_sha=merge_sha)
            self.pull_requests[key] = updated
            if key in self.read_pull_requests:
                self.read_pull_requests[key] = replace(
                    self.read_pull_requests[key], state="closed"
                )
            self.effect_counts["merges"] += 1
            return updated

    def update_branch_sha(self, repository: str, branch: str, sha: str) -> None:
        with self._lock:
            repo_key = repository.casefold()
            self.branch_shas[(repo_key, branch)] = sha
            for key, pr in list(self.pull_requests.items()):
                if (key[0] == repo_key or pr.head_repository.casefold() == repo_key) and pr.head_ref == branch:
                    self.pull_requests[key] = replace(pr, head_sha=sha)
                    if key in self.read_pull_requests:
                        self.read_pull_requests[key] = replace(
                            self.read_pull_requests[key], head_sha=sha
                        )
            self.effect_counts["pushes"] += 1


class FakeGitHubRequestHandler(http.server.BaseHTTPRequestHandler):
    state: FakeGitHubState

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress default noisy stderr logging

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
        path = parsed.path

        try:
            if path == "/rpc/get_issue":
                repo = body["repository"].casefold()
                num = body["issue_number"]
                issue = self.state.issues.get((repo, num))
                if issue is None:
                    self._send_json(404, {"error": "not_found"})
                else:
                    self._send_json(200, asdict(issue))

            elif path == "/rpc/get_pull_request":
                repo = body["repository"].casefold()
                num = body["pull_number"]
                pr_read = self.state.read_pull_requests.get((repo, num))
                if pr_read is None:
                    self._send_json(404, {"error": "not_found"})
                else:
                    self._send_json(200, asdict(pr_read))

            elif path == "/rpc/get_checks":
                repo = body["repository"].casefold()
                ref = body["ref"]
                checks = [asdict(c) for c in self.state.checks.get((repo, ref), [])]
                self._send_json(200, {"checks": checks})

            elif path == "/rpc/get_reviews":
                repo = body["repository"].casefold()
                num = body["pull_number"]
                reviews = []
                for r in self.state.reviews.get((repo, num), []):
                    item = asdict(r)
                    if item.get("submitted_at") is not None:
                        item["submitted_at"] = r.submitted_at.isoformat() if r.submitted_at else None
                    reviews.append(item)
                self._send_json(200, {"reviews": reviews})

            elif path == "/rpc/get_base":
                repo = body["repository"].casefold()
                branch = body["branch"]
                base_sha = self.state.bases.get((repo, branch))
                if base_sha is None:
                    self._send_json(404, {"error": "not_found"})
                else:
                    self._send_json(200, {"sha": base_sha})

            elif path == "/rpc/get_merge_protection":
                repo = body["repository"].casefold()
                branch = body["branch"]
                protection = self.state.merge_protections.get(
                    (repo, branch),
                    MergeProtection(False, False, False, "unverified", verified=False),
                )
                self._send_json(200, asdict(protection))

            elif path == "/rpc/find_pull_requests":
                repo = body["repository"].casefold()
                head_repo = body["head_repository"].casefold()
                head_ref = body["head_ref"]
                base_ref = body["base_ref"]
                matches = [
                    asdict(p)
                    for (r, _), p in self.state.pull_requests.items()
                    if r == repo
                    and p.head_repository.casefold() == head_repo
                    and p.head_ref == head_ref
                    and p.base_ref == base_ref
                ]
                self._send_json(200, {"pull_requests": matches})

            elif path == "/rpc/create_pull_request":
                pr = self.state.create_pr(
                    body["repository"],
                    body["head_repository"],
                    body["head_ref"],
                    body["base_ref"],
                    body.get("title", "Acceptance PR"),
                    body.get("body"),
                )
                self._send_json(201, asdict(pr))

            elif path == "/rpc/write_get_pull_request":
                repo = body["repository"].casefold()
                num = body["pull_request_number"]
                pr = self.state.pull_requests.get((repo, num))
                if pr is None:
                    self._send_json(404, {"error": "not_found"})
                else:
                    self._send_json(200, asdict(pr))

            elif path == "/rpc/get_branch_sha":
                repo = body["repository"].casefold()
                ref = body["ref"]
                sha = self.state.branch_shas.get((repo, ref))
                if sha is None:
                    self._send_json(404, {"error": "not_found"})
                else:
                    self._send_json(200, {"sha": sha})

            elif path == "/rpc/update_branch":
                try:
                    updated = self.state.update_pr_branch(
                        body["repository"],
                        body["pull_request_number"],
                        body["expected_head_sha"],
                    )
                    self._send_json(200, asdict(updated))
                except ValueError as err:
                    self._send_json(409, {"error": str(err)})

            elif path == "/rpc/merge_pull_request":
                try:
                    merged = self.state.merge_pr(
                        body["repository"],
                        body["pull_request_number"],
                        body["expected_head_sha"],
                        body["merge_method"],
                    )
                    self._send_json(200, asdict(merged))
                except ValueError as err:
                    self._send_json(409, {"error": str(err)})

            # Control endpoints
            elif path == "/control/set_check":
                repo = body["repository"].casefold()
                ref = body["ref"]
                chk = CheckSnapshot(
                    name=body["name"],
                    status=body.get("status", "completed"),
                    conclusion=body.get("conclusion", "success"),
                    details_url=body.get("details_url"),
                    head_sha=ref,
                    summary=body.get("summary", "Check passed"),
                )
                self.state.checks.setdefault((repo, ref), []).append(chk)
                self._send_json(200, {"status": "ok"})

            elif path == "/control/set_review":
                repo = body["repository"].casefold()
                num = body["pull_number"]
                rev = ReviewSnapshot(
                    reviewer=body.get("reviewer", "reviewer"),
                    state=body.get("state", "APPROVED"),
                    submitted_at=datetime.now(UTC),
                    requested_changes=body.get("requested_changes", False),
                    comment_count=body.get("comment_count", 0),
                    body=body.get("body", "Looks good"),
                    feedback=tuple(body.get("feedback", ())),
                )
                self.state.reviews.setdefault((repo, num), []).append(rev)
                self._send_json(200, {"status": "ok"})

            elif path == "/control/set_base":
                repo = body["repository"].casefold()
                branch = body["branch"]
                self.state.bases[(repo, branch)] = body["sha"]
                self._send_json(200, {"status": "ok"})

            elif path == "/control/set_merge_protection":
                repo = body["repository"].casefold()
                branch = body["branch"]
                prot = MergeProtection(
                    strict_required_checks=body.get("strict_required_checks", True),
                    merge_queue_enabled=body.get("merge_queue_enabled", False),
                    actor_can_bypass=body.get("actor_can_bypass", False),
                    evidence_source=body.get("evidence_source", "classic"),
                    verified=True,
                    required_check_names=tuple(body.get("required_check_names", ("ci",))),
                )
                self.state.merge_protections[(repo, branch)] = prot
                self._send_json(200, {"status": "ok"})

            elif path == "/control/set_branch_sha":
                self.state.update_branch_sha(
                    body["repository"],
                    body["branch"],
                    body["sha"],
                )
                self._send_json(200, {"status": "ok"})

            elif path == "/control/set_conflict_on_merge":
                self.state.conflict_on_merge = bool(body.get("conflict", True))
                self._send_json(200, {"status": "ok", "conflict": self.state.conflict_on_merge})

            elif path == "/control/get_effects":
                self._send_json(200, self.state.effect_counts)

            else:
                self._send_json(404, {"error": "endpoint not found"})
        except Exception as exc:  # noqa: BLE001 - fixture endpoint reports bounded failure
            self._send_json(500, {"error": str(exc)})

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "role": "fake_github"})
        else:
            self._send_json(404, {"error": "not found"})

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        content = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class FakeGitHubServer:
    """Loopback HTTP service providing deterministic GitHub RPC ports and scenario controls."""

    def __init__(self, bare_remote_path: Path | None = None) -> None:
        self.port = _free_loopback_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.state = FakeGitHubState(bare_remote_path=bare_remote_path)
        handler_cls = type(
            "BoundFakeGitHubRequestHandler",
            (FakeGitHubRequestHandler,),
            {"state": self.state},
        )
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()
        # Verify readiness
        for _ in range(50):
            try:
                res = httpx.get(f"{self.url}/health", timeout=0.2)
                if res.status_code == 200:
                    return
            except Exception:  # noqa: BLE001, S110 - bounded readiness probe
                pass
            import time

            time.sleep(0.02)
        raise RuntimeError("FakeGitHubServer failed to start")

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# Client adapters implementing GitHubPort and GitHubWritePort over HTTP RPC


class FakeGitHubHttpClient:
    """Test-owned client implementing GitHubPort over loopback RPC."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    async def get_issue(self, repository: str, issue_number: int) -> GitHubIssue:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            res = await client.post(
                "/rpc/get_issue", json={"repository": repository, "issue_number": issue_number}
            )
            if res.status_code == 404:
                raise KeyError(f"issue {issue_number} not found in {repository}")
            res.raise_for_status()
            data = res.json()
            updated_at = (
                datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None
            )
            return GitHubIssue(
                number=data["number"],
                title=data["title"],
                body=data.get("body"),
                source_url=data["source_url"],
                updated_at=updated_at,
                state=data["state"],
            )

    async def get_pull_request(self, repository: str, pull_number: int) -> PullRequestSnapshot:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            res = await client.post(
                "/rpc/get_pull_request", json={"repository": repository, "pull_number": pull_number}
            )
            if res.status_code == 404:
                raise KeyError(f"pull request {pull_number} not found in {repository}")
            res.raise_for_status()
            data = res.json()
            return PullRequestSnapshot(
                number=data["number"],
                state=data["state"],
                head_sha=data["head_sha"],
                head_ref=data["head_ref"],
                base_sha=data["base_sha"],
                base_ref=data["base_ref"],
                draft=data["draft"],
                mergeable=data.get("mergeable"),
            )

    async def get_checks(self, repository: str, ref: str) -> tuple[CheckSnapshot, ...]:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            res = await client.post("/rpc/get_checks", json={"repository": repository, "ref": ref})
            res.raise_for_status()
            items = res.json().get("checks", [])
            return tuple(
                CheckSnapshot(
                    name=item["name"],
                    status=item["status"],
                    conclusion=item.get("conclusion"),
                    details_url=item.get("details_url"),
                    head_sha=item.get("head_sha"),
                    summary=item.get("summary"),
                    text=item.get("text"),
                )
                for item in items
            )

    async def get_reviews(
        self, repository: str, pull_number: int
    ) -> tuple[ReviewSnapshot, ...]:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            res = await client.post(
                "/rpc/get_reviews", json={"repository": repository, "pull_number": pull_number}
            )
            res.raise_for_status()
            items = res.json().get("reviews", [])
            return tuple(
                ReviewSnapshot(
                    reviewer=item["reviewer"],
                    state=item["state"],
                    submitted_at=(
                        datetime.fromisoformat(item["submitted_at"])
                        if item.get("submitted_at")
                        else None
                    ),
                    requested_changes=item.get("requested_changes", False),
                    unresolved_threads=item.get("unresolved_threads", 0),
                    comment_count=item.get("comment_count", 0),
                    body=item.get("body"),
                    feedback=tuple(item.get("feedback", ())),
                )
                for item in items
            )

    async def get_base(self, repository: str, branch: str) -> str:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            res = await client.post(
                "/rpc/get_base", json={"repository": repository, "branch": branch}
            )
            if res.status_code == 404:
                raise KeyError(f"base branch {branch} not found in {repository}")
            res.raise_for_status()
            return str(res.json()["sha"])

    async def get_merge_protection(self, repository: str, branch: str) -> MergeProtection:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            res = await client.post(
                "/rpc/get_merge_protection", json={"repository": repository, "branch": branch}
            )
            res.raise_for_status()
            data = res.json()
            return MergeProtection(
                strict_required_checks=data["strict_required_checks"],
                merge_queue_enabled=data["merge_queue_enabled"],
                actor_can_bypass=data["actor_can_bypass"],
                evidence_source=data["evidence_source"],
                verified=data.get("verified", True),
                required_check_names=tuple(data.get("required_check_names", ())),
                merge_queue_method=data.get("merge_queue_method"),
            )


class FakeGitHubHttpWriteClient:
    """Test-owned client implementing GitHubWritePort over loopback RPC."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    async def find_pull_requests(
        self, repository: str, head_repository: str, head_ref: str, base_ref: str
    ) -> tuple[GitHubPullRequest, ...]:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            res = await client.post(
                "/rpc/find_pull_requests",
                json={
                    "repository": repository,
                    "head_repository": head_repository,
                    "head_ref": head_ref,
                    "base_ref": base_ref,
                },
            )
            res.raise_for_status()
            return tuple(GitHubPullRequest(**p) for p in res.json().get("pull_requests", []))

    async def create_pull_request(
        self,
        repository: str,
        head_repository: str,
        head_ref: str,
        base_ref: str,
        title: str,
        body: str | None,
    ) -> GitHubPullRequest:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            res = await client.post(
                "/rpc/create_pull_request",
                json={
                    "repository": repository,
                    "head_repository": head_repository,
                    "head_ref": head_ref,
                    "base_ref": base_ref,
                    "title": title,
                    "body": body,
                },
            )
            res.raise_for_status()
            return GitHubPullRequest(**res.json())

    async def get_pull_request(
        self, repository: str, pull_request_number: int
    ) -> GitHubPullRequest:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            res = await client.post(
                "/rpc/write_get_pull_request",
                json={"repository": repository, "pull_request_number": pull_request_number},
            )
            if res.status_code == 404:
                raise KeyError(f"pull request {pull_request_number} not found")
            res.raise_for_status()
            return GitHubPullRequest(**res.json())

    async def get_branch_sha(self, repository: str, ref: str) -> str:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            res = await client.post(
                "/rpc/get_branch_sha", json={"repository": repository, "ref": ref}
            )
            if res.status_code == 404:
                raise KeyError(f"branch {ref} not found")
            res.raise_for_status()
            return str(res.json()["sha"])

    async def update_branch(
        self, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> GitHubPullRequest:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            res = await client.post(
                "/rpc/update_branch",
                json={
                    "repository": repository,
                    "pull_request_number": pull_request_number,
                    "expected_head_sha": expected_head_sha,
                },
            )
            if res.status_code == 409:
                raise GitHubWriteError(res.json().get("error", "stale"))
            res.raise_for_status()
            return GitHubPullRequest(**res.json())

    async def merge_pull_request(
        self,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
        merge_method: str,
    ) -> GitHubPullRequest:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            res = await client.post(
                "/rpc/merge_pull_request",
                json={
                    "repository": repository,
                    "pull_request_number": pull_request_number,
                    "expected_head_sha": expected_head_sha,
                    "merge_method": merge_method,
                },
            )
            if res.status_code == 409:
                raise GitHubWriteError(res.json().get("error", "stale"))
            if res.status_code >= 500:
                raise GitHubWriteError(res.json().get("error", "merge service failure"))
            res.raise_for_status()
            return GitHubPullRequest(**res.json())


class LocalBarePush:
    """Fixture adapter executing real git push against a local bare remote."""

    def __init__(self, bare_remote_path: Path, fake_github_url: str) -> None:
        self._bare_remote = bare_remote_path
        self._fake_github_url = fake_github_url

    async def push(self, worktree: Any, policy: Any, approved_sha: str) -> None:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "git",
                "-C",
                str(worktree.path),
                "push",
                str(self._bare_remote),
                f"{approved_sha}:refs/heads/{worktree.identity.branch}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Push to bare remote failed: {result.stderr}")
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self._fake_github_url}/control/set_branch_sha",
                json={
                    "repository": policy.github_repository,
                    "branch": worktree.identity.branch,
                    "sha": approved_sha,
                },
            )


class LocalBareAdoption:
    """Fixture adapter executing fetch from bare remote and local fast-forward."""

    def __init__(self, bare_remote_path: Path) -> None:
        self._bare_remote = bare_remote_path

    async def adopt(
        self,
        worktree: Any,
        policy: Any,
        previous_sha: str,
        new_sha: str,
        base_sha: str,
    ) -> None:
        fetch_res = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(worktree.path), "fetch", str(self._bare_remote), new_sha],
            capture_output=True,
            text=True,
        )
        if fetch_res.returncode != 0:
            raise RuntimeError(f"Fetch from bare remote failed: {fetch_res.stderr}")
        for ancestor in (previous_sha, base_sha):
            res = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(worktree.path), "merge-base", "--is-ancestor", ancestor, new_sha],
                capture_output=True,
            )
            if res.returncode != 0:
                raise RuntimeError(f"{ancestor} is not an ancestor of {new_sha}")
        reset_res = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(worktree.path), "reset", "--hard", new_sha],
            capture_output=True,
            text=True,
        )
        if reset_res.returncode != 0:
            raise RuntimeError(f"Reset failed: {reset_res.stderr}")

    async def inspect(
        self,
        worktree: Any,
        policy: Any,
        previous_sha: str,
        new_sha: str,
        base_sha: str,
    ) -> None:
        rev_res = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(worktree.path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        if rev_res.stdout.strip() != new_sha:
            raise RuntimeError(f"Worktree HEAD {rev_res.stdout.strip()} != {new_sha}")
        for ancestor in (previous_sha, base_sha):
            res = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(worktree.path), "merge-base", "--is-ancestor", ancestor, new_sha],
                capture_output=True,
            )
            if res.returncode != 0:
                raise RuntimeError(f"{ancestor} is not an ancestor of {new_sha}")
        status = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(worktree.path), "status", "--porcelain"],
            capture_output=True,
            text=True,
        )
        if status.stdout.strip():
            raise RuntimeError(f"Worktree has uncommitted changes: {status.stdout}")
