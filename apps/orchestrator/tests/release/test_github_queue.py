from __future__ import annotations

import json

import httpx
import pytest
from forge.release.github_queue import GitHubMergeQueue
from forge.release.github_write import GitHubWrite, GitHubWriteError


class _Resolver:
    async def resolve(self, reference: str) -> str:
        return "test-token"


def _pull():
    return {
        "id": "PR_node", "number": 7, "headRefOid": "a" * 40,
        "repository": {"nameWithOwner": "Owner/Repo"},
    }


def _entry():
    return {
        "id": "MQ_entry", "pullRequest": _pull(),
        "mergeQueue": {"configuration": {"mergeMethod": "SQUASH"}},
    }


def _rest():
    return {
        "number": 7, "node_id": "PR_node",
        "html_url": "https://github.com/Owner/Repo/pull/7",
        "state": "open", "merged": False, "merge_commit_sha": None,
        "head": {"repo": {"full_name": "Owner/Repo"}, "ref": "forge/run", "sha": "a" * 40},
        "base": {"repo": {"full_name": "Owner/Repo"}, "ref": "main", "sha": "b" * 40},
    }


def _adapter(handler):
    return GitHubMergeQueue(GitHubWrite(
        _Resolver(), "env://GITHUB_TOKEN",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ))


@pytest.mark.asyncio
async def test_enqueue_binds_exact_head_and_returns_queue_receipt():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_rest())
        payload = json.loads(request.content)
        assert request.url.path == "/graphql"
        assert payload["variables"]["input"] == {
            "pullRequestId": "PR_node", "expectedHeadOid": "a" * 40,
            "jump": False, "clientMutationId": "intent-1",
        }
        return httpx.Response(200, json={"data": {"enqueuePullRequest": {
            "clientMutationId": "intent-1", "mergeQueueEntry": _entry(),
        }}})

    receipt = await _adapter(handler).enqueue(
        "Owner/Repo", 7, "PR_node", "a" * 40, "squash", "intent-1",
    )
    assert receipt.entry_id == "MQ_entry"
    assert receipt.merge_method == "squash"
    assert not hasattr(receipt, "merged")
    assert [call.method for call in calls] == ["GET", "POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("node_id", "foreign"), ("state", "closed")])
async def test_stale_identity_never_enqueues(field, value):
    def handler(request):
        assert request.method == "GET"
        pull = _rest()
        pull[field] = value
        return httpx.Response(200, json=pull)

    with pytest.raises(GitHubWriteError, match="stale"):
        await _adapter(handler).enqueue("Owner/Repo", 7, "PR_node", "a" * 40, "squash", "id")


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [True, False])
async def test_observation_is_read_only_and_absence_is_not_completion(present):
    def handler(request):
        payload = json.loads(request.content)
        assert payload["query"].lstrip().startswith("query")
        assert payload["variables"] == {"id": "PR_node"}
        return httpx.Response(200, json={"data": {"node": {
            **_pull(), "mergeQueueEntry": _entry() if present else None,
        }}})

    receipt = await _adapter(handler).observe("Owner/Repo", 7, "PR_node", "a" * 40)
    assert (receipt is not None) == present


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["errors", "null", "head", "number", "repository", "method", "correlation"])
async def test_ambiguous_mutation_response_is_redacted_and_never_retried(fault):
    writes = 0

    def handler(request):
        nonlocal writes
        if request.method == "GET":
            return httpx.Response(200, json=_rest())
        writes += 1
        entry = _entry()
        result = {"clientMutationId": "id", "mergeQueueEntry": entry}
        body = {"data": {"enqueuePullRequest": result}}
        if fault == "errors":
            body["errors"] = [{"message": "secret-remote-diagnostic"}]
        elif fault == "null":
            result["mergeQueueEntry"] = None
        elif fault == "head":
            entry["pullRequest"]["headRefOid"] = "b" * 40
        elif fault == "number":
            entry["pullRequest"]["number"] = True
        elif fault == "repository":
            entry["pullRequest"]["repository"]["nameWithOwner"] = "Other/Repo"
        elif fault == "method":
            entry["mergeQueue"]["configuration"]["mergeMethod"] = "MERGE"
        else:
            result["clientMutationId"] = "other"
        return httpx.Response(200, json=body)

    with pytest.raises(GitHubWriteError, match="uncertain") as caught:
        await _adapter(handler).enqueue("Owner/Repo", 7, "PR_node", "a" * 40, "squash", "id")
    assert "secret" not in str(caught.value)
    assert writes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["head", "missing", "null_node", "errors"])
async def test_observation_rejects_drift_or_incomplete_evidence(fault):
    def handler(request):
        node = {**_pull(), "mergeQueueEntry": None}
        body = {"data": {"node": node}}
        if fault == "head":
            node["headRefOid"] = "b" * 40
        elif fault == "missing":
            del node["mergeQueueEntry"]
        elif fault == "null_node":
            body["data"]["node"] = None
        else:
            body["errors"] = [{"message": "secret"}]
        return httpx.Response(200, json=body)

    with pytest.raises(GitHubWriteError):
        await _adapter(handler).observe("Owner/Repo", 7, "PR_node", "a" * 40)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [True, False])
async def test_transport_timeout_is_redacted_and_not_retried(mutate):
    calls = 0

    def handler(request):
        nonlocal calls
        if request.method == "GET":
            return httpx.Response(200, json=_rest())
        calls += 1
        raise httpx.ReadTimeout("secret", request=request)

    adapter = _adapter(handler)
    with pytest.raises(GitHubWriteError, match="uncertain" if mutate else "unavailable"):
        if mutate:
            await adapter.enqueue("Owner/Repo", 7, "PR_node", "a" * 40, "squash", "id")
        else:
            await adapter.observe("Owner/Repo", 7, "PR_node", "a" * 40)
    assert calls == 1


@pytest.mark.asyncio
async def test_head_drift_fails_before_mutation():
    def handler(request):
        assert request.method == "GET"
        pull = _rest()
        pull["head"]["sha"] = "b" * 40
        return httpx.Response(200, json=pull)

    with pytest.raises(GitHubWriteError, match="stale"):
        await _adapter(handler).enqueue("Owner/Repo", 7, "PR_node", "a" * 40, "squash", "id")

@pytest.mark.asyncio
@pytest.mark.parametrize("body,category", [
    ({"errors": [{"message": "secret validation failure"}]}, "rejected"),
    ({"errors": [{"message": "secret", "locations": [{"line": 1, "column": 2}]}],
      "extensions": {"requestId": "private"}}, "rejected"),
    ({"data": None, "errors": [{"message": "secret"}]}, "uncertain"),
    ({"errors": [{"message": "secret", "path": ["enqueuePullRequest"]}]}, "uncertain"),
    ({"errors": []}, "uncertain"),
    ({"errors": [{"message": ""}]}, "uncertain"),
    ({"errors": [{"message": "secret", "locations": [{"line": True, "column": 1}]}]}, "uncertain"),
    ({"errors": [{"message": "secret"}], "unexpected": True}, "uncertain"),
])
async def test_only_well_formed_preexecution_errors_prove_refusal(body, category):
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=_rest() if request.method == "GET" else body)

    adapter = _adapter(handler)
    try:
        with pytest.raises(GitHubWriteError) as caught:
            await adapter.enqueue("Owner/Repo", 7, "PR_node", "a" * 40, "squash", "id")
        assert caught.value.category == category
        assert "secret" not in str(caught.value) and "private" not in str(caught.value)
        assert calls == ["GET", "POST"]
    finally:
        await adapter._write.aclose()
