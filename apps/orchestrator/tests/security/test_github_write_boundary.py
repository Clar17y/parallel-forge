from __future__ import annotations

import httpx
import pytest
from forge.release.github_write import GitHubWrite, GitHubWriteError


class _Resolver:
    async def resolve(self, _: str) -> str:
        return "safe-token"


@pytest.mark.asyncio
async def test_write_boundary_rejects_untrusted_repository_and_redirect() -> None:
    adapter = GitHubWrite(
        _Resolver(),
        "env://GITHUB_TOKEN",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(302))),
    )
    with pytest.raises(GitHubWriteError, match="invalid_request"):
        await adapter.get_branch_sha("owner/repo?evil", "main")
    with pytest.raises(GitHubWriteError, match="untrusted_redirect"):
        await adapter.get_branch_sha("owner/repo", "main")
