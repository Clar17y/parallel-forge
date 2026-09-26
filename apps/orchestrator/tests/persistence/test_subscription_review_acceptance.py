"""A4 plans, reviews a real omission, repairs, re-reviews and requests human PR approval."""

import json
from pathlib import Path

import pytest
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.approval import ApprovalGate
from forge.domain.run import RunState
from forge.domain.subscription import RolePreference, SpecialistPurpose
from forge.domain.tool import ToolName
from forge.evaluations.check_evidence import read_check_evidence
from forge.evaluations.subscription_fixtures import (
    _run_git_command,
    build_review_gates_fixture,
    release_slow_unit_barrier,
)
from forge.worker.composition import compose_worker_handlers
from subscription_catalog_script import OPUS
from subscription_counter_manifest import retain_counter_manifest
from subscription_review_script import BRIEF, RANGE_PATHS, SOL, ReviewLifecycleScript
from subscription_worktree_case import command_once
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_counter_acceptance import PRIMARY, WRITER, prepared_counter_case


@pytest.mark.integration
async def test_planning_review_and_bounded_repair_reach_final_validation(session_factory, tmp_path):
    script = ReviewLifecycleScript()
    case = await prepared_counter_case(
        session_factory,
        tmp_path,
        script=script,
        fixture_builder=build_review_gates_fixture,
        extra_preferences=(
            RolePreference(purpose=SpecialistPurpose.PLANNING, preferred_route=SOL),
            RolePreference(purpose=SpecialistPurpose.INTEGRATION, preferred_route=WRITER),
            RolePreference(purpose=SpecialistPurpose.INDEPENDENT_REVIEW, preferred_route=OPUS),
        ),
    )
    handlers = case.handlers
    applications = []
    settlements = {}
    restarted_before_review = False
    try:
        async with case.factory() as work:
            tree = Path((await work.runs.get(case.run.id)).worktree_path)
        original_doc = (tree / "docs/result.md").read_bytes()
        release_slow_unit_barrier(tree)
        for index in range(12):
            outcome = await handlers.subscription_invocations(f"a4-{index}").run_once()
            assert not script.errors, script.errors
            assert outcome is not None
            assert outcome.attempt.result.failure is None
            settlements[outcome.admission.task.task_id] = outcome.attempt.settlement
            assert outcome.application is None or outcome.application.accepted, outcome.application
            if (
                outcome.admission.task.purpose is SpecialistPurpose.INDEPENDENT_REVIEW
                and len(script.reviews) == 1
            ):
                await handlers.aclose()
                handlers = compose_worker_handlers(
                    case.settings,
                    session_factory,
                    subscription_adapters=tuple(
                        script.adapter(route) for route in (PRIMARY, WRITER, SOL, OPUS)
                    ),
                )
                case.handlers = handlers
                restarted_before_review = True
            recovered = await handlers.subscription_decision_recovery.reconcile_all()
            assert recovered.deferred == recovered.unsupported == 0, recovered
            async with case.factory() as work:
                replay = await work.subscription_decisions.handoff_replay(
                    outcome.admission.attempt.attempt_id
                )
                if replay is not None:
                    applications.append((outcome.admission.task.task_id, replay))
            if script.primary_stage == 6:
                break
        assert script.primary_stage == 6
        assert restarted_before_review and len(script.reviews) == 2
        first_review, final_review = script.reviews
        assert (
            first_review.task_id != final_review.task_id
            and first_review.attempt_id != final_review.attempt_id
        )
        assert first_review.candidate_tree_digest != final_review.candidate_tree_digest
        assert (
            first_review.review_output.findings[0].finding_id
            == final_review.review_output.findings[0].finding_id
        )
        assert first_review.review_output.findings[0].resolved_at is None
        assert final_review.review_output.findings[0].resolved_at is not None
        initial = settlements[script.writer_id]
        # A truthful failed handoff settles directly and returns control to the
        # primary. Accepted here acknowledges the failure observation only.
        assert initial.disposition == "failed" and initial.accepted
        assert script.writer_id not in {identity for identity, _ in applications}
        assert all(result.accepted for _, result in applications)
        assert set(script.denied_writes) == {
            script.planner_id,
            first_review.task_id,
            final_review.task_id,
        }
        assert (tree / "docs/result.md").read_bytes() == original_doc
        assert all(
            request.task.route.effective == PRIMARY
            for request in script.requests
            if request.task.purpose is SpecialistPurpose.PRIMARY
        )
        assert (
            next(
                request for request in script.requests if request.task.task_id == script.planner_id
            ).task.route.effective
            == SOL
        )
        assert all(
            request.task.route.effective == OPUS
            for request in script.requests
            if request.task.purpose is SpecialistPurpose.INDEPENDENT_REVIEW
        )
        assert (
            tuple(
                _run_git_command(
                    ["git", "show", "--format=", "--name-only", "HEAD"], tree
                ).stdout.splitlines()
            )
            == RANGE_PATHS
        )
        assert (await handlers.subscription_decision_recovery.reconcile_all()).applied == 0
        await command_once(case, session_factory, case.run.id, "validate")
        async with case.factory() as work:
            run = await work.runs.get(case.run.id)
            assert (
                run.state is RunState.AWAITING_PR_APPROVAL and run.pending_gate is ApprovalGate.PR
            )
            usage = await work.subscription_budget.usage(run.id)
            assert usage.consumed.provider_attempts == len(script.requests) == 12
            assert usage.consumed.named_checks == 3 and usage.consumed.repairs == 0
            assert usage.outstanding.provider_attempts == 0
        store = FilesystemArtifactStore(case.settings.artifact_root)
        unit_ids = {
            check.receipt_id
            for identity in (script.writer_id, script.repair_id)
            for check in script.handoffs[identity].check_results
            if check.command_name == "unit"
        }
        units = [
            receipt
            for receipt in script.receipts
            if receipt["tool_name"] == ToolName.BUILD_RUN_NAMED_CHECK.value
            and receipt["operation_id"] in unit_ids
        ]
        assert (
            len(units) == 2 and units[0]["status"] == "failed" and units[1]["status"] == "succeeded"
        )
        assert (
            await read_check_evidence(
                case.fixture.case_contract,
                store,
                {**units[0], "tool_call_id": units[0]["operation_id"]},
                command_name="unit",
            )
            is None
        )
        # Retain the actual failure report as diagnostic evidence, never test credit.
        failed_stdout = json.loads(
            await store.open_bytes(units[0]["metadata"]["stdout_digest"], max_bytes=262144)
        )
        assert failed_stdout["stream"] == "stdout" and not failed_stdout["truncated"]
        reports = [
            line.removeprefix("FORGE_EVAL_REPORT_V1:")
            for line in failed_stdout["text"].splitlines()
            if line.startswith("FORGE_EVAL_REPORT_V1:")
        ]
        assert len(reports) == 1
        failed_report = json.loads(reports[0])
        assert failed_report["fixture_version"] == case.fixture.case_contract.fixture_version
        assert (
            failed_report["case_key"] == "review-gates" and failed_report["command_name"] == "unit"
        )
        assert failed_report["assertions"]["inclusive_contract"] is True
        assert failed_report["assertions"]["equal_bound_tested"] is False
        grade = await read_check_evidence(
            case.fixture.case_contract,
            store,
            {**units[1], "tool_call_id": units[1]["operation_id"]},
            command_name="unit",
        )
        assert grade is not None
        assert set(grade[0]) == set(case.fixture.case_contract.required_tests) and all(
            grade[0].values()
        )
        assert set(grade[1]) == set(case.fixture.case_contract.required_assertions) and all(
            grade[1].values()
        )
        await retain_counter_manifest(
            case.factory,
            store,
            case.fixture,
            script,
            run_id=case.run.id,
            tmp_path=tmp_path,
            grade=grade,
            scenario="A4-review-repair",
            worker_check_repair_sequences=0,
            operator_view={
                "planner_brief": BRIEF,
                "failed_check_report": failed_report,
                "failed_check_earns_no_credit": True,
                "initial_failure_settlement": initial,
                "primary_delegated_repair_tasks": 1,
                "repair_task_id": script.repair_id,
                "reviews": script.reviews,
                "restart_before_first_review_handoff_application": restarted_before_review,
                "handoff_applications": applications,
                "final_state": run.state,
            },
            restarted_before_handoff=False,
        )
    finally:
        await handlers.aclose()
