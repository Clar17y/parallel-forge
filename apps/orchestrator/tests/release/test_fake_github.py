from __future__ import annotations

import pytest
from forge.domain.github import GitHubIssue, MergeProtection
from forge.release.fake_github import FakeGitHub


@pytest.mark.asyncio
async def test_fake_normalizes_repository_keys_and_fails_closed_protection() -> None:
    github = FakeGitHub()
    issue = GitHubIssue(1, "title", None, "https://github.com/o/r/issues/1", None, "open")
    github.issues[("owner/repo", 1)] = issue
    assert await github.get_issue("OWNER/REPO", 1) == issue
    assert not (await github.get_merge_protection("owner/repo", "main")).safe_for_managed_merge
    github.merge_protections[("owner/repo", "main")] = MergeProtection(True, True, False, "test")
    assert (await github.get_merge_protection("owner/repo", "main")).safe_for_managed_merge
