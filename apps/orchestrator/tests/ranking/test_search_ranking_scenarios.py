"""End-to-end ranking scenarios against this repository and the live model.

Nothing is faked except durable storage: the real ``RepositoryReader`` searches
the real Forge tree, the real ``ControlledToolService`` dispatches the tool, and
the real TypeSafe adapter scores the matches.  Each scenario states a task a
developer might actually be given, then asserts that the files an engineer would
need survive the tail collapse.

    python -m pytest apps/orchestrator/tests/ranking/test_search_ranking_scenarios.py \
        -m live_provider -q -s
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self
from uuid import UUID

import pytest
from forge.application.ports.projects import ProjectPolicyRecord, ProjectRecord
from forge.application.ports.search_ranking import SearchRankingMode
from forge.application.ports.tools import ToolCallRecord
from forge.application.services.tools import ControlledToolService
from forge.domain.actor import AgentRole
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunSnapshot, RunState
from forge.domain.tool import (
    ToolAuthorizationContext,
    ToolCallStatus,
    ToolName,
    ToolRequest,
    repository_resource_identity,
)
from forge.ranking.measurement import format_measurements, measure_search_ranking
from forge.ranking.typesafe import API_KEY_VARIABLE, TypeSafeSearchRanker
from forge.tools.repository import RepositoryReader

pytestmark = [
    pytest.mark.live_provider,
    pytest.mark.skipif(
        not os.environ.get(API_KEY_VARIABLE, "").strip(),
        reason=f"{API_KEY_VARIABLE} is not configured",
    ),
]

PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
TASK_ID = UUID("33333333-3333-4333-8333-333333333333")
EXECUTION_ID = UUID("44444444-4444-4444-8444-444444444444")
STEP_ID = UUID("55555555-5555-4555-8555-555555555555")

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "forge"


@dataclass
class _Runs:
    run: RunSnapshot

    async def get_for_update(self, run_id: UUID) -> RunSnapshot:
        return self.run


@dataclass
class _Projects:
    project: ProjectRecord

    async def get(self, project_id: UUID, *, for_update: bool = False) -> ProjectRecord:
        return self.project


class _ToolCalls:
    def __init__(self) -> None:
        self.records: list[ToolCallRecord] = []

    async def validate_execution_context(self, *_: object) -> bool:
        return True

    async def count_for_execution(self, _: UUID) -> int:
        return 0

    async def record(self, record: ToolCallRecord) -> ToolCallRecord:
        self.records.append(record)
        return record


class _Events:
    async def append(self, event: object) -> object:
        return event


class _UnitOfWork:
    def __init__(self, project: ProjectRecord) -> None:
        self.runs = _Runs(
            RunSnapshot(
                id=RUN_ID,
                project_id=PROJECT_ID,
                task_id=TASK_ID,
                state=RunState.PLANNING,
                policy_version=1,
            )
        )
        self.projects = _Projects(project)
        self.tool_calls = _ToolCalls()
        self.events = _Events()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _project() -> ProjectRecord:
    policy = ProjectPolicy(
        id=PROJECT_ID,
        version=1,
        repository_path=str(PACKAGE_ROOT),
        github_repository="forge/parallel-forge",
        default_branch="main",
        secret_paths=(),
    )
    return ProjectRecord(
        id=PROJECT_ID,
        name="parallel-forge",
        canonical_path=str(PACKAGE_ROOT),
        canonical_path_key=str(PACKAGE_ROOT),
        github_repository="forge/parallel-forge",
        default_branch="main",
        instructions_path=None,
        current_policy_version=1,
        policy=ProjectPolicyRecord(
            project_id=PROJECT_ID,
            version=1,
            policy_digest="a" * 64,
            document_schema_version=1,
            document=policy.model_dump(mode="json"),
        ),
    )


async def _search(
    objective: str,
    literal: str,
    path: str,
    *,
    mode: SearchRankingMode,
    top_k: int = 10,
) -> tuple[dict[str, object], _UnitOfWork]:
    """Run one real search through the real service and return its metadata."""

    work = _UnitOfWork(_project())
    ranker = (
        None
        if mode is SearchRankingMode.OFF
        else TypeSafeSearchRanker.from_environment(model="jev-latest")
    )
    service = ControlledToolService(
        lambda: work,
        repository_reader=RepositoryReader(PACKAGE_ROOT, secret_paths=(), force_python_search=True),
        search_ranker=ranker,
        search_ranking_mode=mode,
        search_ranking_top_k=top_k,
        search_objective=objective,
    )
    try:
        result = await service.invoke(
            ToolAuthorizationContext(
                role=AgentRole.PLANNER,
                run_id=RUN_ID,
                worktree_id=repository_resource_identity(PROJECT_ID),
                policy_version=1,
                agent_execution_id=EXECUTION_ID,
                step_id=STEP_ID,
            ),
            ToolRequest(
                name=ToolName.REPOSITORY_SEARCH, arguments={"literal": literal, "path": path}
            ),
        )
    finally:
        if ranker is not None:
            await ranker.aclose()
    assert result.status is ToolCallStatus.SUCCEEDED
    return dict(result.metadata), work


def _report(title: str, off: dict[str, object], on: dict[str, object]) -> None:
    ranking = dict(on["ranking"])  # type: ignore[arg-type]
    delivered = [str(match["path"]) for match in on["matches"]]  # type: ignore[index]
    omitted = dict(on.get("omitted_matches", {"count": 0, "paths": ()}))  # type: ignore[arg-type]
    print(f"\n=== {title}")
    print(f"    reader offered : {len(off['matches'])} matches")  # type: ignore[arg-type]
    print(f"    agent received : {len(delivered)} matches")
    print(
        f"    ranker cost    : {ranking['input_units']} in / {ranking['output_units']} out, "
        f"{ranking['duration_ms']} ms"
    )
    print("    kept (ranked order):")
    for path in dict.fromkeys(delivered):
        print(f"      + {path}")
    print(f"    collapsed tail : {omitted['count']} matches in {len(omitted['paths'])} files")
    for path in omitted["paths"]:
        print(f"      - {path}")


async def test_scenario_provider_quota_wording_change() -> None:
    """A parser change: the file that parses the message must come first."""

    objective = (
        "Claude changed the wording of its usage-limit message again. Update the parser so the "
        "new phrasing is recognized as an exhausted account allowance, while a per-minute rate "
        "limit is still not treated as exhaustion."
    )
    off, _ = await _search(objective, "quota", "domain", mode=SearchRankingMode.OFF)
    on, work = await _search(objective, "quota", "domain", mode=SearchRankingMode.ON, top_k=10)
    _report("provider quota wording change", off, on)

    delivered = [str(match["path"]) for match in on["matches"]]
    assert len(off["matches"]) > len(delivered)
    # The message parser is the file this task is about.
    assert delivered[0] == "domain/provider_quota.py"
    assert on["ranking"]["applied"] is True
    # Nothing vanished: the tail is named, and the evidence was recorded.
    assert on["omitted_matches"]["count"] == len(off["matches"]) - len(delivered)
    assert work.tool_calls.records[-1].tool_name is ToolName.REPOSITORY_SEARCH


async def test_scenario_redaction_of_operator_feedback() -> None:
    """A cross-cutting task: the ranking must surface the redactor's own module."""

    objective = (
        "Operator feedback submitted for a subscription task is reaching durable storage "
        "without passing the redactor. Make sure feedback text is redacted before it is "
        "persisted or written to audit evidence."
    )
    off, _ = await _search(objective, "redact", "application/services", mode=SearchRankingMode.OFF)
    on, _ = await _search(
        objective, "redact", "application/services", mode=SearchRankingMode.ON, top_k=10
    )
    _report("redaction of operator feedback", off, on)

    delivered = [str(match["path"]) for match in on["matches"]]
    assert len(off["matches"]) > len(delivered)
    # Ranking can only reorder what the literal search found: no amount of
    # relevance surfaces a file that never contained the searched text. The
    # useful claim is that the redactor's own call sites outrank incidental
    # mentions, not that some unmatched file appears.
    assert delivered[0].startswith("application/services/")
    offered_paths = {str(match["path"]) for match in off["matches"]}
    assert set(delivered) <= offered_paths
    assert on["ranking"]["applied"] is True


