"""A5: direct primary typo repair, explicit no-review and unchanged human gates."""

from dataclasses import replace
from pathlib import Path

import pytest
from forge.agents.subscription_protocol import ProviderToolCall
from forge.application.services.subscription_broker import BrokerDenied
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.approval import ApprovalGate
from forge.domain.plan import ScopedPlanOutput
from forge.domain.run import RunState
from forge.domain.subscription import AcceptDecision, ReviewSelection, SpecialistPurpose
from forge.domain.tool import ToolName
from forge.evaluations.check_evidence import read_check_evidence
from forge.evaluations.subscription_fixtures import (
    _run_git_command,
    build_review_gates_fixture,
    get_review_gates_typo_case,
    release_slow_unit_barrier,
)
from forge.persistence.models import Approval
from sqlalchemy import select
from subscription_counter_manifest import retain_counter_manifest
from subscription_worktree_case import command_once
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_counter_acceptance import PRIMARY, CounterScript, prepared_counter_case

DOC = "docs/result.md"
REASON = "trivial exact-text documentation correction"


def typo_fixture(path):
    fixture = build_review_gates_fixture(path)
    return replace(fixture, case_contract=get_review_gates_typo_case(fixture.path))


class TrivialScript(CounterScript):
    def __init__(self):
        super().__init__()
        self.selection = None
        self.unit = None
        self.snapshot = None
        self.work_attempt = None
        self.commit = None

    async def execute(self, request, broker):
        assert request.task.purpose is SpecialistPurpose.PRIMARY
        assert request.task.route.effective == PRIMARY
        if request.run_state is RunState.PLANNING:
            return ScopedPlanOutput(
                summary="Correct one exact documentation typo",
                assumptions=(),
                affected_components=(DOC,),
                steps=("Replace teh result with the result and check",),
                required_checks=("unit",),
                risks=("Unrelated documentation changes",),
                security_considerations=(),
                dependency_changes=(),
                owned_paths=(DOC,),
            )
        if self.selection is not None:
            return AcceptDecision(
                run_id=request.task.run_id,
                task_id=request.task.task_id,
                candidate_commit=self.selection.candidate_commit,
                candidate_tree_digest=self.selection.candidate_tree_digest,
                evidence_receipt_ids=(
                    self.commit["operation_id"],
                    self.unit["operation_id"],
                    self.snapshot["operation_id"],
                ),
                rationale="Accept the exact checked typo correction under the recorded no-review decision",
            )
        self.work_attempt = request.attempt.attempt_id
        assert request.task.owned_paths == (DOC,)

        async def call(key, tool, arguments):
            receipt = await broker(
                ProviderToolCall(
                    call_key=key,
                    thread_id="a5",
                    turn_id="direct",
                    name=tool.value,
                    arguments=arguments,
                )
            )
            self.receipts.append(receipt)
            assert receipt["status"] == "succeeded"
            return receipt

        original = await call("read-doc", ToolName.REPOSITORY_READ_FILE, {"path": DOC})
        with pytest.raises(BrokerDenied):
            await call(
                "outside-plan",
                ToolName.REPOSITORY_WRITE_FILE,
                {"path": "src/range.py", "content": "outside approved scope\n"},
            )
        await call(
            "correct-typo",
            ToolName.REPOSITORY_WRITE_FILE,
            {
                "path": DOC,
                "content": original["metadata"]["content"].replace("teh result", "the result"),
            },
        )
        self.commit = await call(
            "checkpoint", ToolName.GIT_COMMIT, {"message": "docs: correct result typo"}
        )
        self.unit = await call("unit", ToolName.BUILD_RUN_NAMED_CHECK, {"command_name": "unit"})
        self.snapshot = await call("snapshot", ToolName.GIT_DIFF, {"scope": "snapshot"})
        self.selection = ReviewSelection(
            run_id=request.task.run_id,
            candidate_commit=self.commit["metadata"]["new_sha"],
            candidate_tree_digest=self.snapshot["metadata"]["candidate_tree_digest"],
            review_required=False,
            no_review_reason=REASON,
        )
        return self.selection


@pytest.mark.integration
async def test_primary_trivial_fix_preserves_human_gates(session_factory, tmp_path):
    script = TrivialScript()
    case = await prepared_counter_case(
        session_factory,
        tmp_path,
        script=script,
        fixture_builder=typo_fixture,
        start_work=False,
    )
    try:
        assert case.delegated is None and len(script.requests) == 1
        async with case.factory() as work:
            tree = Path((await work.runs.get(case.run.id)).worktree_path)
        release_slow_unit_barrier(tree)
        selected = await case.worker.run_once()
        assert not script.errors, script.errors
        assert selected.attempt.result.failure is None
        assert (
            selected.application.accepted and selected.application.disposition == "review_selected"
        )
        accepted = await case.worker.run_once()
        assert not script.errors, script.errors
        assert accepted.attempt.result.failure is None
        assert (
            accepted.application.accepted
            and accepted.application.disposition == "acceptance_validation_queued"
        )
        await command_once(case, session_factory, case.run.id, "validate")
        async with case.factory() as work:
            run = await work.runs.get(case.run.id)
            assert (
                run.state is RunState.AWAITING_PR_APPROVAL and run.pending_gate is ApprovalGate.PR
            )
            approvals = (
                await work.session.scalars(select(Approval).where(Approval.run_id == run.id))
            ).all()
            assert len(approvals) == 1 and approvals[0].gate == "plan"
            approval_gates = [approval.gate for approval in approvals]
            usage = await work.subscription_budget.usage(run.id)
            assert usage.consumed.provider_attempts == 3
            assert usage.consumed.named_checks == 1 and usage.consumed.repairs == 0
            assert usage.outstanding.provider_attempts == 0
            calls = await work.tool_calls.list_for_run(run.id)
            assert {call.subscription_attempt_id for call in calls} == {script.work_attempt}
        diff = _run_git_command(["git", "diff", "--numstat", run.base_sha], tree).stdout.strip()
        assert diff == "1\t1\tdocs/result.md"
        assert len(script.requests) == 3
        assert all(request.task.purpose is SpecialistPurpose.PRIMARY for request in script.requests)
        store = FilesystemArtifactStore(case.settings.artifact_root)
        grade = await read_check_evidence(
            case.fixture.case_contract,
            store,
            {**script.unit, "tool_call_id": script.unit["operation_id"]},
            command_name="unit",
        )
        assert grade is not None and all(grade[0].values()) and all(grade[1].values())
        await retain_counter_manifest(
            case.factory,
            store,
            case.fixture,
            script,
            run_id=run.id,
            tmp_path=tmp_path,
            grade=grade,
            scenario="A5-trivial",
            worker_check_repair_sequences=0,
            operator_view={
                "implementation_attempts": 1,
                "separate_plan_and_acceptance_attempts": 2,
                "no_review": script.selection,
                "human_approval_gates": approval_gates,
            },
            restarted_before_handoff=False,
        )
    finally:
        await case.handlers.aclose()
