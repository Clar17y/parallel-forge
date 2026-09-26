"""The TypeSafe ranker bounds, redacts, and never escapes its own error type."""

from __future__ import annotations

import json

import httpx
import pytest
from forge.application.ports.repository import SearchMatch
from forge.application.ports.search_ranking import SearchRankingRequest, SearchRankingUnavailable
from forge.domain.actor import AgentRole
from forge.ranking.typesafe import API_KEY_VARIABLE, TypeSafeSearchRanker

OBJECTIVE = "Fix the retry backoff used by the delivery worker."


def _request(matches: tuple[SearchMatch, ...] | None = None) -> SearchRankingRequest:
    return SearchRankingRequest(
        objective=OBJECTIVE,
        literal="backoff",
        path=".",
        role=AgentRole.PLANNER,
        matches=matches
        or (
            SearchMatch(path="retry.py", line_number=4, line_text="backoff = 2"),
            SearchMatch(path="vendor.py", line_number=9, line_text="backoff = 4"),
        ),
    )


def _ranker(handler, **kwargs) -> TypeSafeSearchRanker:
    return TypeSafeSearchRanker(
        api_key="test-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def _answers(*scores: float) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "jev-1.13",
            "request_id": "req_abc",
            "answers": {
                f"m{index}": {"type": "score", "score": score, "confidence": 0.8}
                for index, score in enumerate(scores)
            },
            "usage": {"input_tokens": 400, "output_tokens": 12},
        },
    )


async def test_one_score_question_per_match_is_sent_with_bearer_auth() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _answers(3.0, 0.0)

    await _ranker(handler).rank(_request())

    assert sent[0].headers["Authorization"] == "Bearer test-key"
    payload = json.loads(sent[0].content)
    assert payload["model"] == "jev-latest"
    assert set(payload["questions"]) == {"m0", "m1"}
    assert payload["questions"]["m0"]["type"] == "score"
    assert len(payload["questions"]["m0"]["criteria"]) == 4
    assert payload["state"]["objective"] == OBJECTIVE
    assert [match["path"] for match in payload["state"]["matches"]] == ["retry.py", "vendor.py"]


async def test_scores_become_normalized_relevance_in_reader_order() -> None:
    ranking = await _ranker(lambda _: _answers(3.0, 1.0)).rank(_request())

    assert [item.index for item in ranking.ranked] == [0, 1]
    assert ranking.ranked[0].relevance == 1.0
    assert ranking.ranked[1].relevance == pytest.approx(1 / 3)
    assert ranking.ordered(2) == (0, 1)
    assert ranking.model == "jev-1.13" and ranking.request_id == "req_abc"
    assert ranking.input_tokens == 400 and ranking.output_tokens == 12


async def test_a_lower_scored_first_match_is_ordered_behind_a_higher_one() -> None:
    ranking = await _ranker(lambda _: _answers(0.0, 3.0)).rank(_request())

    assert ranking.ordered(2) == (1, 0)


async def test_an_unscored_match_keeps_its_place_behind_scored_matches() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13",
                "answers": {"m1": {"type": "score", "score": 3.0, "confidence": 0.7}},
                "usage": {},
            },
        )

    ranking = await _ranker(handler).rank(_request())

    assert [item.index for item in ranking.ranked] == [1]
    assert ranking.ordered(2) == (1, 0)


async def test_repository_text_is_redacted_before_it_leaves_the_host() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _answers(1.0)

    matches = (
        SearchMatch(
            path="config.py",
            line_number=2,
            line_text='api_key = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"',
        ),
    )
    await _ranker(handler).rank(_request(matches))

    body = sent[0].content.decode()
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in body
    assert "REDACTED" in body


@pytest.mark.parametrize("status", (401, 422, 429, 500, 529))
async def test_every_error_status_becomes_an_unavailable_ranking(status: int) -> None:
    with pytest.raises(SearchRankingUnavailable):
        await _ranker(lambda _: httpx.Response(status, json={})).rank(_request())


@pytest.mark.parametrize(
    "body",
    (
        {"answers": {}},
        {"answers": {"m0": {"score": None, "confidence": 0.5}}},
        {"answers": {"m0": {"score": 2.0, "confidence": "high"}}},
        {"no_answers": True},
        [],
    ),
)
async def test_unusable_answers_become_an_unavailable_ranking(body: object) -> None:
    with pytest.raises(SearchRankingUnavailable):
        await _ranker(lambda _: httpx.Response(200, json=body)).rank(_request())


async def test_a_transport_failure_becomes_an_unavailable_ranking() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(SearchRankingUnavailable):
        await _ranker(handler).rank(_request())


def test_no_ranker_is_built_without_a_configured_key(monkeypatch) -> None:
    monkeypatch.delenv(API_KEY_VARIABLE, raising=False)
    assert TypeSafeSearchRanker.from_environment() is None

    monkeypatch.setenv(API_KEY_VARIABLE, "   ")
    assert TypeSafeSearchRanker.from_environment() is None

    monkeypatch.setenv(API_KEY_VARIABLE, "live-key")
    ranker = TypeSafeSearchRanker.from_environment()
    assert isinstance(ranker, TypeSafeSearchRanker)


def test_the_key_is_never_exposed_by_the_ranker_representation(monkeypatch) -> None:
    monkeypatch.setenv(API_KEY_VARIABLE, "super-secret-key")
    ranker = TypeSafeSearchRanker.from_environment()

    assert "super-secret-key" not in repr(ranker)
