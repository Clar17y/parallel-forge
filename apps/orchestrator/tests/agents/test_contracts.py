"""Discriminating contract tests for structured agent domain models, budgets, and results."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentBudget,
    AgentFinishStatus,
    AgentRequest,
    AgentResult,
    DeveloperInput,
    DeveloperOutput,
    PlannerInput,
    PolicySummary,
    ReviewDecision,
    ReviewerInput,
    ReviewOutput,
    UntrustedContent,
    UntrustedSourceKind,
)
from forge.domain.plan import PlanOutput
from forge.domain.policy import (
    AgentModelPolicy,
    ProjectPolicy,
    RunnerMode,
)
from forge.domain.review import FindingSeverity, ReviewFinding
from forge.domain.tool import ToolName
from forge.observability.usage import UsageRecord
from pydantic import ValidationError

_NIL_UUID = UUID("00000000-0000-0000-0000-000000000000")
_SAMPLE_COMMIT_SHA = "a" * 40
_SAMPLE_DIFF_DIGEST = "c" * 64
_SAMPLE_INSTRUCTION_DIGEST = "d" * 64


# ===========================================================================
# Reusable Builders
# ===========================================================================


def build_untrusted_content(
    content: str = "Implement feature XYZ safely.",
    *,
    source_kind: UntrustedSourceKind = UntrustedSourceKind.TASK,
    source_reference: str = "task-1",
    truncated: bool = False,
    original_byte_count: int | None = None,
) -> UntrustedContent:
    """Build a valid UntrustedContent instance."""
    return UntrustedContent.from_text(
        content,
        source_kind=source_kind,
        source_reference=source_reference,
        original_byte_count=original_byte_count,
        truncated=truncated,
    )


def build_policy_summary(
    *,
    policy_id: UUID | None = None,
    policy_version: int = 1,
    runner_mode: RunnerMode = RunnerMode.DOCKER,
    trusted_project: bool = False,
    required_checks: tuple[str, ...] = ("unit", "lint"),
    allowed_merge_methods: tuple[str, ...] = ("squash",),
) -> PolicySummary:
    """Build a valid PolicySummary instance."""
    return PolicySummary(
        policy_id=policy_id or uuid4(),
        policy_version=policy_version,
        runner_mode=runner_mode,
        trusted_project=trusted_project,
        required_checks=required_checks,
        allowed_merge_methods=allowed_merge_methods,
        publication_blocking_severities=(FindingSeverity.BLOCKER, FindingSeverity.MAJOR),
        merge_blocking_severities=(FindingSeverity.BLOCKER, FindingSeverity.MAJOR),
    )


def build_plan_output(
    *,
    summary: str = "Safe implementation plan for feature XYZ",
    assumptions: tuple[str, ...] = ("Repository is clean",),
    affected_components: tuple[str, ...] = ("orchestrator",),
    steps: tuple[str, ...] = ("Step 1: write tests", "Step 2: implement"),
    required_checks: tuple[str, ...] = ("pytest -q",),
    risks: tuple[str, ...] = ("Regression risk in edge case",),
    security_considerations: tuple[str, ...] = ("No secrets exposed",),
    dependency_changes: tuple[str, ...] = (),
) -> PlanOutput:
    """Build a valid PlanOutput instance."""
    return PlanOutput(
        summary=summary,
        assumptions=assumptions,
        affected_components=affected_components,
        steps=steps,
        required_checks=required_checks,
        risks=risks,
        security_considerations=security_considerations,
        dependency_changes=dependency_changes,
    )


def build_developer_output(
    *,
    summary: str = "Implemented feature XYZ with test coverage",
    changed_paths: tuple[str, ...] = ("src/forge/domain/feature.py",),
    tests_added_or_changed: tuple[str, ...] = ("tests/domain/test_feature.py",),
    named_checks_run: tuple[str, ...] = ("unit", "lint"),
    local_commit_sha: str = _SAMPLE_COMMIT_SHA,
    diff_digest: str = _SAMPLE_DIFF_DIGEST,
    unresolved_concerns: tuple[str, ...] = (),
    plan_deviations: tuple[str, ...] = (),
) -> DeveloperOutput:
    """Build a valid DeveloperOutput instance."""
    return DeveloperOutput(
        summary=summary,
        changed_paths=changed_paths,
        tests_added_or_changed=tests_added_or_changed,
        named_checks_run=named_checks_run,
        local_commit_sha=local_commit_sha,
        diff_digest=diff_digest,
        unresolved_concerns=unresolved_concerns,
        plan_deviations=plan_deviations,
    )


def build_review_finding(
    *,
    finding_id: str = "finding-1",
    severity: FindingSeverity = FindingSeverity.MAJOR,
    path: str = "src/forge/domain/feature.py",
    start_line: int = 10,
    summary: str = "Missing input validation",
    evidence: str = "No check on None value",
    proposed_resolution: str | None = "Add check for None",
    resolved: bool = False,
) -> ReviewFinding:
    """Build a valid ReviewFinding instance."""
    return ReviewFinding(
        finding_id=finding_id,
        severity=severity,
        path=path,
        start_line=start_line,
        summary=summary,
        evidence=evidence,
        proposed_resolution=proposed_resolution,
        resolved_at=datetime.now(tz=UTC) if resolved else None,
    )


def build_review_output(
    *,
    decision: ReviewDecision = ReviewDecision.APPROVE,
    findings: tuple[ReviewFinding, ...] = (),
    tested_claims: tuple[str, ...] = ("Tests pass",),
    missing_evidence: tuple[str, ...] = (),
    summary: str = "Review passed with no remaining issues",
) -> ReviewOutput:
    """Build a valid ReviewOutput instance."""
    return ReviewOutput(
        decision=decision,
        findings=findings,
        tested_claims=tested_claims,
        missing_evidence=missing_evidence,
        summary=summary,
    )


def build_planner_input(
    *,
    original_task: UntrustedContent | None = None,
    base_commit: str = _SAMPLE_COMMIT_SHA,
    repository_tree: UntrustedContent | None = None,
    relevant_instructions: tuple[UntrustedContent, ...] = (),
    policy_summary: PolicySummary | None = None,
) -> PlannerInput:
    """Build a valid PlannerInput instance."""
    return PlannerInput(
        original_task=original_task or build_untrusted_content("Plan task XYZ"),
        base_commit=base_commit,
        repository_tree=repository_tree
        or build_untrusted_content(
            "tree:\n- file.py", source_kind=UntrustedSourceKind.REPOSITORY_TREE
        ),
        relevant_instructions=relevant_instructions,
        policy_summary=policy_summary or build_policy_summary(),
    )


def build_developer_input(
    *,
    original_task: UntrustedContent | None = None,
    plan: PlanOutput | None = None,
    worktree_id: str = "forge-wt-01",
    base_commit: str = _SAMPLE_COMMIT_SHA,
    remediation_findings: tuple[ReviewFinding, ...] = (),
    relevant_instructions: tuple[UntrustedContent, ...] = (),
) -> DeveloperInput:
    """Build a valid DeveloperInput instance."""
    return DeveloperInput(
        original_task=original_task or build_untrusted_content("Develop task XYZ"),
        plan=plan or build_plan_output(),
        worktree_id=worktree_id,
        base_commit=base_commit,
        remediation_findings=remediation_findings,
        relevant_instructions=relevant_instructions,
    )


def build_reviewer_input(
    *,
    original_task: UntrustedContent | None = None,
    plan: PlanOutput | None = None,
    current_diff: UntrustedContent | None = None,
    check_evidence: tuple[UntrustedContent, ...] = (),
    relevant_instructions: tuple[UntrustedContent, ...] = (),
) -> ReviewerInput:
    """Build a valid ReviewerInput instance."""
    return ReviewerInput(
        original_task=original_task or build_untrusted_content("Review task XYZ"),
        plan=plan or build_plan_output(),
        current_diff=current_diff
        or build_untrusted_content("diff --git ...", source_kind=UntrustedSourceKind.DIFF),
        check_evidence=check_evidence,
        relevant_instructions=relevant_instructions,
    )


def build_agent_budget(
    *,
    max_input_tokens: int = 100_000,
    max_output_tokens: int = 16_000,
    max_tool_calls: int = 100,
    max_duration_seconds: int = 1800,
    max_cost_minor: int = 1000,
) -> AgentBudget:
    """Build a valid AgentBudget instance."""
    return AgentBudget(
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_tool_calls=max_tool_calls,
        max_duration_seconds=max_duration_seconds,
        max_cost_minor=max_cost_minor,
    )


def build_agent_request(
    *,
    role: AgentRole = AgentRole.PLANNER,
    context: PlannerInput | DeveloperInput | ReviewerInput | None = None,
    execution_id: UUID | None = None,
    run_id: UUID | None = None,
    task_id: UUID | None = None,
    parent_execution_id: UUID | None = None,
    provider: str = "fake-provider",
    model: str = "fake-model",
    instruction_version: str = "1",
    system_instruction: str = "You are a specialist. Follow exact rules.",
    allowed_tools: tuple[ToolName, ...] | None = None,
    budget: AgentBudget | None = None,
) -> AgentRequest:
    """Build a valid AgentRequest instance."""
    if context is None:
        if role == AgentRole.PLANNER:
            context = build_planner_input()
        elif role == AgentRole.DEVELOPER:
            context = build_developer_input()
        else:
            context = build_reviewer_input()

    if allowed_tools is None:
        if role == AgentRole.PLANNER:
            allowed_tools = (
                ToolName.REPOSITORY_LIST_FILES,
                ToolName.REPOSITORY_READ_FILE,
            )
        elif role == AgentRole.DEVELOPER:
            allowed_tools = (
                ToolName.REPOSITORY_WRITE_FILE,
                ToolName.GIT_COMMIT,
            )
        else:
            allowed_tools = (
                ToolName.REPOSITORY_READ_FILE,
                ToolName.VALIDATION_RESULTS_READ,
            )

    instruction_digest = hashlib.sha256(system_instruction.encode("utf-8")).hexdigest()

    return AgentRequest(
        execution_id=execution_id or uuid4(),
        run_id=run_id or uuid4(),
        task_id=task_id or uuid4(),
        role=role,
        context=context,
        parent_execution_id=parent_execution_id,
        provider=provider,
        model=model,
        instruction_version=instruction_version,
        system_instruction=system_instruction,
        instruction_digest=instruction_digest,
        allowed_tools=allowed_tools,
        budget=budget or build_agent_budget(),
    )


def build_usage_record(
    *,
    provider: str = "fake-provider",
    model: str = "fake-model",
    prompt_version: str = "1",
    input_tokens: int = 500,
    output_tokens: int = 150,
    tool_call_count: int = 2,
    duration_ms: int = 1200,
    cost_minor: int = 5,
) -> UsageRecord:
    """Build a valid UsageRecord matching an AgentResult."""
    return UsageRecord(
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=0,
        duration_ms=duration_ms,
        tool_call_count=tool_call_count,
        provider_request_id="req-123",
        pricing_version="fake-v1",
        estimated_cost_minor=cost_minor,
        currency="USD",
    )


_DEFAULT_OUTPUT = object()


def build_agent_result(
    *,
    execution_id: UUID | None = None,
    role: AgentRole = AgentRole.PLANNER,
    finish_status: AgentFinishStatus = AgentFinishStatus.SUCCEEDED,
    output: Any = _DEFAULT_OUTPUT,
    parent_execution_id: UUID | None = None,
    provider: str = "fake-provider",
    model: str = "fake-model",
    instruction_digest: str = _SAMPLE_INSTRUCTION_DIGEST,
    usage: UsageRecord | None = None,
    tool_call_count: int = 2,
    duration_ms: int = 1200,
) -> AgentResult:
    """Build a valid AgentResult instance."""
    if output is _DEFAULT_OUTPUT:
        if finish_status == AgentFinishStatus.SUCCEEDED:
            if role == AgentRole.PLANNER:
                output = build_plan_output()
            elif role == AgentRole.DEVELOPER:
                output = build_developer_output()
            else:
                output = build_review_output()
        else:
            output = None

    if usage is None:
        usage = build_usage_record(
            provider=provider,
            model=model,
            tool_call_count=tool_call_count,
            duration_ms=duration_ms,
        )

    return AgentResult(
        execution_id=execution_id or uuid4(),
        role=role,
        finish_status=finish_status,
        output=output,
        parent_execution_id=parent_execution_id,
        provider=provider,
        model=model,
        instruction_digest=instruction_digest,
        usage=usage,
        tool_call_count=tool_call_count,
        duration_ms=duration_ms,
    )


# ===========================================================================
# PlanOutput Contract Tests
# ===========================================================================


class TestPlanOutputContract:
    """Tests for the PlanOutput structured schema."""

    def test_valid_plan_output_passes(self) -> None:
        plan = build_plan_output()
        assert plan.summary == "Safe implementation plan for feature XYZ"
        assert len(plan.steps) == 2
        assert len(plan.required_checks) == 1
        assert len(plan.risks) == 1

    def test_plan_requires_ordered_steps_tests_and_risks(self) -> None:
        """Step 1 from Task 15 plan: empty steps, required_checks, or risks fail."""
        with pytest.raises(ValidationError):
            PlanOutput.model_validate(
                {
                    "summary": "Change card",
                    "assumptions": [],
                    "affected_components": ["web"],
                    "steps": [],
                    "required_checks": [],
                    "risks": [],
                    "security_considerations": [],
                    "dependency_changes": [],
                }
            )

    @pytest.mark.parametrize("empty_field", ["steps", "required_checks", "risks"])
    def test_plan_output_rejects_individual_empty_required_collections(
        self, empty_field: str
    ) -> None:
        data: dict[str, Any] = {
            "summary": "Valid summary",
            "assumptions": ["assumption"],
            "affected_components": ["component"],
            "steps": ["step 1"],
            "required_checks": ["check 1"],
            "risks": ["risk 1"],
            "security_considerations": ["sec 1"],
            "dependency_changes": [],
        }
        data[empty_field] = []
        with pytest.raises(ValidationError):
            PlanOutput.model_validate(data)

    def test_plan_output_forbids_extra_fields(self) -> None:
        data = build_plan_output().model_dump()
        data["extra_field"] = "not_allowed"
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            PlanOutput.model_validate(data)

    def test_plan_output_is_frozen(self) -> None:
        plan = build_plan_output()
        with pytest.raises(ValidationError):
            plan.summary = "New summary"

    def test_plan_output_rejects_blank_summary(self) -> None:
        with pytest.raises(ValidationError):
            build_plan_output(summary="   ")

    def test_plan_output_rejects_duplicate_risks_or_checks(self) -> None:
        with pytest.raises(ValidationError, match="must not contain duplicate entries"):
            build_plan_output(risks=("risk 1", "risk 1"))
        with pytest.raises(ValidationError, match="must not contain duplicate entries"):
            build_plan_output(required_checks=("check 1", "check 1"))

    def test_plan_output_allows_duplicate_steps(self) -> None:
        plan = build_plan_output(steps=("run step", "run step"))
        assert len(plan.steps) == 2


# ===========================================================================
# DeveloperOutput Contract Tests
# ===========================================================================


class TestDeveloperOutputContract:
    """Tests for the DeveloperOutput structured schema."""

    def test_valid_developer_output_passes(self) -> None:
        dev_output = build_developer_output()
        assert dev_output.local_commit_sha == _SAMPLE_COMMIT_SHA
        assert dev_output.diff_digest == _SAMPLE_DIFF_DIGEST

    def test_developer_output_forbids_extra_fields(self) -> None:
        data = build_developer_output().model_dump()
        data["hidden_reasoning"] = "private chain of thought"
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            DeveloperOutput.model_validate(data)

    def test_developer_output_is_frozen(self) -> None:
        dev_output = build_developer_output()
        with pytest.raises(ValidationError):
            dev_output.summary = "mutated"

    def test_developer_output_validates_commit_sha_format(self) -> None:
        with pytest.raises(ValidationError, match="at least 40 characters"):
            build_developer_output(local_commit_sha="not-a-sha")
        with pytest.raises(ValidationError, match="canonical lowercase SHA-1 commit ID"):
            build_developer_output(local_commit_sha="A" * 40)
        with pytest.raises(ValidationError, match="canonical lowercase SHA-1 commit ID"):
            build_developer_output(local_commit_sha="z" * 40)

    def test_developer_output_validates_diff_digest_format(self) -> None:
        with pytest.raises(ValidationError, match="at least 64 characters"):
            build_developer_output(diff_digest="not-a-sha256")
        with pytest.raises(ValidationError, match="canonical lowercase SHA-256 digest"):
            build_developer_output(diff_digest="C" * 64)
        with pytest.raises(ValidationError, match="canonical lowercase SHA-256 digest"):
            build_developer_output(diff_digest="z" * 64)

    def test_developer_output_rejects_path_traversal(self) -> None:
        with pytest.raises(ValidationError, match="must not contain traversal or empty components"):
            build_developer_output(
                changed_paths=("src/forge/./feature.py",),
            )
        with pytest.raises(ValidationError, match="must not contain traversal or empty components"):
            build_developer_output(
                changed_paths=("src/forge/../feature.py",),
            )


# ===========================================================================
# ReviewOutput Contract Tests
# ===========================================================================


class TestReviewOutputContract:
    """Tests for ReviewOutput structured schema and decision rules."""

    def test_valid_approval_without_findings_or_missing_evidence(self) -> None:
        review = build_review_output(decision=ReviewDecision.APPROVE)
        assert review.decision == ReviewDecision.APPROVE
        assert review.findings == ()

    def test_valid_approval_with_resolved_findings(self) -> None:
        resolved_finding = build_review_finding(
            severity=FindingSeverity.BLOCKER,
            resolved=True,
        )
        review = build_review_output(
            decision=ReviewDecision.APPROVE,
            findings=(resolved_finding,),
        )
        assert review.decision == ReviewDecision.APPROVE

    def test_approval_rejects_unresolved_blocker_or_major_findings(self) -> None:
        unresolved_blocker = build_review_finding(
            severity=FindingSeverity.BLOCKER,
            resolved=False,
        )
        with pytest.raises(
            ValidationError, match="approval is invalid when unresolved blocker or major"
        ):
            build_review_output(
                decision=ReviewDecision.APPROVE,
                findings=(unresolved_blocker,),
            )

        unresolved_major = build_review_finding(
            severity=FindingSeverity.MAJOR,
            resolved=False,
        )
        with pytest.raises(
            ValidationError, match="approval is invalid when unresolved blocker or major"
        ):
            build_review_output(
                decision=ReviewDecision.APPROVE,
                findings=(unresolved_major,),
            )

    def test_approval_rejects_missing_evidence(self) -> None:
        with pytest.raises(
            ValidationError, match="approval is invalid when missing evidence remains"
        ):
            build_review_output(
                decision=ReviewDecision.APPROVE,
                missing_evidence=("Need integration test output",),
            )

    def test_request_changes_requires_unresolved_finding_or_missing_evidence(self) -> None:
        # Fails when there are no findings and no missing evidence
        with pytest.raises(
            ValidationError, match="request_changes requires at least one unresolved finding"
        ):
            build_review_output(
                decision=ReviewDecision.REQUEST_CHANGES,
                findings=(),
                missing_evidence=(),
            )

        # Fails when all findings are resolved and missing evidence is empty
        resolved_finding = build_review_finding(resolved=True)
        with pytest.raises(
            ValidationError, match="request_changes requires at least one unresolved finding"
        ):
            build_review_output(
                decision=ReviewDecision.REQUEST_CHANGES,
                findings=(resolved_finding,),
                missing_evidence=(),
            )

        # Passes with unresolved finding
        unresolved = build_review_finding(resolved=False)
        review = build_review_output(
            decision=ReviewDecision.REQUEST_CHANGES,
            findings=(unresolved,),
        )
        assert review.decision == ReviewDecision.REQUEST_CHANGES

        # Passes with missing evidence alone
        review2 = build_review_output(
            decision=ReviewDecision.REQUEST_CHANGES,
            missing_evidence=("Missing check receipts",),
        )
        assert review2.decision == ReviewDecision.REQUEST_CHANGES

    def test_blocked_requires_non_empty_missing_evidence(self) -> None:
        with pytest.raises(
            ValidationError, match="blocked decision requires a non-empty missing_evidence"
        ):
            build_review_output(
                decision=ReviewDecision.BLOCKED,
                missing_evidence=(),
            )

        review = build_review_output(
            decision=ReviewDecision.BLOCKED,
            missing_evidence=("Cannot access repository diff",),
        )
        assert review.decision == ReviewDecision.BLOCKED

    def test_review_output_rejects_duplicate_finding_ids(self) -> None:
        f1 = build_review_finding(finding_id="dup-1")
        f2 = build_review_finding(finding_id="dup-1")
        with pytest.raises(ValidationError, match="duplicate finding_id in review findings"):
            build_review_output(
                decision=ReviewDecision.REQUEST_CHANGES,
                findings=(f1, f2),
            )

    def test_review_output_forbids_extra_fields(self) -> None:
        data = build_review_output().model_dump()
        data["developer_rating"] = 5
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ReviewOutput.model_validate(data)


# ===========================================================================
# UntrustedContent & PolicySummary Contract Tests
# ===========================================================================


class TestUntrustedContentAndPolicySummary:
    """Tests for UntrustedContent cryptographic envelope and PolicySummary."""

    def test_untrusted_content_valid_hash_integrity(self) -> None:
        content_text = "arbitrary untrusted prose"
        expected_digest = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
        envelope = UntrustedContent(
            source_kind=UntrustedSourceKind.TASK,
            source_reference="task-ref",
            content=content_text,
            content_digest=expected_digest,
            original_byte_count=len(content_text.encode("utf-8")),
            truncated=False,
        )
        assert envelope.content_digest == expected_digest

    def test_untrusted_content_rejects_tampered_digest(self) -> None:
        content_text = "genuine content"
        tampered_digest = "f" * 64
        with pytest.raises(
            ValidationError, match="content_digest does not match UTF-8 content hash"
        ):
            UntrustedContent(
                source_kind=UntrustedSourceKind.TASK,
                source_reference="task-ref",
                content=content_text,
                content_digest=tampered_digest,
                original_byte_count=len(content_text.encode("utf-8")),
                truncated=False,
            )

    def test_untrusted_content_truncation_invariants(self) -> None:
        content_text = "short text"
        digest = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
        byte_len = len(content_text.encode("utf-8"))

        # truncated requires original_byte_count > byte_len
        with pytest.raises(ValidationError, match="strictly greater than content byte count"):
            UntrustedContent(
                source_kind=UntrustedSourceKind.TASK,
                source_reference="task-ref",
                content=content_text,
                content_digest=digest,
                original_byte_count=byte_len,
                truncated=True,
            )

        # untruncated requires original_byte_count == byte_len
        with pytest.raises(ValidationError, match="equal to content byte count"):
            UntrustedContent(
                source_kind=UntrustedSourceKind.TASK,
                source_reference="task-ref",
                content=content_text,
                content_digest=digest,
                original_byte_count=byte_len + 5,
                truncated=False,
            )

    def test_policy_summary_rejects_nil_uuid(self) -> None:
        with pytest.raises(ValidationError, match="must not be a nil UUID"):
            build_policy_summary(policy_id=_NIL_UUID)

    def test_policy_summary_from_project_policy(self) -> None:
        policy = ProjectPolicy(
            id=uuid4(),
            version=1,
            repository_path=str(Path("/repo").resolve()),
            github_repository="owner/repo",
            default_branch="main",
            commands=(),
        )
        summary = PolicySummary.from_policy(policy)
        assert summary.policy_id == policy.id
        assert summary.policy_version == policy.version
        assert summary.runner_mode == RunnerMode.DOCKER


# ===========================================================================
# Role Input Contracts (PlannerInput, DeveloperInput, ReviewerInput)
# ===========================================================================


class TestRoleInputContracts:
    """Tests for role input contracts, extra-field rejection, and reviewer isolation."""

    def test_developer_receives_bounded_untrusted_check_evidence(self) -> None:
        data = build_developer_input().model_dump()
        evidence = build_untrusted_content("A controller check failed")
        data["check_evidence"] = [evidence]
        context = DeveloperInput.model_validate(data)
        assert context.check_evidence == (evidence,)
        data["check_evidence"] = [evidence] * 101
        with pytest.raises(ValidationError):
            DeveloperInput.model_validate(data)

    def test_developer_check_evidence_counts_toward_total_context_bound(self) -> None:
        data = build_developer_input().model_dump()
        data["check_evidence"] = [build_untrusted_content("x" * 1_048_576)] * 4
        with pytest.raises(ValidationError, match="context"):
            DeveloperInput.model_validate(data)

    def test_planner_input_forbids_extra_fields(self) -> None:
        data = build_planner_input().model_dump()
        data["extra_notes"] = "forbidden"
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            PlannerInput.model_validate(data)

    def test_developer_input_forbids_extra_fields(self) -> None:
        data = build_developer_input().model_dump()
        data["extra_notes"] = "forbidden"
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            DeveloperInput.model_validate(data)

    def test_developer_input_rejects_invalid_worktree_id(self) -> None:
        with pytest.raises(ValidationError, match="worktree_id is invalid"):
            build_developer_input(worktree_id="-invalid-leading-hyphen")
        with pytest.raises(ValidationError, match="worktree_id is invalid"):
            build_developer_input(worktree_id="invalid_underscores")
        with pytest.raises(ValidationError, match="worktree_id is invalid"):
            build_developer_input(worktree_id="UPPERCASE")

    def test_reviewer_input_forbids_developer_private_reasoning_or_summary(self) -> None:
        data = build_reviewer_input().model_dump()
        data["developer_private_summary"] = "Developer private chain of thought"
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ReviewerInput.model_validate(data)

        data2 = build_reviewer_input().model_dump()
        data2["developer_reasoning"] = "Private scratchpad"
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ReviewerInput.model_validate(data2)

    def test_reviewer_input_has_no_developer_private_fields(self) -> None:
        req = build_agent_request(role=AgentRole.REVIEWER)
        context_dict = req.context.model_dump()
        assert "developer_private_summary" not in context_dict
        assert "developer_reasoning" not in context_dict
        assert "scratchpad" not in context_dict
        assert "worktree_id" not in context_dict


# ===========================================================================
# AgentBudget Contract Tests
# ===========================================================================


class TestAgentBudgetContract:
    """Tests for the AgentBudget contract."""

    def test_default_budget_values_match_specification(self) -> None:
        budget = AgentBudget()
        assert budget.max_input_tokens == 100_000
        assert budget.max_output_tokens == 16_000
        assert budget.max_tool_calls == 100
        assert budget.max_duration_seconds == 1800
        assert budget.max_cost_minor == 1000

    def test_agent_budget_strict_integer_types(self) -> None:
        # Pydantic strict=True forbids passing booleans as integers
        with pytest.raises(ValidationError):
            AgentBudget.model_validate({"max_input_tokens": True})

    def test_agent_budget_bounds(self) -> None:
        with pytest.raises(ValidationError):
            AgentBudget(max_input_tokens=0)
        with pytest.raises(ValidationError):
            AgentBudget(max_duration_seconds=0)
        with pytest.raises(ValidationError):
            AgentBudget(max_tool_calls=-1)
        with pytest.raises(ValidationError):
            AgentBudget(max_cost_minor=-1)

    def test_agent_budget_from_model_policy(self) -> None:
        model_policy = AgentModelPolicy(
            max_input_tokens=50_000,
            max_output_tokens=8_000,
            max_tool_calls=25,
            max_duration_seconds=600,
            max_cost_minor=500,
        )
        budget = AgentBudget.from_model_policy(model_policy)
        assert budget.max_input_tokens == 50_000
        assert budget.max_output_tokens == 8_000
        assert budget.max_tool_calls == 25
        assert budget.max_duration_seconds == 600
        assert budget.max_cost_minor == 500


# ===========================================================================
# AgentRequest Contract Tests
# ===========================================================================


class TestAgentRequestContract:
    """Tests for AgentRequest integrity, role-context pairing, and tool allowlists."""

    def test_valid_planner_developer_reviewer_requests(self) -> None:
        req_plan = build_agent_request(role=AgentRole.PLANNER)
        assert req_plan.role == AgentRole.PLANNER
        assert isinstance(req_plan.context, PlannerInput)

        req_dev = build_agent_request(role=AgentRole.DEVELOPER)
        assert req_dev.role == AgentRole.DEVELOPER
        assert isinstance(req_dev.context, DeveloperInput)

        req_rev = build_agent_request(role=AgentRole.REVIEWER)
        assert req_rev.role == AgentRole.REVIEWER
        assert isinstance(req_rev.context, ReviewerInput)

    def test_role_context_mismatch_rejected(self) -> None:
        dev_ctx = build_developer_input()
        planner_ctx = build_planner_input()
        reviewer_ctx = build_reviewer_input()

        # Planner role with DeveloperInput
        with pytest.raises(
            ValidationError, match="context type DeveloperInput does not match role planner"
        ):
            build_agent_request(role=AgentRole.PLANNER, context=dev_ctx)

        # Developer role with ReviewerInput
        with pytest.raises(
            ValidationError, match="context type ReviewerInput does not match role developer"
        ):
            build_agent_request(role=AgentRole.DEVELOPER, context=reviewer_ctx)

        # Reviewer role with PlannerInput
        with pytest.raises(
            ValidationError, match="context type PlannerInput does not match role reviewer"
        ):
            build_agent_request(role=AgentRole.REVIEWER, context=planner_ctx)

    def test_reviewer_must_be_fresh_with_no_parent_execution_id(self) -> None:
        with pytest.raises(
            ValidationError,
            match="reviewer executions must be fresh and have no parent_execution_id",
        ):
            build_agent_request(
                role=AgentRole.REVIEWER,
                parent_execution_id=uuid4(),
            )

    def test_developer_and_planner_may_have_parent_execution_id(self) -> None:
        parent_id = uuid4()
        req_dev = build_agent_request(role=AgentRole.DEVELOPER, parent_execution_id=parent_id)
        assert req_dev.parent_execution_id == parent_id

        req_plan = build_agent_request(role=AgentRole.PLANNER, parent_execution_id=parent_id)
        assert req_plan.parent_execution_id == parent_id

    def test_tool_allowlists_enforced_per_role(self) -> None:
        # Planner cannot have write or execution tools
        with pytest.raises(ValidationError, match="not permitted for role planner"):
            build_agent_request(
                role=AgentRole.PLANNER,
                allowed_tools=(ToolName.REPOSITORY_WRITE_FILE,),
            )
        with pytest.raises(ValidationError, match="not permitted for role planner"):
            build_agent_request(
                role=AgentRole.PLANNER,
                allowed_tools=(ToolName.GIT_COMMIT,),
            )

        # Reviewer cannot have write or commit tools
        with pytest.raises(ValidationError, match="not permitted for role reviewer"):
            build_agent_request(
                role=AgentRole.REVIEWER,
                allowed_tools=(ToolName.REPOSITORY_WRITE_FILE,),
            )
        with pytest.raises(ValidationError, match="not permitted for role reviewer"):
            build_agent_request(
                role=AgentRole.REVIEWER,
                allowed_tools=(ToolName.GIT_COMMIT,),
            )

        # Developer cannot have review validation reading tools
        with pytest.raises(ValidationError, match="not permitted for role developer"):
            build_agent_request(
                role=AgentRole.DEVELOPER,
                allowed_tools=(ToolName.VALIDATION_RESULTS_READ,),
            )

    def test_instruction_digest_must_match_system_instruction(self) -> None:
        data = build_agent_request().model_dump()
        data["instruction_digest"] = "f" * 64
        with pytest.raises(
            ValidationError, match="instruction_digest does not match system_instruction hash"
        ):
            AgentRequest.model_validate(data)

    def test_system_instruction_cannot_contain_untrusted_context_content(self) -> None:
        leaked_prose = "Leaked untrusted text from task"
        ctx = build_planner_input(original_task=build_untrusted_content(leaked_prose))
        with pytest.raises(
            ValidationError, match="system_instruction must not contain untrusted context content"
        ):
            build_agent_request(
                role=AgentRole.PLANNER,
                context=ctx,
                system_instruction=f"System prompt: {leaked_prose}",
            )

    def test_agent_request_rejects_nil_uuid(self) -> None:
        with pytest.raises(ValidationError, match="request identifier must not be a nil UUID"):
            build_agent_request(execution_id=_NIL_UUID)


# ===========================================================================
# AgentResult Contract Tests
# ===========================================================================


class TestAgentResultContract:
    """Tests for AgentResult invariants, usage identity agreement, and output pairing."""

    def test_valid_succeeded_agent_result_passes(self) -> None:
        result = build_agent_result()
        assert result.finish_status == AgentFinishStatus.SUCCEEDED
        assert isinstance(result.output, PlanOutput)

    def test_succeeded_result_requires_output(self) -> None:
        with pytest.raises(
            ValidationError, match="successful agent execution requires a non-null output"
        ):
            build_agent_result(
                finish_status=AgentFinishStatus.SUCCEEDED,
                output=None,
            )

    def test_succeeded_result_output_must_match_role(self) -> None:
        # Planner role with DeveloperOutput
        with pytest.raises(ValidationError, match="planner result output must be a PlanOutput"):
            build_agent_result(
                role=AgentRole.PLANNER,
                finish_status=AgentFinishStatus.SUCCEEDED,
                output=build_developer_output(),
            )

        # Developer role with ReviewOutput
        with pytest.raises(
            ValidationError, match="developer result output must be a DeveloperOutput"
        ):
            build_agent_result(
                role=AgentRole.DEVELOPER,
                finish_status=AgentFinishStatus.SUCCEEDED,
                output=build_review_output(),
            )

        # Reviewer role with PlanOutput
        with pytest.raises(ValidationError, match="reviewer result output must be a ReviewOutput"):
            build_agent_result(
                role=AgentRole.REVIEWER,
                finish_status=AgentFinishStatus.SUCCEEDED,
                output=build_plan_output(),
            )

    def test_failed_or_budget_exceeded_result_allows_none_output(self) -> None:
        res_budget = build_agent_result(
            finish_status=AgentFinishStatus.BUDGET_EXCEEDED,
            output=None,
        )
        assert res_budget.output is None

        res_timed_out = build_agent_result(
            finish_status=AgentFinishStatus.TIMED_OUT,
            output=None,
        )
        assert res_timed_out.output is None

    def test_reviewer_result_must_have_no_parent_execution_id(self) -> None:
        with pytest.raises(
            ValidationError, match="reviewer results must be fresh and have no parent_execution_id"
        ):
            build_agent_result(
                role=AgentRole.REVIEWER,
                parent_execution_id=uuid4(),
            )

    def test_result_must_agree_with_usage_record(self) -> None:
        usage = build_usage_record(provider="other-provider")
        with pytest.raises(ValidationError, match="result provider must agree with usage provider"):
            build_agent_result(provider="fake-provider", usage=usage)

        usage_model = build_usage_record(model="other-model")
        with pytest.raises(ValidationError, match="result model must agree with usage model"):
            build_agent_result(model="fake-model", usage=usage_model)

        usage_calls = build_usage_record(tool_call_count=10)
        with pytest.raises(
            ValidationError, match="result tool_call_count must agree with usage tool_call_count"
        ):
            build_agent_result(tool_call_count=5, usage=usage_calls)

        usage_duration = build_usage_record(duration_ms=5000)
        with pytest.raises(
            ValidationError, match="result duration_ms must agree with usage duration_ms"
        ):
            build_agent_result(duration_ms=1000, usage=usage_duration)
