"""A4 scripted role decisions; every file/check/commit receipt comes from Forge."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.agents.subscription_protocol import ProviderToolCall
from forge.application.services.subscription_broker import BrokerDenied
from forge.domain.agent import ReviewDecision, ReviewOutput
from forge.domain.plan import ScopedPlanOutput
from forge.domain.review import FindingSeverity, ReviewFinding
from forge.domain.run import RunState
from forge.domain.subscription import (
    AcceptanceCriterion,
    AcceptDecision,
    CheckResultEvidence,
    DelegateDecision,
    HandoffStatus,
    LogicalTaskContract,
    ReasoningEffort,
    ReviewedTaskHandoff,
    ReviewSelection,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
    TaskHandoff,
)
from forge.domain.tool import ToolName
from subscription_catalog_script import OPUS
from test_subscription_counter_acceptance import CounterScript

SOL = RouteSpec(provider="openai", client="codex", model="gpt-5.6-sol", effort=ReasoningEffort.LOW)
RANGE_PATHS = ("src/range.py", "tests/test_range.py")
BRIEF = "Cover lower (1,5)[0]=1, upper (1,5)[-1]=5, reversed (5,1,-1), and equal (3,3)=[3]."
INCOMPLETE_TESTS = '''"""Range boundary cases; equal-bound coverage is the injected omission."""
from range import inclusive_range

def test_lower():
    assert inclusive_range(1, 5)[0] == 1

def test_upper():
    assert inclusive_range(1, 5)[-1] == 5

def test_reversed():
    assert inclusive_range(5, 1, -1) == [5, 4, 3, 2, 1]
'''
EQUAL_TEST = "\ndef test_equal():\n    assert inclusive_range(3, 3) == [3]\n"


class ReviewLifecycleScript(CounterScript):
    def __init__(self):
        super().__init__()
        self.planner_id, self.writer_id, self.repair_id = uuid4(), uuid4(), uuid4()
        self.primary_stage = 0
        self.handoffs = {}
        self.reviews = []
        self.denied_writes = []
        self.seen_outcomes = []

    def delegate(self, request, identity, purpose, paths, checks, description):
        return DelegateDecision(
            run_id=request.task.run_id,
            parent_task_id=request.task.task_id,
            child_tasks=(
                LogicalTaskContract(
                    run_id=request.task.run_id,
                    task_id=identity,
                    parent_task_id=request.task.task_id,
                    purpose=purpose,
                    route=request.envelope.route_for(purpose),
                    owned_paths=paths,
                    named_checks=checks,
                    budget=TaskBudget(max_provider_attempts=1),
                    max_repairs=0,
                    typed_acceptance=(
                        AcceptanceCriterion(
                            criterion_id="range",
                            description=description,
                            required_check_names=checks,
                        ),
                    ),
                ),
            ),
            rationale=description,
        )

    def primary(self, request):
        self.seen_outcomes.append(request.untrusted_context["task_outcomes"])
        stage = self.primary_stage
        self.primary_stage += 1
        if stage == 0:
            return self.delegate(
                request,
                self.planner_id,
                SpecialistPurpose.PLANNING,
                (),
                (),
                "Inspect the inclusive range contract and supply a concrete adversarial brief",
            )
        if stage == 1:
            assert self.handoffs[self.planner_id].summary == BRIEF
            assert any(
                value["task_id"] == str(self.planner_id) and value["recorded_handoff"] is not None
                for value in request.untrusted_context["task_outcomes"]
            )
            return self.delegate(
                request,
                self.writer_id,
                SpecialistPurpose.ROUTINE_IMPLEMENTATION,
                RANGE_PATHS,
                ("unit",),
                "Fix inclusive range and all four boundary cases: " + BRIEF,
            )
        if stage in (2, 4):
            candidate = self.handoffs[self.writer_id if stage == 2 else self.repair_id]
            return ReviewSelection(
                run_id=request.task.run_id,
                candidate_commit=candidate.candidate_commit,
                candidate_tree_digest=candidate.candidate_tree_digest,
                review_required=True,
                reviewer_route=OPUS,
            )
        if stage == 3:
            assert self.reviews[-1].review_output.decision is ReviewDecision.REQUEST_CHANGES
            return self.delegate(
                request,
                self.repair_id,
                SpecialistPurpose.INTEGRATION,
                RANGE_PATHS,
                ("unit", "slow-unit"),
                "Resolve range-equal-bound, preserve the implementation, checkpoint and validate",
            )
        assert stage == 5 and self.reviews[-1].review_output.decision is ReviewDecision.APPROVE
        repaired, reviewed = self.handoffs[self.repair_id], self.reviews[-1]
        return AcceptDecision(
            run_id=request.task.run_id,
            task_id=request.task.task_id,
            candidate_commit=repaired.candidate_commit,
            candidate_tree_digest=repaired.candidate_tree_digest,
            evidence_receipt_ids=(*repaired.evidence_receipt_ids, *reviewed.evidence_receipt_ids),
            rationale="Accept the checked checkpoint and fresh review resolving the stable finding",
        )

    async def execute(self, request, broker):
        if request.run_state is RunState.PLANNING:
            return ScopedPlanOutput(
                summary="Repair inclusive range and all four boundary cases",
                assumptions=(),
                affected_components=RANGE_PATHS,
                steps=("Obtain adversarial brief, implement, review, repair and validate",),
                required_checks=("unit", "slow-unit"),
                risks=("Missing boundary coverage",),
                security_considerations=(),
                dependency_changes=(),
                owned_paths=RANGE_PATHS,
            )
        if request.task.purpose is SpecialistPurpose.PRIMARY:
            return self.primary(request)

        async def call(key, tool, arguments):
            receipt = await broker(
                ProviderToolCall(
                    call_key=key,
                    thread_id="a4",
                    turn_id=str(request.attempt.attempt_id),
                    name=tool.value,
                    arguments=arguments,
                )
            )
            self.receipts.append(receipt)
            return receipt

        source = await call("read-source", ToolName.REPOSITORY_READ_FILE, {"path": RANGE_PATHS[0]})
        tests = await call("read-tests", ToolName.REPOSITORY_READ_FILE, {"path": RANGE_PATHS[1]})
        assert source["status"] == tests["status"] == "succeeded"
        if request.task.purpose in {
            SpecialistPurpose.PLANNING,
            SpecialistPurpose.INDEPENDENT_REVIEW,
        }:
            with pytest.raises(BrokerDenied):
                await call(
                    "forbidden-write",
                    ToolName.REPOSITORY_WRITE_FILE,
                    {"path": RANGE_PATHS[1], "content": "unauthorized\n"},
                )
            self.denied_writes.append(request.task.task_id)
            snapshot = await call("snapshot", ToolName.GIT_DIFF, {"scope": "snapshot"})
            if request.task.purpose is SpecialistPurpose.PLANNING:
                return self.handoff(request, snapshot, summary=BRIEF, changed_paths=())
            return self.review(request, source, tests, snapshot)
        checks, commit = [], None
        if request.task.task_id == self.writer_id:
            fixed = source["metadata"]["content"].replace(
                "return list(range(start, stop, step))",
                "return list(range(start, stop + (1 if step > 0 else -1), step))",
            )
            await call(
                "fix-source",
                ToolName.REPOSITORY_WRITE_FILE,
                {"path": RANGE_PATHS[0], "content": fixed},
            )
            await call(
                "injected-tests",
                ToolName.REPOSITORY_WRITE_FILE,
                {"path": RANGE_PATHS[1], "content": INCOMPLETE_TESTS},
            )
        else:
            assert (
                request.task.task_id == self.repair_id
                and "def test_equal" not in tests["metadata"]["content"]
            )
            await call(
                "repair-equal",
                ToolName.REPOSITORY_WRITE_FILE,
                {"path": RANGE_PATHS[1], "content": tests["metadata"]["content"] + EQUAL_TEST},
            )
            commit = await call(
                "checkpoint",
                ToolName.GIT_COMMIT,
                {"message": "Fix inclusive range and all boundaries"},
            )
            assert commit["status"] == "succeeded"
        for name in request.task.named_checks:
            check = await call(name, ToolName.BUILD_RUN_NAMED_CHECK, {"command_name": name})
            assert check["status"] == (
                "failed" if request.task.task_id == self.writer_id else "succeeded"
            )
            checks.append(check)
        snapshot = await call("snapshot", ToolName.GIT_DIFF, {"scope": "snapshot"})
        return self.handoff(
            request,
            snapshot,
            checks=checks,
            commit=commit,
            summary="Actual failed boundary check retained for review"
            if commit is None
            else "Added equal-bound case, checkpointed, checked and self-reviewed",
        )

    def handoff(
        self, request, snapshot, *, checks=(), commit=None, summary, changed_paths=RANGE_PATHS
    ):
        assert snapshot["status"] == "succeeded"
        value = TaskHandoff(
            run_id=request.task.run_id,
            task_id=request.task.task_id,
            attempt_id=request.attempt.attempt_id,
            status=HandoffStatus.FAILED
            if any(item["status"] == "failed" for item in checks)
            else HandoffStatus.COMPLETED,
            candidate_commit=commit["metadata"]["new_sha"] if commit else None,
            candidate_tree_digest=snapshot["metadata"]["candidate_tree_digest"],
            changed_paths=changed_paths,
            check_results=tuple(
                CheckResultEvidence(
                    command_name=name,
                    exit_code=item["metadata"]["exit_code"],
                    passed=item["status"] == "succeeded",
                    output_digest=item["metadata"]["command_result_digest"],
                    duration_ms=item["metadata"]["command_duration_ms"],
                    receipt_id=item["operation_id"],
                )
                for name, item in zip(request.task.named_checks, checks, strict=True)
            ),
            evidence_receipt_ids=tuple(
                item["operation_id"] for item in (*checks, *((commit,) if commit else ()), snapshot)
            ),
            summary=summary,
        )
        self.handoffs[request.task.task_id] = value
        return value

    def review(self, request, source, tests, snapshot):
        repaired = "def test_equal" in tests["metadata"]["content"]
        assert "stop + (1 if step > 0 else -1)" in source["metadata"]["content"]
        if repaired:
            assert "inclusive_range(3, 3) == [3]" in tests["metadata"]["content"]
        else:
            assert tests["metadata"]["content"] == INCOMPLETE_TESTS
        candidate = self.handoffs[self.repair_id if repaired else self.writer_id]
        value = ReviewedTaskHandoff(
            run_id=request.task.run_id,
            task_id=request.task.task_id,
            attempt_id=request.attempt.attempt_id,
            status=HandoffStatus.COMPLETED,
            candidate_commit=candidate.candidate_commit,
            candidate_tree_digest=snapshot["metadata"]["candidate_tree_digest"],
            evidence_receipt_ids=(snapshot["operation_id"],),
            summary="Inspected the actual frozen range candidate",
            review_output=ReviewOutput(
                decision=ReviewDecision.APPROVE if repaired else ReviewDecision.REQUEST_CHANGES,
                findings=(
                    ReviewFinding(
                        finding_id="range-equal-bound",
                        severity=FindingSeverity.MAJOR,
                        path=RANGE_PATHS[1],
                        start_line=1,
                        summary="Equal-bound case required by the contract",
                        evidence="The controlled read includes inclusive_range(3, 3) == [3]"
                        if repaired
                        else "Controlled test-file read covers lower, upper and reversed but no equal call",
                        proposed_resolution="Add an assertion that inclusive_range(3, 3) equals [3]",
                        resolved_at=datetime.now(UTC) if repaired else None,
                    ),
                ),
                tested_claims=(
                    "Read the exact inclusive implementation and its boundary assertions",
                ),
                missing_evidence=(),
                summary="Finding resolved on the final tree"
                if repaired
                else "Equal-bound test is absent",
            ),
        )
        self.reviews.append(value)
        return value