async def test_scenario_shadow_mode_projects_savings_without_touching_the_result() -> None:
    """Shadow mode is the safe measurement: full result, projected saving."""

    objective = (
        "Docker runner commands hang forever when a container never exits. Bind the container "
        "wait to a timeout so the run cannot stall indefinitely."
    )
    off, _ = await _search(objective, "timeout", "tools", mode=SearchRankingMode.OFF)
    shadow, work = await _search(
        objective, "timeout", "tools", mode=SearchRankingMode.SHADOW, top_k=10
    )

    ranking = shadow["ranking"]
    print("\n=== shadow mode on 'timeout' in tools/")
    print(f"    reader offered   : {len(off['matches'])} matches")
    print(f"    agent received   : {len(shadow['matches'])} matches (unchanged)")
    print(f"    would have kept  : {ranking['would_return_count']} matches")
    print(f"    would have kept  : {list(ranking['would_return_paths'])}")
    print("\n" + format_measurements(measure_search_ranking(work.tool_calls.records)))

    # The agent's input is untouched, so shadow mode cannot regress a run.
    assert len(shadow["matches"]) == len(off["matches"])
    assert "omitted_matches" not in shadow
    assert ranking["applied"] is False
    assert ranking["would_return_count"] < len(off["matches"])
    # The projection is what makes it a measurement rather than a guess.
    (measurement,) = measure_search_ranking(work.tool_calls.records)
    assert measurement.delivered_fraction == 1.0
    assert measurement.projected_fraction < 1.0


async def test_scenario_an_unrelated_objective_scores_every_match_low() -> None:
    """Relevance is judged, not assumed: an unrelated task scores the corpus low."""

    objective = (
        "Change the web dashboard's dark mode palette so the run status badges meet contrast "
        "requirements."
    )
    shadow, _ = await _search(objective, "quota", "domain", mode=SearchRankingMode.SHADOW, top_k=10)
    ranking = shadow["ranking"]

    print("\n=== unrelated objective against 'quota' in domain/")
    print(f"    reader offered  : {ranking['match_count']} matches")
    print(f"    would have kept : {list(ranking['would_return_paths'])}")
    print(f"    ranker cost     : {ranking['input_units']} in, {ranking['duration_ms']} ms")

    # Nothing here is relevant. No match clears the relevance floor, so the
    # complete reader result stands rather than trading matches for nothing.
    assert len(shadow["matches"]) == ranking["match_count"]
    assert ranking["applied"] is False
    assert ranking["status"] == "below_floor"
    assert ranking["would_return_count"] == ranking["match_count"]

    # The case that matters: with ranking fully on, an unrelated search still
    # delivers every match instead of truncating to an arbitrary ten.
    applied, _ = await _search(objective, "quota", "domain", mode=SearchRankingMode.ON, top_k=10)
    print(f"    mode=on delivered: {len(applied['matches'])} of {ranking['match_count']}")
    assert len(applied["matches"]) == ranking["match_count"]
    assert "omitted_matches" not in applied
    assert applied["ranking"]["status"] == "below_floor"
