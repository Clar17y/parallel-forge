"""Recorded search evidence aggregates into a comparable A/B measurement."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from forge.application.ports.tools import ToolCallRecord
from forge.domain.tool import ToolCallStatus, ToolName
from forge.ranking.measurement import format_measurements, measure_search_ranking

RUN_ID = UUID("22222222-2222-4222-8222-222222222222")


def _record(
    ranking: dict[str, object] | None,
    *,
    tool_name: ToolName = ToolName.REPOSITORY_SEARCH,
    status: ToolCallStatus = ToolCallStatus.SUCCEEDED,
) -> ToolCallRecord:
    metadata: dict[str, object] = {"matches": []}
    if ranking is not None:
        metadata["ranking"] = ranking
    return ToolCallRecord(
        id=uuid4(),
        run_id=RUN_ID,
        agent_execution_id=uuid4(),
        tool_name=tool_name,
        normalized_arguments={"literal": "backoff"},
        authorized=True,
        status=status,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        result_metadata=metadata,
        result_metadata_schema_version=1,
    )


def test_ranking_off_reports_every_offered_match_as_delivered() -> None:
    records = [
        _record({"mode": "off", "match_count": 40, "returned_count": 40}),
        _record({"mode": "off", "match_count": 10, "returned_count": 10}),
    ]

    (measurement,) = measure_search_ranking(records)

    assert measurement.mode == "off" and measurement.searches == 2
    assert measurement.matches_offered == 50 and measurement.matches_delivered == 50
    assert measurement.matches_withheld == 0
    assert measurement.delivered_fraction == 1.0
    assert measurement.projected_fraction == 1.0
    assert measurement.ranked == 0 and measurement.ranker_input_units == 0


def test_shadow_mode_projects_savings_without_withholding_anything() -> None:
    records = [
        _record(
            {
                "mode": "shadow",
                "status": "ranked",
                "match_count": 100,
                "returned_count": 100,
                "would_return_count": 15,
                "input_units": 900,
                "output_units": 40,
                "duration_ms": 350,
            }
        )
    ]

    (measurement,) = measure_search_ranking(records)

    assert measurement.matches_delivered == 100 and measurement.matches_withheld == 0
    assert measurement.delivered_fraction == 1.0
    assert measurement.projected_delivered == 15
    assert measurement.projected_fraction == 0.15
    assert measurement.ranker_input_units == 900 and measurement.ranker_duration_ms == 350


def test_ranking_on_reports_the_matches_actually_withheld() -> None:
    records = [
        _record(
            {
                "mode": "on",
                "status": "ranked",
                "match_count": 80,
                "returned_count": 15,
                "input_units": 700,
                "output_units": 30,
                "duration_ms": 300,
            }
        ),
        _record({"mode": "on", "status": "unavailable", "match_count": 20, "returned_count": 20}),
    ]

    (measurement,) = measure_search_ranking(records)

    assert measurement.searches == 2 and measurement.ranked == 1 and measurement.unavailable == 1
    assert measurement.matches_offered == 100 and measurement.matches_delivered == 35
    assert measurement.matches_withheld == 65
    assert measurement.delivered_fraction == 0.35
    # A failed ranking still delivered everything, so it cannot be counted as a saving.
    assert measurement.projected_delivered == 35


def test_below_floor_ranking_counts_as_ranked_and_aggregates_units_and_duration() -> None:
    records = [
        _record(
            {
                "mode": "on",
                "status": "below_floor",
                "match_count": 40,
                "returned_count": 40,
                "input_units": 500,
                "output_units": 20,
                "duration_ms": 250,
            }
        ),
        _record(
            {
                "mode": "on",
                "status": "ranked",
                "match_count": 30,
                "returned_count": 10,
                "input_units": 300,
                "output_units": 15,
                "duration_ms": 150,
            }
        ),
        _record(
            {
                "mode": "on",
                "status": "unavailable",
                "match_count": 20,
                "returned_count": 20,
            }
        ),
    ]

    (measurement,) = measure_search_ranking(records)

    assert measurement.mode == "on"
    assert measurement.searches == 3
    assert measurement.ranked == 2
    assert measurement.unavailable == 1
    assert measurement.matches_offered == 90
    assert measurement.matches_delivered == 70
    assert measurement.matches_withheld == 20
    assert measurement.delivered_fraction == 70 / 90
    assert measurement.projected_delivered == 70
    assert measurement.projected_fraction == 70 / 90
    assert measurement.ranker_input_units == 800
    assert measurement.ranker_output_units == 35
    assert measurement.ranker_duration_ms == 400


def test_modes_are_reported_separately_and_other_tools_are_ignored() -> None:
    records = [
        _record({"mode": "off", "match_count": 10, "returned_count": 10}),
        _record({"mode": "on", "status": "ranked", "match_count": 10, "returned_count": 2}),
        _record(
            {"mode": "off", "match_count": 4, "returned_count": 4}, tool_name=ToolName.GIT_DIFF
        ),
        _record(
            {"mode": "off", "match_count": 4, "returned_count": 4}, status=ToolCallStatus.FAILED
        ),
        _record(None),
    ]

    off, on = measure_search_ranking(records)

    assert off.mode == "off" and off.searches == 1 and off.matches_offered == 10
    assert on.mode == "on" and on.searches == 1 and on.matches_delivered == 2


def test_the_report_names_both_delivered_and_projected_shares() -> None:
    records = [
        _record(
            {
                "mode": "shadow",
                "status": "ranked",
                "match_count": 100,
                "returned_count": 100,
                "would_return_count": 15,
            }
        )
    ]

    rendered = format_measurements(measure_search_ranking(records))

    assert "mode=shadow" in rendered
    assert "matches=100/100" in rendered
    assert "delivered=100%" in rendered
    assert "projected=15%" in rendered


def test_an_empty_measurement_says_so() -> None:
    assert (
        format_measurements(measure_search_ranking([])) == "No repository searches were recorded."
    )
