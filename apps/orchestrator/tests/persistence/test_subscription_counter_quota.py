"""Composed A6 quota fallback retains real partial writes and exact authority."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from forge.agents.subscription_protocol import ProviderToolCall
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.policy import RunnerMode
from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.subscription import (
    AcceptDecision,
    CheckResultEvidence,
    DelegateDecision,
    HandoffStatus,
    QuotaStatus,
    ReasoningEffort,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
    TaskHandoff,
)
from forge.domain.tool import ToolName
from forge.evaluations.check_evidence import read_check_evidence
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery
from forge.worker.composition import compose_worker_handlers
from subscription_counter_manifest import retain_counter_manifest
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_counter_acceptance import (
    PATHS,
    PRIMARY,
    WRITER,
    CounterScript,
    prepared_counter_case,
)
from test_subscription_counter_docker import (
    assert_docker_execution,
    counter_runner_image,  # noqa: F401
)
from test_subscription_usage import _known

LUNA = RouteSpec(
    provider="openai", client="codex", model="gpt-5.6-luna", effort=ReasoningEffort.MEDIUM
)
# Keep the original envelope unchanged; new operator inputs have a distinct scenario ID.
OPERATOR_LUNA = RouteSpec(
    provider="openai", client="codex_app_server", model="gpt-6-luna", effort=ReasoningEffort.MEDIUM
)


class QuotaCounterScript(CounterScript):
    def __init__(self, fallback_route=LUNA):
        super().__init__()
        self.fallback_route = fallback_route
        self.observed_at = datetime.now(UTC)
        self.reset_at = self.observed_at + timedelta(hours=1)
        self.handoff = None

    async def execute(self, request, broker):
        if request.task.purpose is SpecialistPurpose.PRIMARY:
            if self.handoff is not None:
                handoff = self.handoff
                return AcceptDecision(
                    run_id=request.task.run_id,
                    task_id=handoff.task_id,
                    candidate_commit=None,
                    candidate_tree_digest=handoff.candidate_tree_digest,
                    evidence_receipt_ids=handoff.evidence_receipt_ids,
                    rationale="Accept the approved fallback's verified bounded outcome",
                )
            decision = await super().execute(request, broker)
            if isinstance(decision, DelegateDecision):
                child = decision.child_tasks[0]
                decision = replace(
                    decision,
                    child_tasks=(
                        replace(
                            child,
                            budget=replace(child.budget, max_tool_calls=120, max_named_checks=3),
                        ),
                    ),
                )
            return decision

        async def call(key, name, arguments):
            receipt = await broker(
                ProviderToolCall(
                    call_key=key,
                    thread_id="a6",
                    turn_id=str(request.attempt.attempt_id),
                    name=name.value,
                    arguments=arguments,
                )
            )
            self.receipts.append(receipt)
            return receipt

        source = await call("read-counter", ToolName.REPOSITORY_READ_FILE, {"path": PATHS[0]})
        assert source["status"] == "succeeded"
        text = source["metadata"]["content"]
        if request.task.route.effective == WRITER:
            assert "value + 2" in text
            written = await call(
                "partial-write",
                ToolName.REPOSITORY_WRITE_FILE,
                {
                    "path": PATHS[0],
                    "content": text.replace("value + 2", "value + 0"),
                },
            )
            assert written["status"] == "succeeded"
            return SubscriptionInvocationResult(
                attempt=request.attempt,
                failure=SubscriptionFailure.QUOTA,
                quota_exhaustion=QuotaExhaustion(
                    self.observed_at, "scripted_usage_exhausted", self.reset_at
                ),
                telemetry=_known(quota_status=QuotaStatus.EXHAUSTED),
            )
        assert (
            request.task.route.effective == self.fallback_route
            and request.task.route.requested == WRITER
        )
        assert "value + 0" in text, "approved fallback must see the retained partial write"
        tests = await call("read-tests", ToolName.REPOSITORY_READ_FILE, {"path": PATHS[1]})
        fixed = await call(
            "repair-counter",
            ToolName.REPOSITORY_WRITE_FILE,
            {
                "path": PATHS[0],
                "content": text.replace("value + 0", "value + 1"),
            },
        )
        boundary = await call(
            "add-boundary",
            ToolName.REPOSITORY_WRITE_FILE,
            {
                "path": PATHS[1],
                "content": tests["metadata"]["content"] + "    assert increment(-1) == 0\n",
            },
        )
        assert fixed["status"] == boundary["status"] == "succeeded"
        passed = await call("unit", ToolName.BUILD_RUN_NAMED_CHECK, {"command_name": "unit"})
        assert passed["status"] == "succeeded"
        snapshot = await call("snapshot", ToolName.GIT_DIFF, {"scope": "snapshot"})
        assert snapshot["status"] == "succeeded"
        self.handoff = TaskHandoff(
            run_id=request.task.run_id,
            task_id=request.task.task_id,
            attempt_id=request.attempt.attempt_id,
            status=HandoffStatus.COMPLETED,
            candidate_tree_digest=snapshot["metadata"]["candidate_tree_digest"],
            changed_paths=PATHS,
            check_results=(
                CheckResultEvidence(
                    command_name="unit",
                    exit_code=0,
                    passed=True,
                    output_digest=passed["metadata"]["command_result_digest"],
                    duration_ms=passed["metadata"]["command_duration_ms"],
                    receipt_id=passed["operation_id"],
                ),
            ),
            evidence_receipt_ids=(passed["operation_id"], snapshot["operation_id"]),
            summary="Repaired retained Google partial work through the approved Luna route",
        )
        return self.handoff


@pytest.mark.integration
@pytest.mark.parametrize(
    "fallback_route,scenario_suffix",
    [(LUNA, ""), (OPERATOR_LUNA, "-operator-luna6-2")],
    ids=["historical-luna", "operator-luna6-v2"],
)
@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize(
    "runner_mode",
    [RunnerMode.TRUSTED_HOST, pytest.param(RunnerMode.DOCKER, marks=pytest.mark.docker)],
    ids=["trusted-host", "docker"],
)
async def test_counter_quota_fallback_preserves_effects_and_primary(
    session_factory, tmp_path, approved, runner_mode, request, fallback_route, scenario_suffix
):
    runner_image = (
        request.getfixturevalue("counter_runner_image") if runner_mode is RunnerMode.DOCKER else ""
    )
    script = QuotaCounterScript(fallback_route)
    case = await prepared_counter_case(
        session_factory,
        tmp_path,
        script=script,
        worker_fallbacks=(fallback_route,) if approved else (),
        primary_budget=TaskBudget(max_provider_attempts=8, max_tool_calls=200),
        runner_mode=runner_mode,
        runner_image=runner_image,
    )
    handlers = case.handlers
    try:
        exhausted = await case.worker.run_once()
        assert not script.errors, script.errors
        assert exhausted.attempt.settlement.disposition == "quota_deferred"
        assert exhausted.attempt.result.failure is SubscriptionFailure.QUOTA
        async with case.factory() as work:
            blocked = await work.quota.status(work.quota.policy.key_for(WRITER))
            assert blocked.status == "blocked" and blocked.reset_at == script.reset_at
            usage_before = await work.subscription_budget.usage(case.run.id, script.child_id)
            assert (
                usage_before.consumed.provider_attempts == 1 and usage_before.consumed.repairs == 0
            )
            assert usage_before.outstanding.provider_attempts == 0
            scheduled = await work.session.get(SubscriptionScheduledTask, script.child_id)
            assert scheduled.lease_owner is None and scheduled.state == "queued"
            run = await work.runs.get(case.run.id)
            worktree = Path(run.worktree_path)
        assert "value + 0" in (worktree / PATHS[0]).read_text(encoding="utf-8")
        await handlers.aclose()
        handlers = compose_worker_handlers(
            case.settings,
            session_factory,
            subscription_adapters=tuple(
                script.adapter(route)
                for route in ((PRIMARY, WRITER, fallback_route) if approved else (PRIMARY, WRITER))
            ),
        )
        if not approved:
            # Two fresh pollers share the retained PostgreSQL block. No launch,
            # attempt, repair or occupied execution slot is created by a skip.
            values = await asyncio.gather(
                *(
                    handlers.subscription_invocations(f"blocked-{index}").run_once()
                    for index in range(2)
                )
            )
            assert values == [None, None]
            async with case.factory() as work:
                assert (
                    await work.subscription_budget.usage(case.run.id, script.child_id)
                    == usage_before
                )
                scheduled = await work.session.get(SubscriptionScheduledTask, script.child_id)
                assert scheduled.lease_owner is None and scheduled.repairs == 0
            assert len(script.requests) == 3 and len(script.receipts) == 2
            view = await SubscriptionTaskQuery(session_factory).tasks(case.run.id)
            child = next(task for task in view["tasks"] if task["task_id"] == script.child_id)
            assert child["quota_status"]["status"] == "blocked"
            assert not child["fallback_selected"]
            assert "value + 0" in (worktree / PATHS[0]).read_text(encoding="utf-8")
            manifest = await retain_counter_manifest(
                case.factory,
                FilesystemArtifactStore(case.settings.artifact_root),
                case.fixture,
                script,
                run_id=case.run.id,
                tmp_path=tmp_path,
                grade=None,
                scenario=f"A6-deferred{scenario_suffix}",
                worker_check_repair_sequences=0,
                quota_status=blocked,
                operator_view=view,
                restarted_before_handoff=False,
            )
            if runner_mode is RunnerMode.DOCKER:
                assert assert_docker_execution(manifest, runner_image) == []
            return
        worker = handlers.subscription_invocations("approved-fallback")
        repaired = await worker.run_once()
        assert not script.errors, script.errors
        assert repaired.attempt.result.failure is None
        assert repaired.attempt.settlement.disposition == "decision_pending"
        assert (
            repaired.admission.task.task_id == exhausted.admission.task.task_id == script.child_id
        )
        assert repaired.admission.attempt.attempt_number == 2
        assert repaired.admission.task.owned_paths == PATHS
        assert repaired.admission.task.budget == exhausted.admission.task.budget
        assert repaired.admission.task.route.requested == WRITER
        assert repaired.admission.task.route.effective == fallback_route
        await handlers.aclose()
        handlers = compose_worker_handlers(
            case.settings,
            session_factory,
            subscription_adapters=tuple(
                script.adapter(route) for route in (PRIMARY, WRITER, fallback_route)
            ),
        )
        assert (await handlers.subscription_decision_recovery.reconcile_all()).applied == 1
        worker = handlers.subscription_invocations("accept-after-restart")
        accepted = await worker.run_once()
        assert not script.errors, script.errors
        assert accepted.application.disposition == "task_accepted"
        assert accepted.admission.task.route.effective == PRIMARY
        async with case.factory() as work:
            usage = await work.subscription_budget.usage(case.run.id, script.child_id)
            assert usage.consumed.provider_attempts == 2 and usage.consumed.repairs == 0
            assert usage.consumed.tool_calls == 8 and usage.consumed.named_checks == 1
            assert usage.outstanding.provider_attempts == 0
            source = await work.session.get(
                SubscriptionAttemptResult, accepted.admission.attempt.attempt_id
            )
            handoff = await work.session.get(
                SubscriptionAttemptResult, repaired.admission.attempt.attempt_id
            )
            assert source.application_payload["handoff_attempt_id"] == str(
                repaired.admission.attempt.attempt_id
            )
            assert (
                source.application_payload["handoff_application_digest"]
                == handoff.application_digest
            )
            assert (await work.quota.status(work.quota.policy.key_for(WRITER))).status == "blocked"
            assert (await work.runs.get(case.run.id)).pending_gate is None
        assert len(script.requests) == 5
        view = await SubscriptionTaskQuery(session_factory).tasks(case.run.id)
        child = next(task for task in view["tasks"] if task["task_id"] == script.child_id)
        assert child["fallback_selected"]
        assert child["requested_route"]["model"] == WRITER.model
        assert child["effective_route"]["model"] == fallback_route.model
        passing = next(
            item
            for item in script.receipts
            if item["tool_name"] == ToolName.BUILD_RUN_NAMED_CHECK.value
        )
        grade = await read_check_evidence(
            case.fixture.case_contract,
            FilesystemArtifactStore(case.settings.artifact_root),
            {**passing, "tool_call_id": passing["operation_id"]},
            command_name="unit",
        )
        assert grade is not None
        assert set(grade[0]) == set(case.fixture.case_contract.required_tests) and all(
            grade[0].values()
        )
        assert set(grade[1]) == set(case.fixture.case_contract.required_assertions) and all(
            grade[1].values()
        )
        manifest = await retain_counter_manifest(
            case.factory,
            FilesystemArtifactStore(case.settings.artifact_root),
            case.fixture,
            script,
            run_id=case.run.id,
            tmp_path=tmp_path,
            grade=grade,
            scenario=f"A6-approved-fallback{scenario_suffix}",
            worker_check_repair_sequences=0,
            quota_status=blocked,
            operator_view=view,
        )
        if runner_mode is RunnerMode.DOCKER:
            results = assert_docker_execution(manifest, runner_image)
            assert len(results) == 1 and results[0]["exit_code"] == 0
    finally:
        await handlers.aclose()
