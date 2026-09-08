from __future__ import annotations

import httpx
import pytest
from forge.domain.release import GitHubPullRequest
from forge.release.github_write import GitHubWrite, GitHubWriteError


class _Resolver:
    async def resolve(self, reference: str) -> str:
        assert reference == "env://GITHUB_TOKEN"
        return "github_pat_this_is_a_test_token"


def _adapter(handler):
    return GitHubWrite(
        _Resolver(),
        "env://GITHUB_TOKEN",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _pr(*, merged: bool = False) -> dict[str, object]:
    return {
        "number": 7,
        "node_id": "PR_node",
        "html_url": "https://github.com/Owner/Repo/pull/7",
        "state": "closed" if merged else "open",
        "merged": merged,
        "merge_commit_sha": "c" * 40 if merged else None,
        "head": {"repo": {"full_name": "Head/Repo"}, "ref": "forge/run", "sha": "a" * 40},
        "base": {"repo": {"full_name": "Owner/Repo"}, "ref": "main", "sha": "b" * 40},
    }


@pytest.mark.asyncio
async def test_listing_filters_exact_head_and_base_before_parsing_history():
    def response(request):
        if request.url.path.endswith("/7"):
            return httpx.Response(200, json=_pr())
        assert request.url.params["state"] == "all"
        assert request.url.params["head"] == "Head:forge/run"
        assert request.url.params["base"] == "main"
        summary = _pr()
        del summary["merged"]  # GitHub's pull-request-simple listing schema.
        summary["merged_at"] = None
        return httpx.Response(200, json=[summary])

    found = await _adapter(response).find_pull_requests(
        "Owner/Repo", "Head/Repo", "forge/run", "main"
    )
    assert len(found) == 1 and found[0].node_id == "PR_node"


@pytest.mark.asyncio
async def test_conditional_pr_reads_reuse_only_matching_cached_response():
    calls = []

    def response(request):
        calls.append(request)
        if len(calls) == 1:
            assert "If-None-Match" not in request.headers
            return httpx.Response(200, json=_pr(), headers={"ETag": '"pr-one"'})
        assert request.headers["If-None-Match"] == '"pr-one"'
        return httpx.Response(304)

    adapter = _adapter(response)
    first = await adapter.get_pull_request("Owner/Repo", 7)
    assert await adapter.get_pull_request("Owner/Repo", 7) == first
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_merge_clears_read_cache_before_read_after_write():
    calls = []

    def response(request):
        calls.append(request)
        assert "If-None-Match" not in request.headers
        if request.method == "PUT":
            return httpx.Response(200, json={"merged": True})
        return httpx.Response(200, json=_pr(merged=len(calls) > 1), headers={"ETag": '"pr"'})

    adapter = _adapter(response)
    assert not (await adapter.get_pull_request("Owner/Repo", 7)).merged
    assert (await adapter.merge_pull_request("Owner/Repo", 7, "a" * 40, "squash")).merged
    assert [r.method for r in calls] == ["GET", "PUT", "GET"]


@pytest.mark.asyncio
async def test_etag_cache_does_not_cross_credentials_or_accept_unsolicited_304():
    class Resolver:
        token = "test-token-one"

        async def resolve(self, reference):
            return self.token

    resolver = Resolver()
    calls = []

    def response(request):
        calls.append(request)
        assert "If-None-Match" not in request.headers
        return (
            httpx.Response(200, json=_pr(), headers={"ETag": '"first"'})
            if len(calls) == 1
            else httpx.Response(304)
        )

    adapter = GitHubWrite(
        resolver,
        "env://GITHUB_TOKEN",
        client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
    )
    await adapter.get_pull_request("Owner/Repo", 7)
    resolver.token = "test-token-two"
    with pytest.raises(GitHubWriteError, match="invalid_response"):
        await adapter.get_pull_request("Owner/Repo", 7)


@pytest.mark.asyncio
async def test_etag_cache_evicts_old_entries(monkeypatch):
    monkeypatch.setattr("forge.release.github_write._MAX_CACHE", 1)

    def response(request):
        assert "If-None-Match" not in request.headers
        return httpx.Response(200, json={"object": {"sha": "a" * 40}}, headers={"ETag": '"branch"'})

    adapter = _adapter(response)
    for branch in ("main", "feature", "main"):
        assert await adapter.get_branch_sha("Owner/Repo", branch) == "a" * 40


@pytest.mark.asyncio
async def test_matching_malformed_pr_is_not_silently_treated_as_absent():
    value = _pr()
    value["head"]["repo"] = None
    adapter = _adapter(lambda request: httpx.Response(200, json=[value]))
    with pytest.raises(GitHubWriteError, match="malformed_response"):
        await adapter.find_pull_requests("Owner/Repo", "Head/Repo", "forge/run", "main")


@pytest.mark.asyncio
async def test_detail_read_rejects_a_different_pull_number():
    value = _pr()
    value.update(number=8, html_url="https://github.com/Owner/Repo/pull/8")
    adapter = _adapter(lambda request: httpx.Response(200, json=value))
    with pytest.raises(GitHubWriteError, match="malformed_response"):
        await adapter.get_pull_request("Owner/Repo", 7)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,category", [(500, "unavailable"), (503, "unavailable"), (429, "rate_limited")]
)
@pytest.mark.parametrize("write", [False, True])
async def test_transient_read_failures_are_classified_without_retrying_writes(
    status, category, write
):
    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(status)

    adapter = _adapter(response)
    with pytest.raises(GitHubWriteError, match="uncertain" if write else category):
        if write:
            await adapter.create_pull_request(
                "Owner/Repo", "Head/Repo", "forge/run", "main", "title", "body"
            )
        else:
            await adapter.get_pull_request("Owner/Repo", 7)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 405, 422])
