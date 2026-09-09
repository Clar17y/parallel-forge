"""Deterministic, narrowly scoped GitHub release-write boundary."""

from __future__ import annotations

from typing import Protocol

from forge.domain.merge_queue import MergeQueueReceipt
from forge.domain.release import GitHubPullRequest


class GitHubWritePort(Protocol):
    async def find_pull_requests(
        self, repository: str, head_repository: str, head_ref: str, base_ref: str
    ) -> tuple[GitHubPullRequest, ...]: ...

    async def create_pull_request(
        self,
        repository: str,
        head_repository: str,
        head_ref: str,
        base_ref: str,
        title: str,
        body: str | None,
    ) -> GitHubPullRequest: ...

    async def get_pull_request(
        self, repository: str, pull_request_number: int
    ) -> GitHubPullRequest: ...

    async def get_branch_sha(self, repository: str, ref: str) -> str: ...

    async def update_branch(
        self, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> GitHubPullRequest: ...

    async def merge_pull_request(
        self,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
        merge_method: str,
    ) -> GitHubPullRequest: ...


class GitHubMergeQueuePort(Protocol):
    """Queue submission and observation; neither implies merge completion."""

    async def enqueue(
        self,
        repository: str,
        number: int,
        node_id: str,
        head_sha: str,
        merge_method: str,
        correlation_id: str,
    ) -> MergeQueueReceipt: ...

    async def observe(
        self, repository: str, number: int, node_id: str, head_sha: str
    ) -> MergeQueueReceipt | None: ...


__all__ = ["GitHubMergeQueuePort", "GitHubWritePort"]
