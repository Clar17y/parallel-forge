"""Resolve the advisory search-ranking configuration for a worker process.

Ranking stays off unless an operator asks for it *and* a key is present, so a
missing key degrades to the deterministic reader result rather than failing a
run.  The resolved mode is what the tool evidence records, which keeps an A/B
comparison honest about what actually ran.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from forge.application.ports.search_ranking import SearchRankerPort, SearchRankingMode
from forge.observability.redaction import Redactor
from forge.ranking.typesafe import TypeSafeSearchRanker
from forge.settings import Settings


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchRankingConfiguration:
    """The effective ranking mode, bound to the ranker that will serve it."""

    mode: SearchRankingMode = SearchRankingMode.OFF
    top_k: int = 15
    ranker: SearchRankerPort | None = None

    def __post_init__(self) -> None:
        if type(self.top_k) is not int or not 0 < self.top_k <= 100:
            raise ValueError("search ranking top_k must be a bounded positive count")
        if self.mode is not SearchRankingMode.OFF and self.ranker is None:
            raise ValueError("an enabled ranking mode requires a ranker")

    @classmethod
    def from_settings(cls, settings: Settings, *, redactor: Redactor | None = None) -> Self:
        requested = SearchRankingMode(settings.search_ranking_mode)
        if requested is SearchRankingMode.OFF:
            return cls()
        ranker = TypeSafeSearchRanker.from_environment(
            model=settings.search_ranking_model, redactor=redactor
        )
        if ranker is None:
            return cls()
        return cls(mode=requested, top_k=settings.search_ranking_top_k, ranker=ranker)


__all__ = ["SearchRankingConfiguration"]
