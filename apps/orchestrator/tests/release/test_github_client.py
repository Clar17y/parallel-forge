from __future__ import annotations

import asyncio

import httpx
import pytest
from forge.release.github_client import GitHubClient, GitHubClientError


class _Resolver:
    async def resolve(self, reference: str) -> str:
        assert reference == "env://GITHUB_TOKEN"
        return "github_pat_this_is_a_test_token_with_enough_length"


def _client(handler):
    return GitHubClient(
        _Resolver(),
        "env://GITHUB_TOKEN",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=lambda _: _noop(),
    )


async def _noop() -> None:
    pass


@pytest.mark.asyncio
async def test_response_deadline_closes_stalled_body_and_bounds_retries(monkeypatch):
    monkeypatch.setattr("forge.release.github_client._REQUEST_SECONDS", 0.01, raising=False)
    closed = []

    class StalledBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{"
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    client = _client(lambda _: httpx.Response(200, stream=StalledBody()))
    with pytest.raises(GitHubClientError, match="unavailable"):
        await asyncio.wait_for(client.get_issue("owner/repo", 1), timeout=0.5)
    assert len(closed) == 3


@pytest.mark.asyncio
async def test_get_pull_request_normalizes_exact_remote_identity() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.github.com/repos/Clar17y/Parallel/pulls/42"
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(
            200,
            json={
                "number": 42,
                "state": "open",
                "head": {"sha": "a" * 40, "ref": "forge/run-1"},
                "base": {"sha": "b" * 40, "ref": "main"},
                "draft": False,
            },
        )

    snapshot = await _client(handler).get_pull_request("Clar17y/Parallel", 42)
    assert (snapshot.head_sha, snapshot.base_sha, snapshot.base_ref) == ("a" * 40, "b" * 40, "main")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,category", [(401, "forbidden"), (403, "forbidden"), (404, "not_found")]
)
async def test_status_errors_are_stable_and_do_not_expose_token(status: int, category: str) -> None:
    client = _client(lambda _: httpx.Response(status))
    with pytest.raises(GitHubClientError, match=category) as error:
        await client.get_issue("owner/repo", 1)
    assert "github_pat_this" not in str(error.value)


@pytest.mark.asyncio
async def test_etag_304_uses_only_matching_scoped_cache() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={"ETag": '"v1"'},
                json={
                    "number": 1,
                    "title": "x",
                    "body": None,
                    "html_url": "https://github.com/owner/repo/issues/1",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "state": "open",
                },
            )
        assert request.headers["If-None-Match"] == '"v1"'
        return httpx.Response(304)

    client = _client(handler)
    assert (await client.get_issue("owner/repo", 1)).title == "x"
    assert (await client.get_issue("owner/repo", 1)).title == "x"


@pytest.mark.asyncio
async def test_rejects_redirect_malformed_and_untrusted_link() -> None:
    client = _client(lambda _: httpx.Response(302, headers={"Location": "https://evil.test"}))
    with pytest.raises(GitHubClientError, match="untrusted_redirect"):
        await client.get_issue("owner/repo", 1)
    client = _client(lambda _: httpx.Response(200, content=b"not json"))
    with pytest.raises(GitHubClientError, match="malformed_response"):
        await client.get_issue("owner/repo", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 503])
async def test_pagination_and_retries_are_bounded(status) -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(status)
        if request.url.path.endswith("/statuses"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={
                "check_runs": [
                    {
                        "name": "test",
                        "head_sha": "a" * 40,
                        "status": "completed",
                        "conclusion": "success",
                        "details_url": "https://github.com/owner/repo/actions/1",
                    }
                ]
            },
        )

    checks = await _client(handler).get_checks("owner/repo", "a" * 40)
    assert checks[0].name == "test" and attempts == 3


