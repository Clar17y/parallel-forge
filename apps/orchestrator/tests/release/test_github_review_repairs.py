"""Integrated review regressions T21-23-R01 through R03."""

import httpx
import pytest
from test_github_client import _client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "states,blocked",
    [
        (["CHANGES_REQUESTED", "COMMENTED"], True),
        (["CHANGES_REQUESTED", "COMMENTED", "APPROVED"], False),
        (["DISMISSED", "COMMENTED"], False),
        (["CHANGES_REQUESTED", "DISMISSED"], True),
    ],
)
async def test_comments_and_dismissed_other_reviews_do_not_clear_effective_change_request(
    states, blocked
):
    def handler(request):
        if request.url.path.endswith("/reviews"):
            return httpx.Response(
                200,
                json=[
                    {
                        "user": {"login": "reviewer"},
                        "state": state,
                        "submitted_at": f"2026-01-01T00:00:0{i}Z",
                    }
                    for i, state in enumerate(states)
                ],
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}}
                        }
                    }
                }
            },
        )

    result = await _client(handler).get_reviews("owner/repo", 1)
    assert any(item.blocks_merge for item in result) is blocked


@pytest.mark.asyncio
@pytest.mark.parametrize("classic", [404, None, False])
@pytest.mark.parametrize(
    "branch,applies", [("qa/foo/bar", False), ("qa/foo", True), ("main", True)]
)
async def test_protection_uses_github_effective_branch_rules_even_without_classic_checks(
    classic, branch, applies
):
    paths = []

    def handler(request):
        path = request.url.path
        paths.append(path)
        if path.endswith("/protection"):
            if classic == 404:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "required_status_checks": None if classic is None else {"strict": False},
                    "enforce_admins": {"enabled": True},
                    "required_pull_request_reviews": None,
                },
            )
        if path == "/user":
            return httpx.Response(200, json={"id": 1, "login": "forge"})
        if path.endswith("/permission"):
            return httpx.Response(200, json={"permission": "write"})
        if "/rules/branches/" in path:
            return httpx.Response(
                200, json=[{"type": "merge_queue", "ruleset_id": 42}] if applies else []
            )
        if path.endswith("/rulesets/42"):
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "target": "branch",
                    "enforcement": "active",
                    "bypass_actors": [],
                    "rules": [{"type": "merge_queue"}],
                    "conditions": {
                        "ref_name": {
                            "include": ["refs/heads/qa/*", "~DEFAULT_BRANCH", "~ALL"],
                            "exclude": [],
                        }
                    },
                },
            )
        return httpx.Response(200, json=[{"id": 42}])

    result = await _client(handler).get_merge_protection("owner/repo", branch)
    assert result.safe_for_managed_merge is applies
    assert any("/rules/branches/" in path for path in paths)
