"""Narrow advisory Jev contract. Source text never enters durable records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

from forge.domain.policy import JevPolicy

JevKind = Literal["search_ranking", "semantic_search", "review_focus"]
JevStatus = Literal[
    "off", "unavailable", "budget_exhausted", "ranked", "succeeded", "cached", "unknown"
]
USABLE_JEV_STATUSES = frozenset({"ranked", "succeeded", "cached"})


@dataclass(frozen=True, slots=True, kw_only=True)
class JevRequest:
    run_id: UUID
    policy_version: int
    operation_key: str
    kind: JevKind
    worktree_digest: str
    state: dict[str, Any]
    questions: dict[str, Any]
    candidate_digest: str | None = None
    scope_digest: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.run_id, UUID)
            or type(self.policy_version) is not int
            or self.policy_version < 1
        ):
            raise ValueError("Jev request needs a run and policy version")
        if (
            type(self.operation_key) is not str
            or not 0 < len(self.operation_key) <= 128
            or not self.operation_key.strip()
        ):
            raise ValueError("Jev operation key is invalid")
        if self.kind not in ("search_ranking", "semantic_search", "review_focus"):
            raise ValueError("Jev kind is invalid")
        for digest in (self.worktree_digest, self.candidate_digest, self.scope_digest):
            if digest is not None and (
                type(digest) is not str
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("Jev identity must be a SHA-256 hex digest")
        if (
            not isinstance(self.state, dict)
            or not isinstance(self.questions, dict)
            or not self.questions
        ):
            raise ValueError("Jev request needs state and questions")


@dataclass(frozen=True, slots=True, kw_only=True)
class JevResult:
    status: JevStatus
    answers: dict[str, dict[str, float | int]] = field(default_factory=dict)
    requested_model: str | None = None
    actual_model: str | None = None
    input_units: int = 0
    output_units: int = 0
    duration_ms: int = 0
    request_id: str | None = None
    diagnostic: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class JevProviderResponse:
    answers: dict[str, dict[str, float | int]]
    actual_model: str | None
    input_units: int | None
    output_units: int | None
    duration_ms: int
    request_id: str | None


class JevProvider(Protocol):
    async def evaluate(
        self,
        *,
        state: dict[str, Any],
        questions: dict[str, Any],
        model: str,
        timeout_seconds: float,
    ) -> JevProviderResponse: ...


class JevRepository(Protocol):
    async def policy_for_run(
        self, run_id: UUID, *, policy_version: int | None = None,
    ) -> JevPolicy | None: ...

    async def observe_unavailable(
        self, request: JevRequest, *, request_digest: str, policy: Any,
        diagnostic: str | None = None,
    ) -> None: ...
    async def reserve(
        self,
        request: JevRequest,
        *,
        request_digest: str,
        model: str,
        input_units: int,
        max_requests: int,
        max_input_units: int,
        mode: str,
        policy: Any,
    ) -> JevResult | None: ...
    async def settle(self, request: JevRequest, response: JevResult) -> None: ...
    async def summary(self, run_id: UUID, *, policy: Any = None) -> dict[str, Any]: ...


__all__ = [
    "USABLE_JEV_STATUSES",
    "JevKind",
    "JevProvider",
    "JevProviderResponse",
    "JevRepository",
    "JevRequest",
    "JevResult",
    "JevStatus",
]
