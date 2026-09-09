"""Offline deterministic GitHub write fake, including ambiguous-effect failures."""

from __future__ import annotations

from forge.domain.operation import canonical_digest
from forge.domain.release import GitHubPullRequest
from forge.release.github_write import GitHubWriteError, _number, _ref, _repository, _sha


class FakeGitHubWriteCrash(RuntimeError):
    """Simulates a process crash after an externally visible write."""


class FakeGitHubWrite:
    def __init__(self) -> None:
        self.pull_requests: dict[tuple[str, int], GitHubPullRequest] = {}
        self.branch_shas: dict[tuple[str, str], str] = {}
        self.uncertain_next_write = False
        self.crash_next_write = False
        self._next_number = 1

    async def find_pull_requests(
        self, repository: str, head_repository: str, head_ref: str, base_ref: str
    ) -> tuple[GitHubPullRequest, ...]:
        repository, head_repository, head_ref, base_ref = (
            _repository(repository),
            _repository(head_repository),
            _ref(head_ref),
            _ref(base_ref),
        )
        return tuple(
            pr
            for (repo, _), pr in self.pull_requests.items()
            if repo == repository
            and pr.head_repository == head_repository
            and pr.head_ref == head_ref
            and pr.base_ref == base_ref
        )

    async def create_pull_request(
        self,
        repository: str,
        head_repository: str,
        head_ref: str,
        base_ref: str,
        title: str,
        body: str | None,
    ) -> GitHubPullRequest:
        del title, body
        repository, head_repository, head_ref, base_ref = (
            _repository(repository),
            _repository(head_repository),
            _ref(head_ref),
            _ref(base_ref),
        )
        number = self._next_number
        self._next_number += 1
        sha = self.branch_shas.get((head_repository, head_ref), "0" * 40)
        pull = GitHubPullRequest(
            number,
            f"PR_{number}",
            f"https://github.com/{repository}/pull/{number}",
            head_repository,
            head_ref,
            sha,
            repository,
            base_ref,
            self.branch_shas.get((repository, base_ref), "0" * 40),
            "open",
            False,
            None,
        )
        self.pull_requests[(repository, number)] = pull
        self._after_write()
        return pull

    async def get_pull_request(
        self, repository: str, pull_request_number: int
    ) -> GitHubPullRequest:
        return self.pull_requests[(_repository(repository), _number(pull_request_number))]

    async def get_branch_sha(self, repository: str, ref: str) -> str:
        return self.branch_shas[(_repository(repository), _ref(ref))]

    async def update_branch(
        self, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> GitHubPullRequest:
        key = (_repository(repository), _number(pull_request_number))
        expected_head_sha = _sha(expected_head_sha, "invalid_request")
        pull = self.pull_requests[key]
        if pull.head_sha != expected_head_sha:
            raise GitHubWriteError("stale")
        head_sha = self.branch_shas.get((pull.head_repository, pull.head_ref), pull.head_sha)
        updated = GitHubPullRequest(
            pull.number,
            pull.node_id,
            pull.url,
            pull.head_repository,
            pull.head_ref,
            head_sha,
            pull.base_repository,
            pull.base_ref,
            pull.base_sha,
            pull.state,
            pull.merged,
            pull.merge_sha,
        )
        self.pull_requests[key] = updated
        self._after_write()
        return updated

    async def merge_pull_request(
        self, repository: str, pull_request_number: int, expected_head_sha: str, merge_method: str
    ) -> GitHubPullRequest:
        if merge_method not in {"merge", "squash", "rebase"}:
            raise GitHubWriteError("invalid_request")
        key = (_repository(repository), _number(pull_request_number))
        expected_head_sha = _sha(expected_head_sha, "invalid_request")
        pull = self.pull_requests[key]
        if pull.head_sha != expected_head_sha or pull.state != "open":
            raise GitHubWriteError("stale")
        updated = GitHubPullRequest(
            pull.number,
            pull.node_id,
            pull.url,
            pull.head_repository,
            pull.head_ref,
            pull.head_sha,
            pull.base_repository,
            pull.base_ref,
            pull.base_sha,
            "closed",
            True,
            canonical_digest(
                {
                    "repository": repository,
                    "number": pull.number,
                    "head": pull.head_sha,
                    "method": merge_method,
                }
            )[:40],
        )
        self.pull_requests[key] = updated
        self._after_write()
        return updated

    def _after_write(self) -> None:
        if self.crash_next_write:
            self.crash_next_write = False
            raise FakeGitHubWriteCrash("simulated crash after write")
        if self.uncertain_next_write:
            self.uncertain_next_write = False
            raise GitHubWriteError("uncertain")


__all__ = ["FakeGitHubWrite", "FakeGitHubWriteCrash"]
