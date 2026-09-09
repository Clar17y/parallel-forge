"""Immutable remote release identities used for deterministic reconciliation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GitHubPullRequest:
    """The complete GitHub identity required to reconcile a release effect."""

    number: int
    node_id: str
    url: str
    head_repository: str
    head_ref: str
    head_sha: str
    base_repository: str
    base_ref: str
    base_sha: str
    state: str
    merged: bool
    merge_sha: str | None


__all__ = ["GitHubPullRequest"]
