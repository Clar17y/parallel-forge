"""Bounded, fail-closed GitHub REST/GraphQL read adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from forge.domain.github import (
    CheckSnapshot,
    GitHubIssue,
    MergeProtection,
    PullRequestSnapshot,
    ReviewSnapshot,
)
from forge.release.credentials import (
    GitHubCredentialError,
    GitHubCredentialResolverPort,
    validate_github_credential_reference,
)

_API = "https://api.github.com"
_MAX_RESPONSE_BYTES = 1_000_000
_MAX_PAGES = 20
_MAX_CACHE = 128
_RETRIES = 2
_MAX_BACKOFF_SECONDS = 60.0
_REQUEST_SECONDS = 15.0
_TIMEOUT = httpx.Timeout(10.0, connect=5.0, read=10.0, write=10.0, pool=5.0)


class GitHubClientError(RuntimeError):
    def __init__(self, category: str = "unavailable") -> None:
        super().__init__(f"GitHub read failed: {category}")
        self.category = category

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.category!r})"


class GitHubClient:
    def __init__(
        self,
        credential_resolver: GitHubCredentialResolverPort,
        credential_reference: str,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time,
    ) -> None:
        if not isinstance(credential_resolver, GitHubCredentialResolverPort):
            raise GitHubCredentialError()
        self._resolver = credential_resolver
        self._reference = validate_github_credential_reference(credential_reference)
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False)
        self._owns_client = client is None
        self._sleep = sleep
        self._now = now
        self._cache: OrderedDict[str, tuple[str, Any]] = OrderedDict()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(credential_configured=True)"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_issue(self, repository: str, issue_number: int) -> GitHubIssue:
        data = _mapping(
            await self._json("GET", self._path(repository, "issues", _number(issue_number)))
        )
        if _positive_int(data.get("number")) != issue_number or "pull_request" in data:
            raise GitHubClientError("malformed_response")
        return GitHubIssue(
            issue_number,
            _text(data.get("title")),
            _optional_text(data.get("body")),
            _github_issue_url(data.get("html_url"), repository, issue_number),
            _required_timestamp(data.get("updated_at")),
            _text(data.get("state")),
        )

    async def get_pull_request(self, repository: str, pull_number: int) -> PullRequestSnapshot:
        data = _mapping(
            await self._json("GET", self._path(repository, "pulls", _number(pull_number)))
        )
        if _positive_int(data.get("number")) != pull_number:
            raise GitHubClientError("malformed_response")
        head, base = _mapping(data.get("head")), _mapping(data.get("base"))
        return PullRequestSnapshot(
            pull_number,
            _text(data.get("state")),
            _sha(head.get("sha")),
            _text(head.get("ref")),
            _sha(base.get("sha")),
            _text(base.get("ref")),
            _bool(data.get("draft")),
            _nullable_bool(data.get("mergeable")),
        )

    async def get_checks(self, repository: str, ref: str) -> tuple[CheckSnapshot, ...]:
        sha = _sha(ref)
        runs = await self._pages(
            self._path(repository, "commits", sha, "check-runs"), key="check_runs"
        )
        checks = []
        for run in runs:
            if _sha(run.get("head_sha")) != sha:
                raise GitHubClientError("malformed_response")
            checks.append(
                CheckSnapshot(
                    _text(run.get("name")),
                    _text(run.get("status")),
                    _optional_text(run.get("conclusion")),
                    _optional_https_url(run.get("details_url")),
                    sha,
                )
            )
        seen = set()
        for status in await self._pages(self._path(repository, "commits", sha, "statuses")):
            context = _text(status.get("context"))
            if context not in seen:
                seen.add(context)
                checks.append(
                    CheckSnapshot(
                        f"status:{context}",
                        _text(status.get("state")),
                        _text(status.get("state")),
                        _optional_https_url(status.get("target_url")),
                        sha,
                    )
                )
        return tuple(checks)

    async def get_reviews(self, repository: str, pull_number: int) -> tuple[ReviewSnapshot, ...]:
        reviews = await self._pages(
            self._path(repository, "pulls", _number(pull_number), "reviews")
        )
        threads = await self._review_threads(repository, pull_number)
        unresolved = sum(not _bool(x.get("isResolved")) for x in threads)
        comments = sum(_nested_int(x, "comments", "totalCount") for x in threads)
        latest: dict[str, ReviewSnapshot] = {}
        for item in reviews:
            reviewer = _text(_mapping(item.get("user")).get("login"))
            state = _text(item.get("state")).casefold()
            if state not in {"approved", "changes_requested", "commented", "dismissed", "pending"}:
                raise GitHubClientError("malformed_response")
            candidate = ReviewSnapshot(
                reviewer, state, _timestamp(item.get("submitted_at")), state == "changes_requested"
            )
            prior = latest.get(reviewer.casefold())
            decisive = state in {"approved", "changes_requested"}
            prior_decisive = prior is not None and prior.state in {"approved", "changes_requested"}
            if (
                prior is None
                or (decisive and not prior_decisive)
                or (
                    decisive == prior_decisive
                    and _sort_time(candidate.submitted_at) >= _sort_time(prior.submitted_at)
                )
            ):
                latest[reviewer.casefold()] = candidate
        result = list(latest.values())
        if unresolved or comments or not result:
            result.append(
                ReviewSnapshot(
                    "github-review-threads",
                    "threads",
                    None,
                    unresolved_threads=unresolved,
                    comment_count=comments,
                )
            )
        return tuple(result)

    async def get_base(self, repository: str, branch: str) -> str:
        return _sha(
            _mapping(
                _mapping(
                    await self._json(
                        "GET", self._path(repository, "git", "ref", "heads", _ref(branch))
                    )
                ).get("object")
            ).get("sha")
        )

    async def get_merge_protection(self, repository: str, branch: str) -> MergeProtection:
        try:
            protection = None
            try:
                protection = _mapping(
                    await self._json(
                        "GET", self._path(repository, "branches", _ref(branch), "protection")
                    )
                )
            except GitHubClientError as error:
                if error.category != "not_found":
                    raise
            strict = False
            enforced = False
            allowances: dict[str, Any] = {"users": [], "teams": [], "apps": []}
            if protection is not None:
                checks = protection.get("required_status_checks")
                if checks is not None:
                    strict = _bool(_mapping(checks).get("strict")) and bool(
                        _list(_mapping(checks).get("contexts"))
                    )
                enforced = _bool(_mapping(protection.get("enforce_admins")).get("enabled"))
                reviews = protection.get("required_pull_request_reviews")
                if reviews is not None:
                    allowances = _mapping(_mapping(reviews).get("bypass_pull_request_allowances"))
            actor = _mapping(await self._json("GET", "/user"))
            actor_id = _positive_int(actor.get("id"))
            login = _text(actor.get("login")).casefold()
            permission = _mapping(
                await self._json(
                    "GET", self._path(repository, "collaborators", login, "permission")
                )
            )
            role = _text(permission.get("permission"))
            if role not in {"admin", "maintain", "write", "triage", "read"}:
                return _unverified()
            admin = role == "admin"
            if _list(allowances.get("teams")) or _list(allowances.get("apps")):
                return _unverified()
            bypass = any(
                _text(_mapping(x).get("login")).casefold() == login
                for x in _list(allowances.get("users"))
            ) or (protection is not None and admin and not enforced)
            rule_strict, queue, verified, rule_bypass = await self._rulesets(
                repository, branch, actor_id
            )
            return MergeProtection(
                strict or rule_strict,
                queue,
                bypass or rule_bypass,
                "branch_protection_rulesets_and_actor",
                verified=verified,
            )
        except GitHubClientError:
            return _unverified()

    async def _rulesets(
        self, repository: str, branch: str, actor_id: int
    ) -> tuple[bool, bool, bool, bool]:
        queue = False
        strict = False
        effective = await self._pages(self._path(repository, "rules", "branches", _ref(branch)))
        rulesets: dict[int, set[str]] = {}
        for item in effective:
            rulesets.setdefault(_positive_int(item.get("ruleset_id")), set()).add(
                _text(item.get("type"))
            )
            queue = queue or item["type"] == "merge_queue"
            if item["type"] == "required_status_checks":
                parameters = _mapping(item.get("parameters"))
                strict = strict or (
                    _bool(parameters.get("strict_required_status_checks_policy"))
                    and bool(_list(parameters.get("required_status_checks")))
                )
        for ruleset_id, types in rulesets.items():
            rule = _mapping(
                await self._json(
                    "GET",
                    self._path(repository, "rulesets", ruleset_id),
                    params={"includes_parents": "true"},
                )
            )
            if (
                rule.get("id") != ruleset_id
                or rule.get("target") != "branch"
                or rule.get("enforcement") != "active"
                or not types.issubset(
                    {_text(_mapping(x).get("type")) for x in _list(rule.get("rules"))}
                )
            ):
                return False, False, False, False
            for entry in _list(rule.get("bypass_actors")):
                item = _mapping(entry)
                kind = _text(item.get("actor_type"))
                mode = _text(item.get("bypass_mode"))
                if mode not in {"always", "pull_request", "exempt"}:
                    return False, False, False, False
                if kind == "User" and _positive_int(item.get("actor_id")) == actor_id:
                    return strict, queue, True, True
                if kind != "User":
                    return False, False, False, False
        return strict, queue, True, False

    async def _review_threads(self, repository: str, pull_number: int) -> list[dict[str, Any]]:
        owner, name = _repository(repository)
        cursor = None
        result: list[dict[str, Any]] = []
        query = "query($owner:String!,$repo:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$repo){pullRequest(number:$number){reviewThreads(first:100,after:$cursor){nodes{isResolved comments{totalCount}} pageInfo{hasNextPage endCursor}}}}}"
        for _ in range(_MAX_PAGES):
            data = _mapping(
                await self._json(
                    "POST",
                    "/graphql",
                    json={
                        "query": query,
                        "variables": {
                            "owner": owner,
                            "repo": name,
                            "number": pull_number,
                            "cursor": cursor,
                        },
                    },
                )
            )
            if data.get("errors") is not None:
                raise GitHubClientError("malformed_response")
            con = _nested_mapping(data, "data", "repository", "pullRequest", "reviewThreads")
            result.extend(_mapping(x) for x in _list(con.get("nodes")))
            page = _mapping(con.get("pageInfo"))
            if not _bool(page.get("hasNextPage")):
                return result
            cursor = _text(page.get("endCursor"))
        raise GitHubClientError("pagination_limit")

    async def _pages(self, path: str, *, key: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            data = await self._json("GET", path, params={"per_page": "100", "page": str(page)})
            values = _list(_mapping(data).get(key)) if key else _list(data)
            out.extend(_mapping(x) for x in values)
            if len(values) < 100:
                return out
        raise GitHubClientError("pagination_limit")

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        if method not in {"GET", "POST"} or not path.startswith("/") or path.startswith("//"):
            raise GitHubClientError("invalid_request")
        try:
            token = await self._resolver.resolve(self._reference)
        except asyncio.CancelledError:
            raise
        except GitHubCredentialError, OSError, RuntimeError, TypeError, ValueError:
            raise GitHubClientError("credential") from None
        url = _API + path
        key = _cache_key(method, url, kwargs.get("params"), token) if method == "GET" else None
        cached = self._cache.get(key) if key else None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "forge-github-read-adapter",
            "Authorization": "Bearer " + token,
        }
        if cached:
            headers["If-None-Match"] = cached[0]
        for attempt in range(_RETRIES + 1):
            try:
                async with asyncio.timeout(_REQUEST_SECONDS):
                    response = await self._client.send(
                        self._client.build_request(method, url, headers=headers, **kwargs),
                        stream=True,
                        follow_redirects=False,
                    )
                    raw = await _bounded_body(response)
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError, TimeoutError:
                if attempt < _RETRIES:
                    await self._sleep(0.25 * (2**attempt))
                    continue
                raise GitHubClientError("unavailable") from None
            if response.status_code == 304 and cached:
                return cached[1]
            if response.is_redirect:
                raise GitHubClientError("untrusted_redirect")
            delay = _retry_delay(response, self._now())
            if response.status_code in {502, 503, 504} or delay is not None:
                if attempt < _RETRIES:
                    await self._sleep(delay if delay is not None else 0.25 * (2**attempt))
                    continue
                raise GitHubClientError("rate_limited" if delay is not None else "unavailable")
            if response.status_code in {401, 403}:
                raise GitHubClientError("forbidden")
            if response.status_code == 404:
                raise GitHubClientError("not_found")
            if response.status_code >= 400:
                raise GitHubClientError("invalid_response")
            try:
                data = json.loads(raw)
            except TypeError, ValueError, UnicodeError:
                raise GitHubClientError("malformed_response") from None
            if key and (etag := response.headers.get("ETag")):
                self._cache[key] = (etag, data)
                self._cache.move_to_end(key)
                while len(self._cache) > _MAX_CACHE:
                    self._cache.popitem(last=False)
            return data
        raise AssertionError

    @staticmethod
    def _path(repository: str, *parts: str | int) -> str:
        owner, name = _repository(repository)
        return (
            "/repos/"
            + quote(owner, safe="")
            + "/"
            + quote(name, safe="")
            + "/"
            + "/".join(quote(str(x), safe="") for x in parts)
        )


async def _bounded_body(response: httpx.Response) -> bytes:
    chunks = []
    size = 0
    try:
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > _MAX_RESPONSE_BYTES:
                raise GitHubClientError("response_too_large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        await response.aclose()


def _retry_delay(response: httpx.Response, now: float) -> float | None:
    if response.status_code != 403:
        return None
    try:
        if response.headers.get("Retry-After") is not None:
            return min(_MAX_BACKOFF_SECONDS, max(0.0, float(response.headers["Retry-After"])))
        if response.headers.get("X-RateLimit-Remaining") == "0":
            return min(
                _MAX_BACKOFF_SECONDS, max(0.0, float(response.headers["X-RateLimit-Reset"]) - now)
            )
    except ValueError:
        return None
    return None


def _cache_key(method: str, url: str, params: object, token: str) -> str:
    return (
        hashlib.sha256(token.encode()).hexdigest()
        + ":"
        + method
        + ":"
        + url
        + "?"
        + json.dumps(params or {}, sort_keys=True, separators=(",", ":"), default=str)
    )


def _unverified() -> MergeProtection:
    return MergeProtection(False, False, False, "unverified", verified=False)


def _repository(value: str) -> tuple[str, str]:
    if type(value) is not str or value != value.strip() or value.count("/") != 1:
        raise GitHubClientError("invalid_request")
    owner, name = value.split("/")
    if (
        not owner
        or not name
        or owner in {".", ".."}
        or name in {".", ".."}
        or any(c in value for c in "?#\\\r\n")
    ):
        raise GitHubClientError("invalid_request")
    return owner, name


def _ref(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or value in {".", ".."}
        or any(c in value for c in "?#\\\r\n")
    ):
        raise GitHubClientError("invalid_request")
    return value


def _number(v: int) -> int:
    if type(v) is not int or v < 1:
        raise GitHubClientError("invalid_request")
    return v


def _mapping(v: Any) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise GitHubClientError("malformed_response")
    return v


def _nested_mapping(v: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = v
    for key in keys:
        current = _mapping(current).get(key)
    return _mapping(current)


def _list(v: Any) -> list[Any]:
    if not isinstance(v, list):
        raise GitHubClientError("malformed_response")
    return v


def _text(v: object) -> str:
    if type(v) is not str or not v:
        raise GitHubClientError("malformed_response")
    return v


def _optional_text(v: Any) -> str | None:
    return None if v is None else _text(v)


def _bool(v: Any) -> bool:
    if type(v) is not bool:
        raise GitHubClientError("malformed_response")
    return v


def _nullable_bool(v: Any) -> bool | None:
    return None if v is None else _bool(v)


def _positive_int(v: Any) -> int:
    if type(v) is not int or v < 1:
        raise GitHubClientError("malformed_response")
    return v


def _sha(v: Any) -> str:
    text = _text(v)
    if len(text) != 40 or any(c not in "0123456789abcdef" for c in text):
        raise GitHubClientError("malformed_response")
    return text


def _optional_https_url(v: Any) -> str | None:
    if v is None:
        return None
    text = _text(v)
    if urlsplit(text).scheme != "https":
        raise GitHubClientError("malformed_response")
    return text


def _github_issue_url(v: Any, repository: str, number: int) -> str:
    text = _text(v)
    p = urlsplit(text)
    owner, repo = _repository(repository)
    if (
        p.scheme != "https"
        or p.hostname != "github.com"
        or p.username
        or p.password
        or p.query
        or p.fragment
        or p.path != f"/{owner}/{repo}/issues/{number}"
    ):
        raise GitHubClientError("malformed_response")
    return text


def _timestamp(v: Any) -> datetime | None:
    return None if v is None else _required_timestamp(v)


def _required_timestamp(v: Any) -> datetime:
    try:
        p = datetime.fromisoformat(_text(v))
    except ValueError:
        raise GitHubClientError("malformed_response") from None
    if p.tzinfo is None or p.utcoffset() is None:
        raise GitHubClientError("malformed_response")
    return p.astimezone(UTC)


def _nested_int(v: dict[str, Any], *keys: str) -> int:
    current: Any = v
    for key in keys:
        current = _mapping(current).get(key)
    return _positive_int(current) if current else 0


def _sort_time(v: datetime | None) -> datetime:
    return v or datetime.min.replace(tzinfo=UTC)


__all__ = ["GitHubClient", "GitHubClientError"]
