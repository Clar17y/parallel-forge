"""Ranking evidence must survive PostgreSQL and come back as a measurement.

The unit suites prove the ranking decision; this proves the durable half. A
real search runs through the real tool service against real PostgreSQL, and the
recorded row is then read back exactly as ``forge search-ranking report`` reads
it, so the telemetry cannot quietly be lost or redacted in storage.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.ports.search_ranking import (
    RankedMatch,
    SearchRanking,
    SearchRankingMode,
    SearchRankingRequest,
)
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
from forge.persistence.models import AgentExecution, Step
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.ranking.measurement import format_measurements, measure_search_ranking
from forge.tools.repository import RepositoryReader

pytestmark = pytest.mark.integration

OBJECTIVE = "Make the delivery retry backoff grow between attempts."


class _Ranker:
    """Deterministic ranking so the durable assertions stay exact."""

    async def rank(self, request: SearchRankingRequest) -> SearchRanking:
        return SearchRanking(
            ranked=tuple(
                RankedMatch(
                    index=index,
                    relevance=1.0 if match.path == "retry.py" else 0.4,
                    confidence=0.9,
                )
                for index, match in enumerate(request.matches)
            ),
            model="jev-latest",
            request_id="req_durable",
            input_tokens=321,
            output_tokens=45,
            duration_ms=77,
        )


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "retry.py").write_text("backoff = 2\n", encoding="utf-8")
    (root / "worker.py").write_text("backoff = 3\n", encoding="utf-8")
    (root / "vendor.py").write_text("backoff = 4\n", encoding="utf-8")
    return root


async def _seed(session_factory, repository: Path) -> tuple[RunSnapshot, object, object, object]:
    """Create the project, policy, task, run and execution the tool requires."""

    from forge.persistence.models import Project, ProjectPolicyVersion, Task

    project_id, task_id, run_id = uuid4(), uuid4(), uuid4()
    execution_id, step_id = uuid4(), uuid4()
    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path=str(repository),
        github_repository="Clar17y/forge-ranking",
        default_branch="main",
        secret_paths=(),
    )
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                Project(
                    id=project_id,
                    canonical_path=str(repository),
                    github_repository="Clar17y/forge-ranking",
                    default_branch="main",
                ),
                ProjectPolicyVersion(
                    project_id=project_id,
                    version=1,
                    policy_digest="a" * 64,
                    document_schema_version=1,
                    document=policy.model_dump(mode="json"),
                ),
                Task(
                    id=task_id,
                    project_id=project_id,
                    normalized_text=OBJECTIVE,
                    task_digest="b" * 64,
                ),
            ]
        )
        await session.flush()
        project = await session.get(Project, project_id)
        assert project is not None
        project.current_policy_version = 1

    run = RunSnapshot(
        id=run_id,
        project_id=project_id,
        task_id=task_id,
        state=RunState.CREATED,
        policy_version=1,
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(run)
        # Repository reads are authorized while planning, so move the run there
        # through the real state engine rather than seeding an invalid state.
        run = await work.runs.transition(
            run_id=run_id,
            expected_version=0,
            target=RunState.PLANNING,
            event_type="run.planning_started",
            event_payload={"source": "test"},
        )
        await work.commit()
    async with session_factory() as session, session.begin():
        # The tool boundary proves the execution and step share one locked run
        # context, so both rows must exist exactly as a real planning run has.
        session.add(Step(id=step_id, run_id=run_id, kind="planning", attempt=1, status="RUNNING"))
        await session.flush()
        session.add(
            AgentExecution(
                id=execution_id,
                run_id=run_id,
                step_id=step_id,
                role="planner",
                instruction_version="1",
                provider="test",
                model="test-model",
                status="RUNNING",
            )
        )
    return run, execution_id, step_id, project_id


async def test_search_ranking_evidence_round_trips_through_postgresql(
    session_factory, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    run, execution_id, step_id, project_id = await _seed(session_factory, repository)
    service = ControlledToolService(
        lambda: PostgresUnitOfWork(session_factory),
        repository_reader=RepositoryReader(repository, secret_paths=(), force_python_search=True),
        search_ranker=_Ranker(),
        search_ranking_mode=SearchRankingMode.ON,
        search_ranking_top_k=1,
        search_objective=OBJECTIVE,
    )

    result = await service.invoke(
        ToolAuthorizationContext(
            role=AgentRole.PLANNER,
            run_id=run.id,
            worktree_id=repository_resource_identity(project_id),
            policy_version=1,
            agent_execution_id=execution_id,
            step_id=step_id,
        ),
        ToolRequest(name=ToolName.REPOSITORY_SEARCH, arguments={"literal": "backoff", "path": "."}),
    )

    assert result.status is ToolCallStatus.SUCCEEDED
    assert [match["path"] for match in result.metadata["matches"]] == ["retry.py"]

    # Read the durable row back exactly as the operator report does.
    async with PostgresUnitOfWork(session_factory) as work:
        records = await work.tool_calls.list_for_run(run.id)
    searches = [item for item in records if item.tool_name is ToolName.REPOSITORY_SEARCH]
    assert len(searches) == 1
    stored = dict(searches[0].result_metadata or {})
    ranking = dict(stored["ranking"])

    # The decision and its cost both survived storage and redaction.
    assert ranking["mode"] == "on" and ranking["applied"] is True
    assert ranking["match_count"] == 3 and ranking["returned_count"] == 1
    assert ranking["model"] == "jev-latest" and ranking["request_id"] == "req_durable"
    assert ranking["input_units"] == 321 and ranking["output_units"] == 45
    assert [match["path"] for match in stored["matches"]] == ["retry.py"]
    assert dict(stored["omitted_matches"])["count"] == 2

    (measurement,) = measure_search_ranking(records)
    assert measurement.mode == "on" and measurement.searches == 1
    assert measurement.matches_offered == 3 and measurement.matches_delivered == 1
    assert measurement.matches_withheld == 2
    assert measurement.ranker_input_units == 321

    rendered = format_measurements(measure_search_ranking(records))
    print("\n" + rendered)
    assert "mode=on" in rendered and "matches=1/3" in rendered


async def test_ranking_off_records_a_measurable_baseline_row(
    session_factory, tmp_path: Path
) -> None:
    """The baseline arm of an A/B must also be readable from evidence."""

    repository = _repository(tmp_path)
    run, execution_id, step_id, project_id = await _seed(session_factory, repository)
    service = ControlledToolService(
        lambda: PostgresUnitOfWork(session_factory),
        repository_reader=RepositoryReader(repository, secret_paths=(), force_python_search=True),
        search_ranker=_Ranker(),
        search_ranking_mode=SearchRankingMode.OFF,
        search_objective=OBJECTIVE,
    )

    await service.invoke(
        ToolAuthorizationContext(
            role=AgentRole.PLANNER,
            run_id=run.id,
            worktree_id=repository_resource_identity(project_id),
            policy_version=1,
            agent_execution_id=execution_id,
            step_id=step_id,
        ),
        ToolRequest(name=ToolName.REPOSITORY_SEARCH, arguments={"literal": "backoff", "path": "."}),
    )

    async with PostgresUnitOfWork(session_factory) as work:
        records = await work.tool_calls.list_for_run(run.id)

    (measurement,) = measure_search_ranking(records)
    assert measurement.mode == "off"
    assert measurement.matches_offered == 3 and measurement.matches_delivered == 3
    assert measurement.delivered_fraction == 1.0
    print("\n" + format_measurements(records and measure_search_ranking(records)))