@pytest.mark.asyncio
async def test_merge_protection_fails_closed_when_protection_cannot_be_read() -> None:
    protection = await _client(lambda _: httpx.Response(403)).get_merge_protection(
        "owner/repo", "main"
    )
    assert not protection.safe_for_managed_merge


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocker", [None, "admin", "users", "teams", "apps", "ruleset", "repository", "installation"]
)
async def test_installation_token_requires_repository_access_and_no_possible_bypass(blocker):
    async def handler(request):
        path = request.url.path
        if path == "/user":
            return httpx.Response(403)
        if path == "/installation/repositories":
            return (
                httpx.Response(403)
                if blocker == "installation"
                else httpx.Response(
                    200,
                    json={
                        "repositories": [
                            {
                                "id": 1,
                                "full_name": "other/repo"
                                if blocker == "repository"
                                else "owner/repo",
                            }
                        ]
                    },
                )
            )
        if path.endswith("/protection"):
            allowances = {"users": [], "teams": [], "apps": []}
            if blocker in allowances:
                allowances[blocker] = [{"id": 7}]
            return httpx.Response(
                200,
                json={
                    "required_status_checks": {"strict": True, "contexts": ["ci"]},
                    "enforce_admins": {"enabled": blocker != "admin"},
                    "required_pull_request_reviews": {"bypass_pull_request_allowances": allowances},
                },
            )
        if "/rules/branches/" in path:
            return httpx.Response(
                200,
                json=[
                    {
                        "ruleset_id": 9,
                        "type": "merge_queue",
                        "parameters": {"merge_method": "SQUASH"},
                    }
                ],
            )
        if path.endswith("/rulesets/9"):
            return httpx.Response(
                200,
                json={
                    "id": 9,
                    "target": "branch",
                    "enforcement": "active",
                    "rules": [{"type": "merge_queue", "parameters": {"merge_method": "SQUASH"}}],
                    "bypass_actors": [
                        {"actor_type": "User", "actor_id": 7, "bypass_mode": "always"}
                    ]
                    if blocker == "ruleset"
                    else [],
                },
            )
        raise AssertionError(f"unexpected endpoint {path}")

    protection = await _client(handler).get_merge_protection("owner/repo", "main")
    assert protection.safe_for_managed_merge == (blocker is None)
    if blocker is None:
        assert protection.verified and not protection.actor_can_bypass
        assert protection.required_check_names == ("ci",)
        assert protection.evidence_source == "branch_protection_rulesets_no_bypass"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "methods", [("MERGE",), ("SQUASH",), ("REBASE",), ("MERGE", "SQUASH"), ("invalid",)]
)
async def test_effective_merge_queue_method_is_verified(methods):
    async def handler(request):
        path = request.url.path
        if path.endswith("/protection"):
            return httpx.Response(404)
        if path == "/user":
            return httpx.Response(200, json={"id": 1, "login": "operator"})
        if path.endswith("/permission"):
            return httpx.Response(200, json={"permission": "write"})
        rules = [
            {"ruleset_id": i + 1, "type": "merge_queue", "parameters": {"merge_method": method}}
            for i, method in enumerate(methods)
        ]
        if "/rules/branches/" in path:
            return httpx.Response(200, json=rules)
        index = int(path.rsplit("/", 1)[1])
        return httpx.Response(
            200,
            json={
                "id": index,
                "target": "branch",
                "enforcement": "active",
                "bypass_actors": [],
                "rules": [{"type": "merge_queue", "parameters": rules[index - 1]["parameters"]}],
            },
        )

    protection = await _client(handler).get_merge_protection("owner/repo", "main")
    if len(methods) == 1 and methods[0] != "invalid":
        assert protection.verified
        assert protection.merge_queue_method == methods[0].lower()
    else:
        assert not protection.safe_for_managed_merge


@pytest.mark.asyncio
async def test_merge_protection_requires_actor_bypass_evidence() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/protection"):
            return httpx.Response(
                200,
                json={
                    "required_status_checks": {"strict": True, "contexts": ["ci"]},
                    "enforce_admins": {"enabled": True},
                    "required_pull_request_reviews": {
                        "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": []}
                    },
                },
            )
        if "/rules/branches/" in request.url.path:
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/permission"):
            return httpx.Response(200, json={"permission": "write"})
        assert request.url.path == "/user"
        return httpx.Response(200, json={"login": "forge-app", "id": 1})

    protection = await _client(handler).get_merge_protection("owner/repo", "main")
    assert protection.safe_for_managed_merge and not protection.actor_can_bypass


@pytest.mark.asyncio
async def test_issue_identity_and_aware_updated_at_are_required() -> None:
    client = _client(
        lambda _: httpx.Response(
            200,
            json={
                "number": 2,
                "title": "x",
                "html_url": "https://github.com/owner/repo/issues/2?token=no",
                "updated_at": "2026-01-01T00:00:00",
                "state": "open",
            },
        )
    )
    with pytest.raises(GitHubClientError, match="malformed_response"):
        await client.get_issue("owner/repo", 1)


