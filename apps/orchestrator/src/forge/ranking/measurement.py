"""Aggregate recorded search-ranking evidence into an A/B comparison.

Every repository search records what the reader offered, what the agent
received, and what a ranking would have returned.  That makes the measurement a
pure function of durable evidence: no separate metrics pipeline, and a run
measured with ranking off is directly comparable to the same fixtures measured
with it on.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from forge.application.ports.tools import ToolCallRecord
from forge.domain.tool import ToolCallStatus, ToolName


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchRankingMeasurement:
    """One mode's totals across every successful repository search."""

    mode: str
    searches: int = 0
    ranked: int = 0
    unavailable: int = 0
    matches_offered: int = 0
    matches_delivered: int = 0
    projected_delivered: int = 0
    ranker_input_units: int = 0
    ranker_output_units: int = 0
    ranker_duration_ms: int = 0

    @property
    def matches_withheld(self) -> int:
        """Matches the reader offered that the agent did not receive."""

        return max(0, self.matches_offered - self.matches_delivered)

    @property
    def delivered_fraction(self) -> float:
        """Share of offered matches the agent actually received."""

        if self.matches_offered == 0:
            return 1.0
        return self.matches_delivered / self.matches_offered

    @property
    def projected_fraction(self) -> float:
        """Share the agent would have received had this ranking been applied.

        In shadow mode this is the estimate of what turning ranking on would do;
        with ranking already on it equals the delivered share.
        """

        if self.matches_offered == 0:
            return 1.0
        return self.projected_delivered / self.matches_offered


def measure_search_ranking(
    records: Iterable[ToolCallRecord],
) -> tuple[SearchRankingMeasurement, ...]:
    """Summarize recorded searches, one entry per observed ranking mode."""

    totals: dict[str, dict[str, int]] = {}
    for record in records:
        ranking = _ranking_of(record)
        if ranking is None:
            continue
        mode = ranking.get("mode")
        if not isinstance(mode, str):
            continue
        bucket = totals.setdefault(mode, {})
        offered = _count(ranking.get("match_count"))
        delivered = _count(ranking.get("returned_count"))
        bucket["searches"] = bucket.get("searches", 0) + 1
        bucket["matches_offered"] = bucket.get("matches_offered", 0) + offered
        bucket["matches_delivered"] = bucket.get("matches_delivered", 0) + delivered
        # Shadow mode leaves the result untouched but records what it would
        # have returned, which is what makes it a projection and not a guess.
        projected = (
            _count(ranking.get("would_return_count"))
            if "would_return_count" in ranking
            else delivered
        )
        bucket["projected_delivered"] = bucket.get("projected_delivered", 0) + projected
        if ranking.get("status") == "unavailable":
            bucket["unavailable"] = bucket.get("unavailable", 0) + 1
        if ranking.get("status") == "ranked":
            bucket["ranked"] = bucket.get("ranked", 0) + 1
            bucket["ranker_input_units"] = bucket.get("ranker_input_units", 0) + _count(
                ranking.get("input_units")
            )
            bucket["ranker_output_units"] = bucket.get("ranker_output_units", 0) + _count(
                ranking.get("output_units")
            )
            bucket["ranker_duration_ms"] = bucket.get("ranker_duration_ms", 0) + _count(
                ranking.get("duration_ms")
            )
    return tuple(
        SearchRankingMeasurement(mode=mode, **values) for mode, values in sorted(totals.items())
    )


def format_measurements(measurements: Sequence[SearchRankingMeasurement]) -> str:
    """Render one human-readable line per mode for the operator CLI."""

    if not measurements:
        return "No repository searches were recorded."
    lines = []
    for item in measurements:
        lines.append(
            f"mode={item.mode} searches={item.searches} ranked={item.ranked} "
            f"unavailable={item.unavailable} "
            f"matches={item.matches_delivered}/{item.matches_offered} "
            f"delivered={item.delivered_fraction:.0%} "
            f"projected={item.projected_fraction:.0%} "
            f"ranker_units={item.ranker_input_units}+{item.ranker_output_units} "
            f"ranker_ms={item.ranker_duration_ms}"
        )
    return "\n".join(lines)


def _ranking_of(record: ToolCallRecord) -> Mapping[str, object] | None:
    if record.tool_name is not ToolName.REPOSITORY_SEARCH:
        return None
    if record.status is not ToolCallStatus.SUCCEEDED:
        return None
    metadata = record.result_metadata
    if not isinstance(metadata, Mapping):
        return None
    ranking = metadata.get("ranking")
    return ranking if isinstance(ranking, Mapping) else None


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


__all__ = [
    "SearchRankingMeasurement",
    "format_measurements",
    "measure_search_ranking",
]
