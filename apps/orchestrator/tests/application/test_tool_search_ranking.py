"""Advisory search ranking reorders and collapses without hiding evidence."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.ports.repository import SearchMatch
from forge.application.ports.search_ranking import (
    RankedMatch,
    SearchRanking,
    SearchRankingMode,
    SearchRankingRequest,
    SearchRankingUnavailable,
)
from forge.application.services.tools import ControlledToolService, _authorized_agent_role
from forge.domain.actor import AgentRole
from forge.domain.subscription import SpecialistPurpose
from forge.domain.tool import (
    SubscriptionToolAuthorizationContext,
    ToolAuthorization,
    ToolCallStatus,
    ToolName,
    ToolRequest,
)
from forge.tools.repository import RepositoryReader
from test_planner_tools import (  # type: ignore[import-not-found]
    RUN_ID,
    TASK_ID,
    _context,
    _project,
    _TrackingReader,
    _UnitOfWork,
)

OBJECTIVE = "Fix the retry backoff used by the delivery worker."


class _Ranker:
    """Rank by a fixed path order so expectations stay deterministic."""

    def __init__(self, order: tuple[str, ...], *, error: Exception | None = None) -> None:
        self._order = order
        self._error = error
        self.requests: list[SearchRankingRequest] = []

    async def rank(self, request: SearchRankingRequest) -> SearchRanking:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        ranked = []
        for index, match in enumerate(request.matches):
            position = (
                self._order.index(match.path) if match.path in self._order else len(self._order)
            )
            ranked.append(
                RankedMatch(
                    index=index,
                    relevance=round(1.0 - position / (len(self._order) + 1), 3),
                    confidence=0.9,
                )
            )
        return SearchRanking(
            ranked=tuple(ranked),
            model="jev-latest",
            request_id="req_test",
            input_tokens=120,
            output_tokens=30,
            duration_ms=40,
        )


def _repository(tmp_path: Path) -> None:
    (tmp_path / "retry.py").write_text("backoff = 2  # target\n", encoding="utf-8")
    (tmp_path / "worker.py").write_text("backoff = 3\n", encoding="utf-8")
    (tmp_path / "vendor.py").write_text("backoff = 4\n", encoding="utf-8")


def _service(
    tmp_path: Path,
    ranker: _Ranker | None,
    *,
    mode: SearchRankingMode = SearchRankingMode.ON,
    top_k: int = 2,
) -> tuple[ControlledToolService, _UnitOfWork]:
    reader = _TrackingReader(
        RepositoryReader(tmp_path, secret_paths=(".env",), force_python_search=True)
    )
    work = _UnitOfWork(_run(), _project(tmp_path))
    service = ControlledToolService(
        lambda: work,
        repository_reader=reader,
        search_ranker=ranker,
        search_ranking_mode=mode,
        search_ranking_top_k=top_k,
        search_objective=OBJECTIVE,
    )
    return service, work


def _run():  # type: ignore[no-untyped-def]
    from forge.domain.run import RunSnapshot, RunState
    from test_planner_tools import PROJECT_ID, RUN_ID, TASK_ID

    return RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=RunState.PLANNING,
        policy_version=1,
    )


async def _search(service: ControlledToolService):  # type: ignore[no-untyped-def]
    return await service.invoke(
        _context(),
        ToolRequest(name=ToolName.REPOSITORY_SEARCH, arguments={"literal": "backoff", "path": "."}),
    )


async def test_ranking_on_returns_top_matches_first_and_collapses_the_tail(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    ranker = _Ranker(("retry.py", "worker.py", "vendor.py"))
    service, _ = _service(tmp_path, ranker, top_k=2)

    result = await _search(service)

    assert result.status is ToolCallStatus.SUCCEEDED
    assert [match["path"] for match in result.metadata["matches"]] == ["retry.py", "worker.py"]
    omitted = result.metadata["omitted_matches"]
    assert omitted["count"] == 1 and list(omitted["paths"]) == ["vendor.py"]
    ranking = result.metadata["ranking"]
    assert ranking["mode"] == "on" and ranking["applied"] is True
    assert ranking["match_count"] == 3 and ranking["returned_count"] == 2
    assert ranking["model"] == "jev-latest" and ranking["input_units"] == 120
    assert ranking["output_units"] == 30 and ranking["duration_ms"] == 40


async def test_shadow_mode_records_telemetry_but_returns_the_readers_own_order(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    ranker = _Ranker(("vendor.py", "worker.py", "retry.py"))
    service, _ = _service(tmp_path, ranker, mode=SearchRankingMode.SHADOW, top_k=1)

    result = await _search(service)

    assert [match["path"] for match in result.metadata["matches"]] == [
        "retry.py",
        "vendor.py",
        "worker.py",
    ]
    assert "omitted_matches" not in result.metadata
    ranking = result.metadata["ranking"]
    assert ranking["mode"] == "shadow" and ranking["applied"] is False
    assert ranking["returned_count"] == 3
    assert ranking["would_return_count"] == 1
    assert list(ranking["would_return_paths"]) == ["vendor.py"]


async def test_ranking_off_never_calls_the_ranker(tmp_path: Path) -> None:
    _repository(tmp_path)
    ranker = _Ranker(("retry.py",))
    service, _ = _service(tmp_path, ranker, mode=SearchRankingMode.OFF)

    result = await _search(service)

    assert ranker.requests == []
    assert len(result.metadata["matches"]) == 3
    assert result.metadata["ranking"]["mode"] == "off"
    assert result.metadata["ranking"]["match_count"] == 3


@pytest.mark.parametrize(
    "error", (SearchRankingUnavailable("down"), TimeoutError(), ValueError("bad"))
)
async def test_a_failing_ranker_fails_open_with_every_match(
    tmp_path: Path, error: Exception
) -> None:
    _repository(tmp_path)
    service, _ = _service(tmp_path, _Ranker(("retry.py",), error=error), top_k=1)

    result = await _search(service)

    assert result.status is ToolCallStatus.SUCCEEDED
    assert len(result.metadata["matches"]) == 3
    assert result.metadata["ranking"]["status"] == "unavailable"
    assert result.metadata["ranking"]["applied"] is False


class _FlatRanker:
    """Score every match the same, to exercise the relevance floor."""

    def __init__(self, relevance: float) -> None:
        self._relevance = relevance

    async def rank(self, request: SearchRankingRequest) -> SearchRanking:
        return SearchRanking(
            ranked=tuple(
                RankedMatch(index=index, relevance=self._relevance, confidence=0.9)
                for index in range(len(request.matches))
            ),
            model="jev-latest",
            request_id=None,
            input_tokens=10,
            output_tokens=1,
            duration_ms=5,
        )


async def test_nothing_is_withheld_when_no_match_clears_the_relevance_floor(
    tmp_path: Path,
) -> None:
    """An unrelated search must not trade matches away for no relevance."""

    _repository(tmp_path)
    service, _ = _service(tmp_path, _FlatRanker(0.2), top_k=1)

    result = await _search(service)

    assert len(result.metadata["matches"]) == 3
    assert "omitted_matches" not in result.metadata
    ranking = result.metadata["ranking"]
    assert ranking["status"] == "below_floor" and ranking["applied"] is False
    assert ranking["returned_count"] == 3


async def test_a_match_at_the_floor_still_allows_the_tail_to_collapse(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    service, _ = _service(tmp_path, _FlatRanker(1 / 3), top_k=1)

    result = await _search(service)

    assert len(result.metadata["matches"]) == 1
    assert result.metadata["ranking"]["applied"] is True


async def test_shadow_mode_projects_no_saving_below_the_floor(tmp_path: Path) -> None:
    _repository(tmp_path)
    service, _ = _service(tmp_path, _FlatRanker(0.2), mode=SearchRankingMode.SHADOW, top_k=1)

    result = await _search(service)

    ranking = result.metadata["ranking"]
    assert ranking["status"] == "below_floor"
    assert ranking["would_return_count"] == 3


async def test_the_ranker_receives_the_objective_and_only_authorized_matches(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    (tmp_path / ".env").write_text("backoff = 9\n", encoding="utf-8")
    ranker = _Ranker(("retry.py",))
    service, _ = _service(tmp_path, ranker)

    await _search(service)

    assert len(ranker.requests) == 1
    request = ranker.requests[0]
    assert request.objective == OBJECTIVE and request.literal == "backoff"
    assert all(type(match) is SearchMatch for match in request.matches)
    assert ".env" not in {match.path for match in request.matches}


async def test_ranking_is_skipped_when_the_reader_returns_nothing_to_rank(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    ranker = _Ranker(("retry.py",))
    service, _ = _service(tmp_path, ranker)

    result = await service.invoke(
        _context(),
        ToolRequest(name=ToolName.REPOSITORY_SEARCH, arguments={"literal": "absent", "path": "."}),
    )

    assert list(result.metadata["matches"]) == []
    assert ranker.requests == []


@pytest.mark.parametrize(
    ("purpose", "expected_role"),
    [
        (SpecialistPurpose.PRIMARY, AgentRole.DEVELOPER),
        (SpecialistPurpose.ROUTINE_IMPLEMENTATION, AgentRole.DEVELOPER),
        (SpecialistPurpose.COMPLEX_IMPLEMENTATION, AgentRole.DEVELOPER),
        (SpecialistPurpose.INTEGRATION, AgentRole.DEVELOPER),
        (SpecialistPurpose.EXPLORATION, AgentRole.DEVELOPER),
        (SpecialistPurpose.PLANNING, AgentRole.PLANNER),
        (SpecialistPurpose.INDEPENDENT_REVIEW, AgentRole.REVIEWER),
        (SpecialistPurpose.SECURITY, AgentRole.REVIEWER),
        (SpecialistPurpose.VERIFICATION, AgentRole.REVIEWER),
    ],
)
def test_subscription_role_adaptation_maps_all_specialist_purposes(
    purpose: SpecialistPurpose,
    expected_role: AgentRole,
) -> None:
    context = SubscriptionToolAuthorizationContext(
        run_id=RUN_ID,
        task_id=TASK_ID,
        attempt_id=uuid4(),
        worktree_id="forge-test",
        purpose=purpose,
        policy_version=1,
        permitted_tools=frozenset({ToolName.REPOSITORY_SEARCH}),
        invocation_id=uuid4(),
    )
    authorization = ToolAuthorization(
        context=context,
        request=ToolRequest(name=ToolName.REPOSITORY_SEARCH, arguments={"literal": "x"}),
    )
    role = _authorized_agent_role(authorization)
    assert role is not None
    assert role is expected_role
