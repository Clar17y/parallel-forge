from __future__ import annotations

import pytest
from forge.release.fake_github_write import FakeGitHubWrite, FakeGitHubWriteCrash
from forge.release.github_write import GitHubWriteError


@pytest.mark.asyncio
async def test_fake_enforces_head_cas_and_can_model_uncertain_write() -> None:
    fake = FakeGitHubWrite()
    pull = await fake.create_pull_request(
        "owner/repo", "owner/repo", "forge/run", "main", "t", None
    )
    fake.branch_shas[("owner/repo", "forge/run")] = "b" * 40
    with pytest.raises(GitHubWriteError, match="stale"):
        await fake.update_branch("owner/repo", pull.number, "a" * 40)
    pull = await fake.update_branch("owner/repo", pull.number, "0" * 40)
    fake.uncertain_next_write = True
    with pytest.raises(GitHubWriteError, match="uncertain"):
        await fake.merge_pull_request("owner/repo", pull.number, pull.head_sha, "squash")
    assert (await fake.get_pull_request("owner/repo", pull.number)).merged


@pytest.mark.asyncio
async def test_fake_can_model_a_crash_after_the_remote_create() -> None:
    fake = FakeGitHubWrite()
    fake.crash_next_write = True
    with pytest.raises(FakeGitHubWriteCrash):
        await fake.create_pull_request("owner/repo", "owner/repo", "forge/run", "main", "t", None)
    assert len(fake.pull_requests) == 1
