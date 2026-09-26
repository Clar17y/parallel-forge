"""Live TypeSafe contract check.

Skipped unless TYPESAFE_API_KEY is set and the live_provider marker is
selected, so the default suite stays offline and deterministic:

    python -m pytest apps/orchestrator/tests/ranking -m live_provider -q
"""

from __future__ import annotations

import os

import pytest
from forge.application.ports.repository import SearchMatch
from forge.application.ports.search_ranking import SearchRankingRequest
from forge.domain.actor import AgentRole
from forge.ranking.typesafe import API_KEY_VARIABLE, TypeSafeSearchRanker

pytestmark = [
    pytest.mark.live_provider,
    pytest.mark.skipif(
        not os.environ.get(API_KEY_VARIABLE, "").strip(),
        reason=f"{API_KEY_VARIABLE} is not configured",
    ),
]

OBJECTIVE = (
    "The delivery worker retries too aggressively after a provider timeout. "
    "Change the retry backoff so it grows between attempts."
)

# The relevant implementation, a same-word coincidence, and unrelated prose.
MATCHES = (
    SearchMatch(
        path="worker/delivery_runtime.py",
        line_number=88,
        line_text="    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)",
    ),
    SearchMatch(
        path="docs/glossary.md",
        line_number=12,
        line_text="Backoff: the delay a client waits before retrying a request.",
    ),
    SearchMatch(
        path="tests/fixtures/sample_payload.json",
        line_number=3,
        line_text='  "backoff": "unused test fixture value",',
    ),
)


async def test_live_ranking_prefers_the_implementation_over_a_coincidence() -> None:
    ranker = TypeSafeSearchRanker.from_environment()
    assert ranker is not None
    try:
        ranking = await ranker.rank(
            SearchRankingRequest(
                objective=OBJECTIVE,
                literal="backoff",
                path=".",
                role=AgentRole.DEVELOPER,
                matches=MATCHES,
            )
        )
    finally:
        await ranker.aclose()

    assert len(ranking.ranked) == len(MATCHES)
    assert ranking.input_tokens > 0
    # The implementation line must outrank the unused JSON fixture value.
    scores = {item.index: item.relevance for item in ranking.ranked}
    assert scores[0] > scores[2]
    assert ranking.ordered(len(MATCHES))[0] == 0