async def test_definite_merge_refusal_has_one_attempt_and_no_read_after_write(status):
    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(status)

    with pytest.raises(GitHubWriteError, match="rejected"):
        await _adapter(response).merge_pull_request("Owner/Repo", 7, "a" * 40, "squash")
    assert len(calls) == 1 and calls[0].method == "PUT"


@pytest.mark.asyncio
async def test_create_has_one_attempt_and_returns_remote_identity() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=_pr())

    result = await _adapter(handler).create_pull_request(
        "Owner/Repo", "Head/Repo", "forge/run", "main", "title", "body"
    )
    assert isinstance(result, GitHubPullRequest)
    assert result.node_id == "PR_node"
    assert len(seen) == 1
    assert seen[0].url == "https://api.github.com/repos/Owner/Repo/pulls"
    assert seen[0].headers["X-GitHub-Api-Version"] == "2026-03-10"


@pytest.mark.asyncio
async def test_update_and_merge_require_expected_head_and_map_stale_conflicts() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/update-branch") or request.url.path.endswith("/merge")
        return httpx.Response(409)

    adapter = _adapter(handler)
    with pytest.raises(GitHubWriteError, match="stale"):
        await adapter.update_branch("Owner/Repo", 7, "a" * 40)
    with pytest.raises(GitHubWriteError, match="stale"):
        await adapter.merge_pull_request("Owner/Repo", 7, "a" * 40, "squash")


@pytest.mark.asyncio
async def test_update_and_merge_read_canonical_pr_after_acknowledgement() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("update-branch"):
            return httpx.Response(202, json={"message": "updated"})
        if request.url.path.endswith("/merge"):
            return httpx.Response(200, json={"merged": True, "sha": "c" * 40})
        return httpx.Response(200, json=_pr(merged=True))

    adapter = _adapter(handler)
    assert (await adapter.update_branch("Owner/Repo", 7, "a" * 40)).merged
    assert (await adapter.merge_pull_request("Owner/Repo", 7, "a" * 40, "squash")).merged
    assert calls == [
        "/repos/Owner/Repo/pulls/7/update-branch",
        "/repos/Owner/Repo/pulls/7",
        "/repos/Owner/Repo/pulls/7/merge",
        "/repos/Owner/Repo/pulls/7",
    ]


@pytest.mark.asyncio
async def test_transport_failure_is_uncertain_and_does_not_leak_token() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("github_pat_this_is_a_test_token")

    with pytest.raises(GitHubWriteError, match="uncertain") as raised:
        await _adapter(handler).create_pull_request(
            "Owner/Repo", "Head/Repo", "forge/run", "main", "title", "body"
        )
    assert "github_pat_this" not in str(raised.value)
