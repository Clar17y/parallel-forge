"""Read-only GitHub boundary used by issue and release workflows."""

from __future__ import annotations

from typing import Protocol

from forge.domain.github import (
    CheckSnapshot,
    GitHubIssue,
    MergeProtection,
    PullRequestSnapshot,
    ReviewSnapshot,
)


class GitHubPort(Protocol):
    async def get_issue(self, repository: str, issue_number: int) -> GitHubIssue: ...

    async def get_pull_request(self, repository: str, pull_number: int) -> PullRequestSnapshot: ...

    async def get_checks(self, repository: str, ref: str) -> tuple[CheckSnapshot, ...]: ...

    async def get_reviews(
        self, repository: str, pull_number: int
    ) -> tuple[ReviewSnapshot, ...]: ...

    async def get_base(self, repository: str, branch: str) -> str: ...

    async def get_merge_protection(self, repository: str, branch: str) -> MergeProtection: ...


__all__ = ["GitHubPort"]
