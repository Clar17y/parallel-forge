"""Advisory relevance ranking for bounded repository search results.

Ranking is presentation only.  It never changes which repository content an
agent is permitted to reach: containment, secret exclusion, and authorization
stay with the deterministic reader and authorizer.  A ranker reorders matches
the reader already returned and may collapse a low-relevance tail into a
bounded summary, so the omitted paths remain visible and reachable by a
narrower search.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from forge.application.ports.repository import SearchMatch
from forge.domain.actor import AgentRole

MAX_RANKED_MATCHES = 100
MAX_OBJECTIVE_BYTES = 4096


class SearchRankingMode(StrEnum):
    """How a computed ranking is allowed to affect the returned result."""

    OFF = "off"
    SHADOW = "shadow"
    ON = "on"


class SearchRankingUnavailable(RuntimeError):
    """The ranker produced no usable ranking; the caller must fail open."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchRankingRequest:
    """One bounded ranking question over already-authorized matches."""

    objective: str
    literal: str
    path: str
    role: AgentRole
    matches: tuple[SearchMatch, ...]

    def __post_init__(self) -> None:
        if type(self.objective) is not str or type(self.literal) is not str:
            raise TypeError("ranking request text must be str")
        if len(self.objective.encode("utf-8")) > MAX_OBJECTIVE_BYTES:
            raise ValueError("ranking objective exceeds its byte bound")
        if not 0 < len(self.matches) <= MAX_RANKED_MATCHES:
            raise ValueError("ranking requires a bounded nonempty match set")


@dataclass(frozen=True, slots=True, kw_only=True)
class RankedMatch:
    """One scored match, identified by its index in the reader's result."""

    index: int
    relevance: float
    confidence: float

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("ranked match index must be a result position")
        for value in (self.relevance, self.confidence):
            if type(value) is not float or not 0.0 <= value <= 1.0:
                raise ValueError("ranked match values must be probabilities")


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchRanking:
    """A complete ranking with the telemetry needed to measure its value."""

    ranked: tuple[RankedMatch, ...]
    model: str
    request_id: str | None
    input_tokens: int
    output_tokens: int
    duration_ms: int

    def __post_init__(self) -> None:
        indexes = [item.index for item in self.ranked]
        if len(set(indexes)) != len(indexes):
            raise ValueError("a ranking must not repeat a match position")
        if not self.model or len(self.model) > 128:
            raise ValueError("a ranking must name its bounded model")
        for value in (self.input_tokens, self.output_tokens, self.duration_ms):
            if type(value) is not int or value < 0:
                raise ValueError("ranking telemetry must be nonnegative counts")

    def ordered(self, total: int) -> tuple[int, ...]:
        """Return every result position, most relevant first.

        Positions the ranker did not score keep their reader order behind the
        scored ones, so a partial ranking can never drop a match.
        """

        scored = sorted(
            (item for item in self.ranked if item.index < total),
            key=lambda item: (-item.relevance, item.index),
        )
        seen = {item.index for item in scored}
        return tuple(item.index for item in scored) + tuple(
            index for index in range(total) if index not in seen
        )


class SearchRankerPort(Protocol):
    """Rank authorized search matches against the agent's current objective."""

    async def rank(self, request: SearchRankingRequest) -> SearchRanking: ...


__all__ = [
    "MAX_OBJECTIVE_BYTES",
    "MAX_RANKED_MATCHES",
    "RankedMatch",
    "SearchRankerPort",
    "SearchRanking",
    "SearchRankingMode",
    "SearchRankingRequest",
    "SearchRankingUnavailable",
]
