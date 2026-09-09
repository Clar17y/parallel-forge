"""Deterministic mutable in-memory implementation of the read-only GitHub port."""

from __future__ import annotations

from forge.domain.github import (
    CheckSnapshot,
    GitHubIssue,
    MergeProtection,
    PullRequestSnapshot,
    ReviewSnapshot,
)


class FakeGitHub:
    def __init__(self) -> None:
        self.issues: dict[tuple[str, int], GitHubIssue] = {}
        self.pull_requests: dict[tuple[str, int], PullRequestSnapshot] = {}
        self.checks: dict[tuple[str, str], tuple[CheckSnapshot, ...]] = {}
        self.reviews: dict[tuple[str, int], tuple[ReviewSnapshot, ...]] = {}
        self.bases: dict[tuple[str, str], str] = {}
        self.merge_protections: dict[tuple[str, str], MergeProtection] = {}

    @staticmethod
    def _text_key(repository: str, value: str) -> tuple[str, str]:
        return (repository.casefold(), value)

    @staticmethod
    def _number_key(repository: str, value: int) -> tuple[str, int]:
        return (repository.casefold(), value)

    async def get_issue(self, repository: str, issue_number: int) -> GitHubIssue:
        return self.issues[self._number_key(repository, issue_number)]

    async def get_pull_request(self, repository: str, pull_number: int) -> PullRequestSnapshot:
        return self.pull_requests[self._number_key(repository, pull_number)]

    async def get_checks(self, repository: str, ref: str) -> tuple[CheckSnapshot, ...]:
        return tuple(self.checks.get(self._text_key(repository, ref), ()))

    async def get_reviews(self, repository: str, pull_number: int) -> tuple[ReviewSnapshot, ...]:
        return tuple(self.reviews.get(self._number_key(repository, pull_number), ()))

    async def get_base(self, repository: str, branch: str) -> str:
        return self.bases[self._text_key(repository, branch)]

    async def get_merge_protection(self, repository: str, branch: str) -> MergeProtection:
        return self.merge_protections.get(
            self._text_key(repository, branch),
            MergeProtection(False, False, False, "unverified", verified=False),
        )


__all__ = ["FakeGitHub"]
