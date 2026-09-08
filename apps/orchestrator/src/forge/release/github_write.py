"""Fail-closed GitHub REST write adapter for release controller intents."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from forge.domain.release import GitHubPullRequest
from forge.release.credentials import (
    GitHubCredentialError,
    GitHubCredentialResolverPort,
    validate_github_credential_reference,
)
from forge.release.github_client import _cache_key

_API = "https://api.github.com"
_MAX_RESPONSE_BYTES = 1_000_000
_MAX_PAGES = 20
_MAX_CACHE = 128
_REQUEST_SECONDS = 15.0
_TIMEOUT = httpx.Timeout(10.0, connect=5.0, read=10.0, write=10.0, pool=5.0)
_MERGE_METHODS = frozenset({"merge", "squash", "rebase"})


class GitHubWriteError(RuntimeError):
    def __init__(self, category: str = "unavailable") -> None:
        self.category = category
        super().__init__(f"GitHub write failed: {category}")

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.category!r})"


class GitHubWrite:
    def __init__(
        self,
        credential_resolver: GitHubCredentialResolverPort,
        credential_reference: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not isinstance(credential_resolver, GitHubCredentialResolverPort):
            raise GitHubCredentialError()
        self._resolver = credential_resolver
        self._reference = validate_github_credential_reference(credential_reference)
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False)
        self._owns_client = client is None
        self._cache: OrderedDict[str, tuple[str, bytes]] = OrderedDict()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(credential_configured=True)"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def find_pull_requests(
        self, repository: str, head_repository: str, head_ref: str, base_ref: str
    ) -> tuple[GitHubPullRequest, ...]:
        repository, head_repository, head_ref, base_ref = (
            _repository(repository),
            _repository(head_repository),
            _ref(head_ref),
            _ref(base_ref),
        )
        found: list[GitHubPullRequest] = []
        for page in range(1, _MAX_PAGES + 1):
            values = _list(
                await self._json(
                    "GET",
                    _path(repository, "pulls"),
                    params={
                        "state": "all",
                        "per_page": "100",
                        "page": str(page),
                        "head": f"{head_repository.split('/', 1)[0]}:{head_ref}",
                        "base": base_ref,
                    },
                )
            )
            for value in values:
                summary = _mapping(value)
                head, base = _mapping(summary.get("head")), _mapping(summary.get("base"))
                if _ref(head.get("ref")) != head_ref or _ref(base.get("ref")) != base_ref:
                    continue
                if _repository(_mapping(head.get("repo")).get("full_name")) != head_repository:
                    continue
                if _repository(_mapping(base.get("repo")).get("full_name")) != repository:
                    raise GitHubWriteError("malformed_response")
                # List responses use pull-request-simple, without `merged`.
                # Rehydrate matching immutable identity from the detail endpoint.
                pull = await self.get_pull_request(
                    repository, _number_response(summary.get("number"))
                )
                if (
                    pull.node_id != _text(summary.get("node_id"), "malformed_response")
                    or pull.head_repository != head_repository
                    or pull.head_ref != head_ref
                    or pull.base_ref != base_ref
                ):
                    raise GitHubWriteError("malformed_response")
                found.append(pull)
            if len(values) < 100:
                return tuple(found)
        raise GitHubWriteError("pagination_limit")

    async def create_pull_request(
        self,
        repository: str,
        head_repository: str,
        head_ref: str,
        base_ref: str,
        title: str,
        body: str | None,
    ) -> GitHubPullRequest:
        repository, head_repository, head_ref, base_ref = (
            _repository(repository),
            _repository(head_repository),
            _ref(head_ref),
            _ref(base_ref),
        )
        payload: dict[str, object] = {
            "title": _text(title, "invalid_request"),
            "head": f"{head_repository.split('/', 1)[0]}:{head_ref}",
            "base": base_ref,
        }
        if body is not None:
            payload["body"] = _text(body, "invalid_request")
        return _pull_request(
            await self._json("POST", _path(repository, "pulls"), json=payload), repository
        )

    async def get_pull_request(
        self, repository: str, pull_request_number: int
    ) -> GitHubPullRequest:
        repository = _repository(repository)
        pull = _pull_request(
            await self._json("GET", _path(repository, "pulls", _number(pull_request_number))),
            repository,
        )
        if pull.number != pull_request_number:
            raise GitHubWriteError("malformed_response")
        return pull

    async def get_branch_sha(self, repository: str, ref: str) -> str:
        repository, ref = _repository(repository), _ref(ref)
        data = _mapping(await self._json("GET", _path(repository, "git", "ref", "heads", ref)))
        return _sha(_mapping(data.get("object")).get("sha"), "malformed_response")

    async def update_branch(
        self, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> GitHubPullRequest:
        repository = _repository(repository)
        number = _number(pull_request_number)
        # GitHub returns only an acknowledgement for this endpoint.  The
        # controller needs the canonical PR state, so read it after success.
        await self._json(
            "PUT",
            _path(repository, "pulls", number, "update-branch"),
            json={"expected_head_sha": _sha(expected_head_sha, "invalid_request")},
        )
        return await self._read_after_write(repository, number)

    async def merge_pull_request(
        self, repository: str, pull_request_number: int, expected_head_sha: str, merge_method: str
    ) -> GitHubPullRequest:
        repository = _repository(repository)
        if merge_method not in _MERGE_METHODS:
            raise GitHubWriteError("invalid_request")
        data = await self._json(
            "PUT",
            _path(repository, "pulls", _number(pull_request_number), "merge"),
            json={"sha": _sha(expected_head_sha, "invalid_request"), "merge_method": merge_method},
        )
        # The merge endpoint returns a different shape; read the authoritative PR identity.
        if not _bool(_mapping(data).get("merged"), "malformed_response"):
            raise GitHubWriteError("stale")
        return await self._read_after_write(repository, pull_request_number)

    async def _read_after_write(
        self, repository: str, pull_request_number: int
    ) -> GitHubPullRequest:
        try:
            return await self.get_pull_request(repository, pull_request_number)
        except GitHubWriteError:
            # The preceding write is durable-but-ambiguous until the controller
            # reconciles it with a later read.
            raise GitHubWriteError("uncertain") from None

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        if (
            method not in {"GET", "POST", "PUT"}
            or not path.startswith("/")
            or path.startswith("//")
        ):
            raise GitHubWriteError("invalid_request")
        try:
            token = await self._resolver.resolve(self._reference)
            token = _token(token)
        except asyncio.CancelledError:
            raise
        except GitHubCredentialError, OSError, RuntimeError, TypeError, ValueError:
            raise GitHubWriteError("credential") from None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "forge-github-write-adapter",
            "Authorization": "Bearer " + token,
        }
        key = (
            _cache_key(method, _API + path, kwargs.get("params"), token)
            if method == "GET"
            else None
        )
        cached = self._cache.get(key) if key else None
        if cached:
            headers["If-None-Match"] = cached[0]
        if method != "GET":
            # Even a failed transport may have completed the write remotely.
            self._cache.clear()
        try:
            async with asyncio.timeout(_REQUEST_SECONDS):
                response = await self._client.send(
                    self._client.build_request(method, _API + path, headers=headers, **kwargs),
                    stream=True,
                    follow_redirects=False,
                )
                raw = await _bounded_body(response)
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError, TimeoutError:
            # A write may have reached GitHub: controller must reconcile rather than retry it.
            raise GitHubWriteError(
                "uncertain" if method in {"POST", "PUT"} else "unavailable"
            ) from None
        if response.status_code == 304:
            if cached is None:
                raise GitHubWriteError("invalid_response")
            return json.loads(cached[1])
        if response.is_redirect:
            raise GitHubWriteError("untrusted_redirect")
        if method == "GET" and response.status_code == 429:
            raise GitHubWriteError("rate_limited")
        if method == "GET" and response.status_code >= 500:
            raise GitHubWriteError("unavailable")
        if response.status_code == 409:
            raise GitHubWriteError("stale")
        if response.status_code in {401, 403}:
            raise GitHubWriteError("permission")
        if response.status_code == 404:
            raise GitHubWriteError("not_found")
        if method == "PUT" and path.endswith("/merge") and response.status_code in {400, 405, 422}:
            raise GitHubWriteError("rejected")
        if response.status_code >= 400:
            raise GitHubWriteError("uncertain" if method in {"POST", "PUT"} else "invalid_response")
        try:
            data = json.loads(raw)
        except TypeError, ValueError, UnicodeError:
            raise GitHubWriteError("malformed_response") from None
        if key:
            if etag := response.headers.get("ETag"):
                self._cache[key] = (etag, raw)
                self._cache.move_to_end(key)
                while len(self._cache) > _MAX_CACHE:
                    self._cache.popitem(last=False)
            else:
                self._cache.pop(key, None)
        return data


async def _bounded_body(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > _MAX_RESPONSE_BYTES:
                raise GitHubWriteError("response_too_large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        await response.aclose()


def _path(repository: str, *parts: str | int) -> str:
    owner, name = repository.split("/", 1)
    return (
        "/repos/"
        + quote(owner, safe="")
        + "/"
        + quote(name, safe="")
        + "/"
        + "/".join(quote(str(part), safe="") for part in parts)
    )


def _repository(value: object) -> str:
    if type(value) is not str or value != value.strip() or value.count("/") != 1:
        raise GitHubWriteError("invalid_request")
    owner, name = value.split("/", 1)
    if (
        not owner
        or not name
        or owner in {".", ".."}
        or name in {".", ".."}
        or any(c in value for c in "?#\\\r\n")
    ):
        raise GitHubWriteError("invalid_request")
    return value


def _ref(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or value in {".", ".."}
        or any(c in value for c in "?#\\\r\n")
    ):
        raise GitHubWriteError("invalid_request")
    return value


def _number(value: object) -> int:
    if type(value) is not int or value < 1:
        raise GitHubWriteError("invalid_request")
    return value


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubWriteError("malformed_response")
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise GitHubWriteError("malformed_response")
    return value


def _text(value: object, category: str) -> str:
    if type(value) is not str or not value:
        raise GitHubWriteError(category)
    return value


def _token(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 4096
        or any(character.isspace() or not character.isascii() for character in value)
    ):
        raise GitHubCredentialError()
    return value


def _sha(value: object, category: str) -> str:
    value = _text(value, category)
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise GitHubWriteError(category)
    return value


def _bool(value: object, category: str) -> bool:
    if type(value) is not bool:
        raise GitHubWriteError(category)
    return value


def _url(value: object, repository: str, number: int) -> str:
    text = _text(value, "malformed_response")
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/{repository}/pull/{number}"
    ):
        raise GitHubWriteError("malformed_response")
    return text


def _pull_request(value: object, repository: str) -> GitHubPullRequest:
    data = _mapping(value)
    number = _number_response(data.get("number"))
    head, base = _mapping(data.get("head")), _mapping(data.get("base"))
    head_repository = _repository(_mapping(head.get("repo")).get("full_name"))
    base_repository = _repository(_mapping(base.get("repo")).get("full_name"))
    if base_repository != repository:
        raise GitHubWriteError("malformed_response")
    merged = _bool(data.get("merged"), "malformed_response")
    merge_sha = data.get("merge_commit_sha")
    if merge_sha is not None:
        merge_sha = _sha(merge_sha, "malformed_response")
    if merged and merge_sha is None:
        raise GitHubWriteError("malformed_response")
    return GitHubPullRequest(
        number,
        _text(data.get("node_id"), "malformed_response"),
        _url(data.get("html_url"), repository, number),
        head_repository,
        _ref(head.get("ref")),
        _sha(head.get("sha"), "malformed_response"),
        base_repository,
        _ref(base.get("ref")),
        _sha(base.get("sha"), "malformed_response"),
        _text(data.get("state"), "malformed_response"),
        merged,
        merge_sha,
    )


def _number_response(value: object) -> int:
    if type(value) is not int or value < 1:
        raise GitHubWriteError("malformed_response")
    return value


__all__ = ["GitHubWrite", "GitHubWriteError"]
