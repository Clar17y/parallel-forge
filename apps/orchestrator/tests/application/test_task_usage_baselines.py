from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from forge.application.services.task_usage_baselines import (
    TaskUsageBaselineService,
    UsageReportFilters,
)
from forge.domain.task_usage_baselines import BaselineSample, SamplingRules


class Source:
    def __init__(self, samples, projects):
        self._samples = samples
        self._projects = projects
        self.requested_projects = None
        self.requested_subjects = None

    async def accessible_project_ids(self):
        return self._projects

    async def samples(self, *, project_ids, as_of, window_days):
        self.requested_projects = project_ids
        return tuple(self._samples)

    async def exclusions(self, *, project_ids, as_of, window_days, subjects):
        self.requested_subjects = subjects
        return {"failed": len(subjects)}


def make_sample(project_id, model, offset=1, route_members=None):
    return BaselineSample(
        project_id=project_id,
        subject_kind="run",
        subject_id=uuid4(),
        completed_at=datetime(2026, 9, offset, tzinfo=UTC),
        total_tokens=100,
        outcome="completed",
        finalized=True,
        measured=True,
        configured_route="configured",
        effective_route="effective",
        task_kind="implementation",
        route_members=route_members or (("developer", "provider-a", model, "medium"),),
    )


async def test_report_applies_accessible_project_filter_before_all_breakdowns():
    allowed, denied = uuid4(), uuid4()
    source = Source(
        [make_sample(allowed, "model-a"), make_sample(denied, "model-secret")],
        {allowed},
    )
    report = await TaskUsageBaselineService(source).report(
        UsageReportFilters(
            project_ids=frozenset({allowed, denied}),
            as_of=datetime(2026, 10, 1, tzinfo=UTC),
            rules=SamplingRules(history_limit=10, minimum_comparables=1),
        )
    )
    assert source.requested_projects == {allowed}
    assert report["overview"]["sample_count"] == 1
    assert list(report["by_model"]) == ["model-a"]
    assert report["excluded_outcomes"] == {"failed": 1}


async def test_authoring_and_implementation_averages_remain_separate():
    project = uuid4()
    first = make_sample(
        project,
        "model-a",
        route_members=(
            ("developer", "provider-a", "model-a", "medium"),
            ("reviewer", "provider-a", "model-b", "high"),
        ),
    )
    second = BaselineSample(
        **{
            **{
                field: getattr(first, field)
                for field in first.__dataclass_fields__
            },
            "subject_kind": "authoring_job",
            "subject_id": uuid4(),
            "task_kind": "authoring",
            "total_tokens": 300,
        }
    )
    source = Source([first, second], {project})
    report = await TaskUsageBaselineService(source).report(
        UsageReportFilters(
            as_of=datetime(2026, 10, 1, tzinfo=UTC),
            rules=SamplingRules(history_limit=10, minimum_comparables=1),
            task_kind="implementation",
        )
    )
    assert report["by_task_type"]["implementation"]["mean_tokens"] == 100
    assert list(report["by_task_type"]) == ["implementation"]
    assert report["by_model"]["model-a"]["mean_tokens"] == 100
    assert report["by_model"]["model-b"]["mean_tokens"] == 100
    assert "mixed_route" in report["by_model"]["model-b"]["labels"]


async def test_all_report_projections_share_combined_subject_route_and_window_filters():
    project = uuid4()
    run = make_sample(
        project,
        "model-a",
        route_members=(
            ("developer", "provider-a", "model-a", "medium"),
            ("reviewer", "provider-b", "model-b", "high"),
        ),
    )
    run = replace(run, failed_attempt_count=1)
    other_kind = BaselineSample(
        **{
            **{field: getattr(run, field) for field in run.__dataclass_fields__},
            "subject_kind": "authoring_job",
            "subject_id": uuid4(),
            "task_kind": "authoring",
            "total_tokens": 300,
            "failed_attempt_count": 2,
        }
    )
    stale = BaselineSample(
        **{
            **{field: getattr(run, field) for field in run.__dataclass_fields__},
            "subject_id": uuid4(),
            "completed_at": datetime(2026, 1, 1, tzinfo=UTC),
            "failed_attempt_count": 4,
        }
    )
    mismatched_route = replace(
        run,
        subject_id=uuid4(),
        route_members=(
            ("developer", "provider-a", "model-b", "medium"),
            ("reviewer", "provider-b", "model-a", "high"),
        ),
        failed_attempt_count=3,
    )
    source = Source([run, other_kind, stale, mismatched_route], {project})
    report = await TaskUsageBaselineService(source).report(
        UsageReportFilters(
            subject_kind="run",
            task_kind="implementation",
            provider="provider-a",
            model="model-a",
            effort="medium",
            as_of=datetime(2026, 10, 1, tzinfo=UTC),
            rules=SamplingRules(history_limit=10, minimum_comparables=1, window_days=30),
        )
    )
    assert report["overview"]["sample_count"] == 1
    assert list(report["by_task_type"]) == ["implementation"]
    assert list(report["by_project"]) == [str(project)]
    assert list(report["by_provider"]) == ["provider-a"]
    assert list(report["by_model"]) == ["model-a"]
    assert list(report["by_effort"]) == ["medium"]
    assert all(":authoring:" not in key for key in report["comparable_references"])
    assert report["failed_attempt_count"] == 1
    assert source.requested_subjects == {(project, "run", run.subject_id)}
    assert report["excluded_outcomes"] == {"failed": 1}


async def test_member_bucket_excludes_subject_matching_dimensions_on_different_members():
    project = uuid4()
    matched = make_sample(project, "model-a", route_members=(
        ("developer", "provider-a", "model-a", "medium"),
        ("reviewer", "provider-b", "model-b", "high"),
    ))
    crossed = make_sample(project, "model-a", route_members=(
        ("developer", "provider-a", "model-b", "medium"),
        ("reviewer", "provider-b", "model-a", "high"),
    ))
    crossed = replace(crossed, total_tokens=900)
    report = await TaskUsageBaselineService(Source([matched, crossed], {project})).report(
        UsageReportFilters(provider="provider-a", as_of=datetime(2026, 10, 1, tzinfo=UTC),
                           rules=SamplingRules(history_limit=10, minimum_comparables=1))
    )
    assert report["by_model"]["model-a"]["sample_count"] == 1
    assert report["by_model"]["model-a"]["mean_tokens"] == 100
