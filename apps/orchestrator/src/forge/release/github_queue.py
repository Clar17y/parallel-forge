"""Controlled queue insertion and read-only reconciliation through GitHub GraphQL."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from forge.domain.merge_queue import MergeQueueReceipt
from forge.release.github_write import GitHubWrite, GitHubWriteError, _mapping

_PULL_FIELDS = "id number headRefOid repository { nameWithOwner }"
_ENTRY_FIELDS = (
    "id pullRequest { " + _PULL_FIELDS + " } "
    "mergeQueue { configuration { mergeMethod } }"
)
_ENQUEUE = (
    "mutation ForgeEnqueue($input: EnqueuePullRequestInput!) { "
    "enqueuePullRequest(input: $input) { clientMutationId mergeQueueEntry { "
    + _ENTRY_FIELDS + " } } }"
)
_OBSERVE = (
    "query ForgeObserveQueue($id: ID!) { node(id: $id) { ... on PullRequest { "
    + _PULL_FIELDS + " mergeQueueEntry { " + _ENTRY_FIELDS + " } } } }"
)


class GitHubMergeQueue:
    def __init__(self, write: GitHubWrite) -> None:
        self._write = write

    async def enqueue(
        self, repository: str, number: int, node_id: str, head_sha: str,
        merge_method: str, correlation_id: str,
    ) -> MergeQueueReceipt:
        expected = _expected(repository, number, node_id, head_sha, merge_method)
        if (
            type(correlation_id) is not str or not correlation_id
            or len(correlation_id) > 512 or any(c.isspace() for c in correlation_id)
        ):
            raise GitHubWriteError("invalid_request")
        pull = await self._write.get_pull_request(repository, number)
        if (
            pull.node_id != node_id or pull.head_sha != head_sha
            or pull.base_repository != repository or pull.head_repository != repository
            or pull.state != "open" or pull.merged
        ):
            raise GitHubWriteError("stale")
        # No retries: even GraphQL errors can accompany a committed mutation.
        try:
            response = _mapping(await self._write._json("POST", "/graphql", json={
                "query": _ENQUEUE, "variables": {"input": {
                    "pullRequestId": node_id, "expectedHeadOid": head_sha,
                    "jump": False, "clientMutationId": correlation_id,
                }},
            }))
        except GitHubWriteError:
            raise GitHubWriteError("uncertain") from None
        if _request_error(response):
            raise GitHubWriteError("rejected")
        try:
            data = _data(response)
            result = _mapping(data.get("enqueuePullRequest"))
            if result.get("clientMutationId") != correlation_id:
                raise GitHubWriteError("malformed_response")
            receipt = _receipt(result.get("mergeQueueEntry"), expected)
            if receipt.merge_method != merge_method:
                raise GitHubWriteError("stale")
            return receipt
        except GitHubWriteError, ValidationError:
            raise GitHubWriteError("uncertain") from None

    async def observe(
        self, repository: str, number: int, node_id: str, head_sha: str,
    ) -> MergeQueueReceipt | None:
        expected = _expected(repository, number, node_id, head_sha, "merge")
        try:
            response = _mapping(await self._write._json("POST", "/graphql", json={
                "query": _OBSERVE, "variables": {"id": node_id},
            }))
            node = _mapping(_data(response).get("node"))
            _identity(node, expected)
            # Missing fields are malformed; explicit null is authoritative absence.
            if "mergeQueueEntry" not in node:
                raise GitHubWriteError("malformed_response")
            entry = node["mergeQueueEntry"]
            return None if entry is None else _receipt(entry, expected)
        except ValidationError:
            raise GitHubWriteError("malformed_response") from None
        except GitHubWriteError as error:
            if error.category == "uncertain":
                # GraphQL queries use POST but carry no mutation authority.
                raise GitHubWriteError("unavailable") from None
            raise


def _expected(
    repository: str, number: int, node_id: str, head_sha: str, merge_method: str,
) -> MergeQueueReceipt:
    try:
        return MergeQueueReceipt.model_validate({
            "repository": repository, "pull_request_number": number,
            "pull_request_node_id": node_id, "head_sha": head_sha,
            "merge_method": merge_method, "entry_id": "validation-only",
        })
    except ValidationError:
        raise GitHubWriteError("invalid_request") from None


def _request_error(response: dict[str, Any]) -> bool:
    """GraphQL request errors precede execution; execution results contain data.

    Accept only a well-formed request-error result. Error paths indicate execution
    and cannot prove no effect, even if the server omitted its data field.
    """
    if set(response) - {"errors", "extensions"} or (
        "extensions" in response and not isinstance(response["extensions"], dict)
    ):
        return False
    errors = response.get("errors")
    if not isinstance(errors, list) or not errors:
        return False
    for error in errors:
        if (
            not isinstance(error, dict) or set(error) - {"message", "locations", "extensions"}
            or not isinstance(error.get("message"), str) or not error["message"].strip()
            or ("extensions" in error and not isinstance(error["extensions"], dict))
        ):
            return False
        if "locations" in error:
            locations = error["locations"]
            if not isinstance(locations, list) or not locations:
                return False
            for location in locations:
                if (
                    not isinstance(location, dict) or set(location) != {"line", "column"}
                    or any(type(location[key]) is not int or location[key] < 1 for key in ("line", "column"))
                ):
                    return False
    return True


def _data(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("errors"):
        raise GitHubWriteError("malformed_response")
    return _mapping(response.get("data"))


def _identity(pull: dict[str, Any], expected: MergeQueueReceipt) -> None:
    if (
        pull.get("id") != expected.pull_request_node_id
        or type(pull.get("number")) is not int
        or pull.get("number") != expected.pull_request_number
        or pull.get("headRefOid") != expected.head_sha
        or _mapping(pull.get("repository")).get("nameWithOwner") != expected.repository
    ):
        raise GitHubWriteError("stale")


def _receipt(value: object, expected: MergeQueueReceipt) -> MergeQueueReceipt:
    entry = _mapping(value)
    _identity(_mapping(entry.get("pullRequest")), expected)
    configuration = _mapping(_mapping(entry.get("mergeQueue")).get("configuration"))
    method = configuration.get("mergeMethod")
    if method not in ("MERGE", "SQUASH", "REBASE"):
        raise GitHubWriteError("malformed_response")
    return MergeQueueReceipt.model_validate({
        **expected.model_dump(), "entry_id": entry.get("id"),
        "merge_method": method.lower(),
    })
