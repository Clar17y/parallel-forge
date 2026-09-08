"""Normalized, immutable projections of GitHub read state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class GitHubIssue:
    number: int
    title: str
    body: str | None
    source_url: str
    updated_at: datetime | None
    state: str


@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    number: int
    state: str
    head_sha: str
    head_ref: str
    base_sha: str
    base_ref: str
    draft: bool
    mergeable: bool | None = None


@dataclass(frozen=True, slots=True)
class CheckSnapshot:
    name: str
    status: str
    conclusion: str | None
    details_url: str | None = None
    head_sha: str | None = None
    summary: str | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    reviewer: str
    state: str
    submitted_at: datetime | None
    requested_changes: bool = False
    unresolved_threads: int = 0
    comment_count: int = 0
    body: str | None = None
    feedback: tuple[str, ...] = ()

    @property
    def blocks_merge(self) -> bool:
        return self.requested_changes or self.unresolved_threads > 0


@dataclass(frozen=True, slots=True)
class MergeProtection:
    strict_required_checks: bool
    merge_queue_enabled: bool
    actor_can_bypass: bool
    evidence_source: str
    verified: bool = True
    required_check_names: tuple[str, ...] = ()

    @property
    def safe_for_managed_merge(self) -> bool:
        return (
            self.verified
            and (self.strict_required_checks or self.merge_queue_enabled)
            and not self.actor_can_bypass
        )


__all__ = [
    "CheckSnapshot",
    "GitHubIssue",
    "MergeProtection",
    "PullRequestSnapshot",
    "ReviewSnapshot",
]