@pytest.mark.asyncio
async def test_reviews_paginate_threads_and_keep_blocker_without_rest_reviews() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/reviews"):
            return httpx.Response(200, json=[])
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [{"isResolved": False, "comments": {"totalCount": 1}}],
                                    "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                                }
                            }
                        }
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [{"isResolved": True, "comments": {"totalCount": 2}}],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            },
        )

    reviews = await _client(handler).get_reviews("owner/repo", 1)
    assert len(reviews) == 1 and reviews[0].blocks_merge and reviews[0].comment_count == 3


@pytest.mark.asyncio
async def test_rate_limit_uses_server_retry_after() -> None:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    attempts = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(403, headers={"Retry-After": "3"})
        return httpx.Response(
            200,
            json={
                "number": 1,
                "title": "x",
                "html_url": "https://github.com/owner/repo/issues/1",
                "updated_at": "2026-01-01T00:00:00Z",
                "state": "open",
            },
        )

    client = GitHubClient(
        _Resolver(),
        "env://GITHUB_TOKEN",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=sleep,
    )
    await client.get_issue("owner/repo", 1)
    assert delays == [3.0]


@pytest.mark.asyncio
async def test_checks_include_latest_commit_status_contexts() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        return httpx.Response(
            200,
            json=[
                {"context": "ci", "state": "success", "target_url": "https://ci.example/2"},
                {"context": "ci", "state": "failure", "target_url": "https://ci.example/1"},
            ],
        )

    checks = await _client(handler).get_checks("owner/repo", "a" * 40)
    assert [(check.name, check.conclusion, check.head_sha) for check in checks] == [
        ("status:ci", "success", "a" * 40)
    ]


@pytest.mark.asyncio
async def test_check_and_review_text_are_projected_without_fetching_urls() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "name": "ci",
                            "head_sha": "a" * 40,
                            "status": "completed",
                            "conclusion": "failure",
                            "details_url": "https://ci.test/1",
                            "output": {"summary": "failed", "text": "trace"},
                        }
                    ]
                },
            )
        if request.url.path.endswith("statuses"):
            return httpx.Response(
                200, json=[{"context": "legacy", "state": "failure", "description": "broken"}]
            )
        if request.url.path.endswith("reviews"):
            return httpx.Response(
                200,
                json=[
                    {
                        "user": {"login": "r"},
                        "state": "changes_requested",
                        "submitted_at": None,
                        "body": "fix it",
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "isResolved": False,
                                        "comments": {
                                            "totalCount": 1,
                                            "nodes": [{"body": "thread feedback"}],
                                        },
                                    },
                                    {
                                        "isResolved": True,
                                        "comments": {
                                            "totalCount": 1,
                                            "nodes": [{"body": "hidden"}],
                                        },
                                    },
                                ],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            },
        )

    client = _client(handler)
    checks = await client.get_checks("owner/repo", "a" * 40)
    assert (checks[0].summary, checks[0].text, checks[1].summary) == ("failed", "trace", "broken")
    reviews = await client.get_reviews("owner/repo", 1)
    assert reviews[0].body == "fix it" and reviews[-1].feedback == ("thread feedback",)


@pytest.mark.asyncio
async def test_merge_protection_unions_classic_and_effective_ruleset_check_names() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/protection"):
            return httpx.Response(
                200,
                json={
                    "required_status_checks": {"strict": True, "contexts": ["z", "classic"]},
                    "enforce_admins": {"enabled": True},
                    "required_pull_request_reviews": {
                        "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": []}
                    },
                },
            )
        if "/rules/branches/" in path:
            return httpx.Response(
                200,
                json=[
                    {
                        "ruleset_id": 9,
                        "type": "required_status_checks",
                        "parameters": {
                            "strict_required_status_checks_policy": True,
                            "required_status_checks": [{"context": "a"}, {"context": "z"}],
                        },
                    }
                ],
            )
        if path.endswith("/rulesets/9"):
            return httpx.Response(
                200,
                json={
                    "id": 9,
                    "target": "branch",
                    "enforcement": "active",
                    "rules": [{"type": "required_status_checks"}],
                    "bypass_actors": [],
                },
            )
        if path.endswith("/permission"):
            return httpx.Response(200, json={"permission": "write"})
        return httpx.Response(200, json={"login": "forge", "id": 1})

    protection = await _client(handler).get_merge_protection("owner/repo", "main")
    assert protection.required_check_names == ("a", "classic", "z")
