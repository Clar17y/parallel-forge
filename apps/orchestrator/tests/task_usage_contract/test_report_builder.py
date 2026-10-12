"""Subject reports preserve source context and policy absence."""

from uuid import uuid4

import pytest
from forge.api.schemas.task_usage import UsageReportWire
from forge.application.ports.task_usage import FinalizedSourceFact, UsageTotals
from forge.application.services.task_usage_reports import build_usage_report
from forge.domain.task_usage_contract import (
    RouteCohort,
    RouteMember,
    SourceBinding,
    SubjectKey,
    WorkUnitBinding,
)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows, task_rows=()):
        self._rows = [rows, task_rows]

    async def execute(self, _statement):
        return _Rows(self._rows.pop(0))


@pytest.mark.asyncio
async def test_report_keeps_independent_context_and_no_policy() -> None:
    subject = SubjectKey(project_id=uuid4(), kind="run", subject_id=uuid4())
    route = RouteCohort.from_members(
        (RouteMember(role="primary", provider="test", model="test", ordinal=0),)
    )
    root = WorkUnitBinding(
        subject=subject,
        work_unit_id=uuid4(),
        admitted_phase_id="plan",
        configured_route=route,
        effective_route=route,
        source=SourceBinding(kind="subscription_attempt", source_id=uuid4(), parent_id=uuid4()),
    )
    child = WorkUnitBinding(
        subject=subject,
        work_unit_id=uuid4(),
        admitted_phase_id="implementation",
        configured_route=route,
        effective_route=route,
        source=SourceBinding(
            kind="subscription_attempt", source_id=uuid4(), parent_id=uuid4()
        ),
    )
    facts = tuple(
        FinalizedSourceFact(
            binding=binding,
            input_tokens=input_tokens,
            output_tokens=10,
            cached_input_tokens=0,
            reasoning_output_tokens=None,
            context_occupied_tokens=current,
            context_capacity_tokens=100,
            compaction_count=compactions,
            completeness="complete",
            final_status="succeeded",
            evidence_kind="observation_final",
            disagreements=(),
        )
        for binding, input_tokens, current, compactions in (
            (root, 20, 40, 1),
            (child, 30, 30, 2),
        )
    )
    totals = UsageTotals(
        subject=subject,
        snapshot_digest="a" * 64,
        source_count=2,
        final_source_count=2,
        input_tokens=50,
        output_tokens=20,
        cached_input_tokens=0,
        reasoning_output_tokens=None,
        completeness="complete",
        source_facts=facts,
    )
    session = _Session(
        [(root.work_unit_id, 55), (child.work_unit_id, 35)],
        [(root.source.parent_id, None), (child.source.parent_id, root.source.parent_id)],
    )
    report = await build_usage_report(
        session,
        totals=totals,
        policy_revision=None,
        baseline=None,
        checkpoint=None,
    )
    assert report is not None
    assert report.policy_revision is None and report.input_tokens == 50
    assert report.context_occupied_tokens is None
    assert report.context_capacity_tokens is None
    assert report.context_peak_tokens == 55
    assert report.compaction_count == 3
    assert [(source.phase, source.delegated) for source in report.sources] == [
        ("plan", False),
        ("implementation", True),
    ]
    assert [source.context_occupied_tokens for source in report.sources] == [40, 30]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent_execution", "authoring_attempt", "subscription_attempt"])
async def test_report_leaves_unproved_ancestry_unknown(kind: str) -> None:
    subject = SubjectKey(
        project_id=uuid4(),
        kind="authoring_job" if kind == "authoring_attempt" else "run",
        subject_id=uuid4(),
    )
    route = RouteCohort.from_members(
        (RouteMember(role="primary", provider="test", model="test", ordinal=0),)
    )
    binding = WorkUnitBinding(
        subject=subject, work_unit_id=uuid4(), admitted_phase_id="plan",
        configured_route=route, effective_route=route,
        source=SourceBinding(
            kind=kind, source_id=uuid4(),
            parent_id=uuid4() if kind == "subscription_attempt" else None,
        ),
    )
    totals = UsageTotals(
        subject=subject, snapshot_digest="a" * 64,
        source_count=1, final_source_count=0,
        input_tokens=1, output_tokens=1, cached_input_tokens=None,
        reasoning_output_tokens=None, completeness="lower_bound",
        source_facts=(FinalizedSourceFact(
            binding=binding, input_tokens=1, output_tokens=1,
            cached_input_tokens=None, reasoning_output_tokens=None,
            context_occupied_tokens=None, context_capacity_tokens=None,
            compaction_count=None, completeness="lower_bound", final_status=None,
            evidence_kind="live", disagreements=(),
        ),),
    )
    report = await build_usage_report(
        _Session([], []), totals=totals, policy_revision=None,
        baseline=None, checkpoint=None,
    )
    assert report is not None
    assert report.sources[0].delegated is None
    assert UsageReportWire.dump_python(report, mode="json")["sources"][0]["delegated"] is None
