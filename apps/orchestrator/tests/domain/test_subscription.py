from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.domain.subscription import (
    SPECIALIST_ALLOWED_TOOLS,
    SPECIALIST_CAPABILITIES,
    AcceptanceCriterion,
    AcceptDecision,
    AttemptIdentity,
    AttemptTelemetry,
    AuthMode,
    BillingMode,
    BrokerAuthorizationBinding,
    BudgetPool,
    Capability,
    CheckResultEvidence,
    DecisionKind,
    DelegateDecision,
    ExecutionEnvelope,
    FailureReason,
    HandoffStatus,
    LogicalTaskContract,
    ModelMapping,
    OperatorProfile,
    QuotaStatus,
    ReasoningEffort,
    ReassignDecision,
    ReviewSelection,
    RolePreference,
    RouteBinding,
    RouteMapping,
    RouteSpec,
    ScopeRequestDecision,
    ScopeResponseDecision,
    SpecialistPurpose,
    TaskBudget,
    TaskHandoff,
    TaskLineage,
    ToolCallBinding,
    UnknownTelemetryPolicy,
    WaitDecision,
    can_delegate,
    decode_subscription_record,
    encode_subscription_record,
    is_read_only,
    permits_fallback,
    validate_task_dag,
    validate_tool_permission,
)
from forge.domain.tool import ToolName


def _sample_sha256() -> str:
    return "a" * 64


@pytest.mark.parametrize("container", ["envelope", "task"])
def test_primary_substitution_cannot_hide_behind_nonprimary_binding_flag(container) -> None:
    requested = _sample_route(model="astra")
    effective = _sample_route(model="luna")
    binding = RouteBinding(
        requested=requested,
        effective=effective,
        mapping_applied=RouteMapping(
            requested=requested,
            effective=effective,
            approved_by="operator",
            approval_id="mapping",
            reason="worker fallback",
        ),
        is_primary=False,
    )
    with pytest.raises(ValueError, match="primary.*substitution"):
        if container == "envelope":
            ExecutionEnvelope(
                run_id=uuid4(),
                profile_id=uuid4(),
                profile_version=1,
                safety_policy_version=1,
                routes=((SpecialistPurpose.PRIMARY, binding),),
            )
        else:
            LogicalTaskContract(
                run_id=uuid4(),
                task_id=uuid4(),
                purpose=SpecialistPurpose.PRIMARY,
                route=binding,
                budget=TaskBudget(),
            )


def test_ephemeral_broker_credentials_cannot_enter_durable_record_codec() -> None:
    binding = BrokerAuthorizationBinding(
        run_id=uuid4(),
        task_id=uuid4(),
        attempt_id=uuid4(),
        worktree_id="worktree-1",
        role=SpecialistPurpose.PLANNING,
        policy_version=1,
        permitted_tools=frozenset(),
        broker_token="never-persist-this-token",
    )
    assert binding.broker_token not in repr(binding)
    with pytest.raises(TypeError):
        encode_subscription_record(binding)


def _sample_commit() -> str:
    return "b" * 40


def _sample_route(
    *,
    provider: str = "openai",
    client: str = "codex",
    model: str = "astra",
    effort: ReasoningEffort = ReasoningEffort.LOW,
    auth_mode: AuthMode = AuthMode.SUBSCRIPTION,
    billing_mode: BillingMode = BillingMode.ALLOWANCE_ONLY,
) -> RouteSpec:
    return RouteSpec(
        provider=provider,
        client=client,
        model=model,
        effort=effort,
        auth_mode=auth_mode,
        billing_mode=billing_mode,
    )


def _sample_binding(
    *,
    model: str = "astra",
    is_primary: bool = False,
) -> RouteBinding:
    route = _sample_route(model=model)
    return RouteBinding(requested=route, effective=route, is_primary=is_primary)


def _sample_delegate_decision() -> DelegateDecision:
    run_id, parent_id = uuid4(), uuid4()
    child = LogicalTaskContract(
        run_id=run_id,
        task_id=uuid4(),
        parent_task_id=parent_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=_sample_binding(),
        budget=TaskBudget(),
    )
    return DelegateDecision(
        run_id=run_id, parent_task_id=parent_id, child_tasks=(child,), rationale="delegate"
    )


# =========================================================================
# Acceptance Criterion 1: Profiles, Routes, Mappings, Envelopes
# =========================================================================


def test_route_spec_validates_fields_and_is_immutable() -> None:
    route = _sample_route()
    assert route.provider == "openai"
    assert route.model == "astra"
    assert route.effort == ReasoningEffort.LOW

    with pytest.raises(FrozenInstanceError):
        route.model = "other"  # type: ignore[misc]

    with pytest.raises(ValueError, match="provider must not be blank"):
        RouteSpec(provider="   ", client="c", model="m")


def test_route_mapping_requires_explicit_approved_mapping_for_different_routes() -> None:
    req = _sample_route(model="gemini-3.8-flash-medium")
    eff = _sample_route(model="gemini-3.8-flash")

    # Without mapping, differing models are rejected
    with pytest.raises(
        ValueError,
        match="differing requested and effective route requires explicit approved mapping",
    ):
        RouteBinding(requested=req, effective=eff)

    mapping = RouteMapping(
        requested=req,
        effective=eff,
        approved_by="operator",
        approval_id="approval-1",
        reason="official alias on this host",
    )
    binding = RouteBinding(requested=req, effective=eff, mapping_applied=mapping)
    assert binding.requested.model == "gemini-3.8-flash-medium"
    assert binding.effective.model == "gemini-3.8-flash"
    assert binding.mapping_applied == mapping


def test_route_mapping_binds_the_full_requested_and_effective_route() -> None:
    req = _sample_route(model="gemini-3.8-flash-medium")
    eff = _sample_route(model="gemini-3.8-flash")

    mismatched_mapping = RouteMapping(
        requested=_sample_route(model="gemini-3.8-flash-medium", client="other-client"),
        effective=eff,
        approved_by="operator",
        approval_id="approval-1",
        reason="alias",
    )
    with pytest.raises(ValueError, match="mapping requested route does not match requested route"):
        RouteBinding(requested=req, effective=eff, mapping_applied=mismatched_mapping)


def test_primary_route_cannot_fallback_or_switch_provider() -> None:
    primary_req = _sample_route(provider="openai", model="astra")
    primary_eff = _sample_route(provider="anthropic", model="claude-opus-5")

    with pytest.raises(
        ValueError,
        match="differing requested and effective route requires explicit approved mapping",
    ):
        RouteBinding(requested=primary_req, effective=primary_eff, is_primary=True)

    mapping = RouteMapping(
        requested=primary_req,
        effective=primary_eff,
        approved_by="operator",
        approval_id="approval-1",
        reason="unauthorized provider switch attempt",
    )
    with pytest.raises(ValueError, match="primary route substitution is prohibited"):
        RouteBinding(
            requested=primary_req,
            effective=primary_eff,
            mapping_applied=mapping,
            is_primary=True,
        )


def test_operator_profile_stores_preferences_and_mappings() -> None:
    profile_id = uuid4()
    primary_pref = RolePreference(
        purpose=SpecialistPurpose.PRIMARY,
        preferred_route=_sample_route(model="astra"),
    )
    worker_pref = RolePreference(
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        preferred_route=_sample_route(
            provider="google", client="gemini", model="gemini-3.8-flash-medium"
        ),
        fallback_routes=(_sample_route(provider="openai", client="codex", model="luna"),),
    )
    profile = OperatorProfile(
        profile_id=profile_id,
        version=1,
        preferences=(primary_pref, worker_pref),
    )
    assert profile.version == 1
    assert profile.preference_for(SpecialistPurpose.PRIMARY) == primary_pref
    assert profile.preference_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION) == worker_pref

    with pytest.raises(KeyError, match="no preference configured"):
        profile.preference_for(SpecialistPurpose.PLANNING)


def test_operator_profile_rejects_duplicate_preferences() -> None:
    pref1 = RolePreference(
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        preferred_route=_sample_route(model="m1"),
    )
    pref2 = RolePreference(
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        preferred_route=_sample_route(model="m2"),
    )
    with pytest.raises(ValueError, match="duplicate preferences"):
        OperatorProfile(profile_id=uuid4(), version=1, preferences=(pref1, pref2))


def test_execution_envelope_freezes_distinct_safety_and_profile_versions() -> None:
    run_id = uuid4()
    profile_id = uuid4()
    primary_route = _sample_route(model="astra")
    primary_binding = RouteBinding(
        requested=primary_route, effective=primary_route, is_primary=True
    )
    created_at = datetime.now(UTC)

    envelope = ExecutionEnvelope(
        run_id=run_id,
        profile_id=profile_id,
        profile_version=1,
        safety_policy_version=3,
        routes=((SpecialistPurpose.PRIMARY, primary_binding),),
        allowed_fallbacks=(),
        billing_mode=BillingMode.ALLOWANCE_ONLY,
        created_at=created_at,
    )
    assert envelope.profile_version == 1
    assert envelope.safety_policy_version == 3
    assert envelope.route_for(SpecialistPurpose.PRIMARY) == primary_binding
    assert envelope.created_at == created_at
    assert envelope.fallbacks_for(SpecialistPurpose.PRIMARY) == ()


def test_execution_envelope_rejects_api_key_in_allowance_only_mode() -> None:
    run_id = uuid4()
    profile_id = uuid4()
    api_key_route = _sample_route(auth_mode=AuthMode.API_KEY, billing_mode=BillingMode.PAID_OPT_IN)
    binding = RouteBinding(requested=api_key_route, effective=api_key_route)

    with pytest.raises(ValueError, match="allowance-only envelope rejects api_key mode"):
        ExecutionEnvelope(
            run_id=run_id,
            profile_id=profile_id,
            profile_version=1,
            safety_policy_version=1,
            routes=((SpecialistPurpose.ROUTINE_IMPLEMENTATION, binding),),
            billing_mode=BillingMode.ALLOWANCE_ONLY,
        )


def test_execution_envelope_rejects_primary_fallback() -> None:
    run_id = uuid4()
    profile_id = uuid4()
    primary_binding = _sample_binding(model="astra", is_primary=True)
    fallback_route = _sample_route(model="sol")

    with pytest.raises(ValueError, match="primary fallback prohibited in execution envelope"):
        ExecutionEnvelope(
            run_id=run_id,
            profile_id=profile_id,
            profile_version=1,
            safety_policy_version=1,
            routes=((SpecialistPurpose.PRIMARY, primary_binding),),
            allowed_fallbacks=((SpecialistPurpose.PRIMARY, (fallback_route,)),),
        )


# =========================================================================
# Acceptance Criterion 2: Closed Capability Sets, Delegate and Reviewer
# =========================================================================


def test_specialist_capabilities_map_separates_purpose_from_tools() -> None:
    assert Capability.DELEGATE in SPECIALIST_CAPABILITIES[SpecialistPurpose.PRIMARY]
    for purpose, caps in SPECIALIST_CAPABILITIES.items():
        if purpose is not SpecialistPurpose.PRIMARY:
            assert Capability.DELEGATE not in caps

    # Reviewer has only READ and REVIEW_READ
    reviewer_caps = SPECIALIST_CAPABILITIES[SpecialistPurpose.INDEPENDENT_REVIEW]
    assert reviewer_caps == frozenset({Capability.READ, Capability.REVIEW_READ})


def test_specialist_allowed_tools_enforces_reviewer_read_only() -> None:
    reviewer_tools = SPECIALIST_ALLOWED_TOOLS[SpecialistPurpose.INDEPENDENT_REVIEW]
    assert ToolName.REPOSITORY_READ_FILE in reviewer_tools
    assert ToolName.VALIDATION_RESULTS_READ in reviewer_tools
    assert ToolName.REVIEW_ARTIFACTS_READ in reviewer_tools
    assert ToolName.REPOSITORY_WRITE_FILE not in reviewer_tools
    assert ToolName.GIT_COMMIT not in reviewer_tools


def test_only_primary_can_delegate() -> None:
    assert can_delegate(SpecialistPurpose.PRIMARY) is True
    for purpose in SpecialistPurpose:
        if purpose is not SpecialistPurpose.PRIMARY:
            assert can_delegate(purpose) is False


def test_read_only_specialists_are_identified() -> None:
    assert is_read_only(SpecialistPurpose.INDEPENDENT_REVIEW) is True
    assert is_read_only(SpecialistPurpose.PLANNING) is True
    assert is_read_only(SpecialistPurpose.SECURITY) is True
    assert is_read_only(SpecialistPurpose.PRIMARY) is False
    assert is_read_only(SpecialistPurpose.ROUTINE_IMPLEMENTATION) is False
    assert is_read_only(SpecialistPurpose.COMPLEX_IMPLEMENTATION) is False
    assert is_read_only(SpecialistPurpose.EXPLORATION) is False


def test_validate_tool_permission_enforces_closed_vocabulary() -> None:
    # Primary can read and write
    validate_tool_permission(SpecialistPurpose.PRIMARY, ToolName.REPOSITORY_READ_FILE)
    validate_tool_permission(SpecialistPurpose.PRIMARY, ToolName.REPOSITORY_WRITE_FILE)

    # Independent review is read-only
    validate_tool_permission(SpecialistPurpose.INDEPENDENT_REVIEW, ToolName.REPOSITORY_READ_FILE)
    validate_tool_permission(SpecialistPurpose.INDEPENDENT_REVIEW, ToolName.VALIDATION_RESULTS_READ)
    with pytest.raises(ValueError, match="is not permitted for role independent_review"):
        validate_tool_permission(
            SpecialistPurpose.INDEPENDENT_REVIEW, ToolName.REPOSITORY_WRITE_FILE
        )

    # Planning cannot write
    with pytest.raises(ValueError, match="is not permitted for role planning"):
        validate_tool_permission(SpecialistPurpose.PLANNING, ToolName.REPOSITORY_WRITE_FILE)


def test_task_lineage_separates_task_hierarchy_from_provider_session() -> None:
    task_id = uuid4()
    root_id = uuid4()
    lineage = TaskLineage(
        task_id=task_id,
        lineage_root_task_id=root_id,
        fresh_conversation=True,
    )
    assert lineage.fresh_conversation is True
    assert lineage.provider_session_id is None


# =========================================================================
# Acceptance Criterion 3: Logical Task Contracts and DAG Validation
# =========================================================================


def test_logical_task_contract_validates_fields_and_normalizes_paths() -> None:
    run_id = uuid4()
    task_id = uuid4()
    binding = _sample_binding()
    budget = TaskBudget()

    contract = LogicalTaskContract(
        run_id=run_id,
        task_id=task_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=binding,
        budget=budget,
        owned_paths=("src/counter.py", "tests/test_counter.py"),
        named_checks=("unit",),
        typed_acceptance=(
            AcceptanceCriterion(criterion_id="increment", description="Fix increment"),
        ),
        untrusted_context_refs=("ctx-1",),
    )
    assert contract.owned_paths == ("src/counter.py", "tests/test_counter.py")
    assert contract.named_checks == ("unit",)


def test_logical_task_contract_rejects_read_only_specialist_with_owned_paths() -> None:
    run_id = uuid4()
    task_id = uuid4()
    binding = _sample_binding()
    budget = TaskBudget()

    with pytest.raises(
        ValueError, match="read-only specialist independent_review cannot own writable paths"
    ):
        LogicalTaskContract(
            run_id=run_id,
            task_id=task_id,
            purpose=SpecialistPurpose.INDEPENDENT_REVIEW,
            route=binding,
            budget=budget,
            owned_paths=("src/counter.py",),
        )


def test_logical_task_contract_rejects_shell_characters_in_named_checks() -> None:
    run_id = uuid4()
    task_id = uuid4()
    binding = _sample_binding()
    budget = TaskBudget()

    with pytest.raises(ValueError, match="named check 'unit; rm -rf /' is invalid"):
        LogicalTaskContract(
            run_id=run_id,
            task_id=task_id,
            purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
            route=binding,
            budget=budget,
            named_checks=("unit; rm -rf /",),
        )


def test_validate_task_dag_passes_for_clean_hierarchy() -> None:
    run_id = uuid4()
    primary_id = uuid4()
    worker_a_id = uuid4()
    worker_b_id = uuid4()
    binding = _sample_binding()
    budget = TaskBudget()

    primary_task = LogicalTaskContract(
        run_id=run_id,
        task_id=primary_id,
        purpose=SpecialistPurpose.PRIMARY,
        route=binding,
        budget=budget,
        owned_paths=("alpha/value.txt", "beta/value.txt"),
    )
    worker_a = LogicalTaskContract(
        run_id=run_id,
        task_id=worker_a_id,
        parent_task_id=primary_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=binding,
        budget=budget,
        owned_paths=("alpha/value.txt",),
    )
    worker_b = LogicalTaskContract(
        run_id=run_id,
        task_id=worker_b_id,
        parent_task_id=primary_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=binding,
        budget=budget,
        dependency_task_ids=(worker_a_id,),
        owned_paths=("beta/value.txt",),
    )

    # Clean DAG validates without error
    validate_task_dag([primary_task, worker_a, worker_b])


def test_validate_task_dag_rejects_foreign_lineage() -> None:
    run_id1 = uuid4()
    run_id2 = uuid4()
    task1 = LogicalTaskContract(
        run_id=run_id1,
        task_id=uuid4(),
        purpose=SpecialistPurpose.PRIMARY,
        route=_sample_binding(),
        budget=TaskBudget(),
    )
    task2 = LogicalTaskContract(
        run_id=run_id2,
        task_id=uuid4(),
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=_sample_binding(),
        budget=TaskBudget(),
    )
    with pytest.raises(ValueError, match="foreign lineage"):
        validate_task_dag([task1, task2])


def test_validate_task_dag_rejects_scope_escalation() -> None:
    run_id = uuid4()
    primary_id = uuid4()
    child_id = uuid4()

    primary = LogicalTaskContract(
        run_id=run_id,
        task_id=primary_id,
        purpose=SpecialistPurpose.PRIMARY,
        route=_sample_binding(),
        budget=TaskBudget(),
        owned_paths=("src/counter.py",),
    )
    # Child attempts to claim 'src/other.py' not owned by parent
    child = LogicalTaskContract(
        run_id=run_id,
        task_id=child_id,
        parent_task_id=primary_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=_sample_binding(),
        budget=TaskBudget(),
        owned_paths=("src/counter.py", "src/other.py"),
    )
    with pytest.raises(ValueError, match="escalates scope"):
        validate_task_dag([primary, child])


def test_validate_task_dag_rejects_non_primary_parent() -> None:
    run_id = uuid4()
    worker1_id = uuid4()
    child_id = uuid4()

    worker1 = LogicalTaskContract(
        run_id=run_id,
        task_id=worker1_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=_sample_binding(),
        budget=TaskBudget(),
        owned_paths=("src/counter.py",),
    )
    child = LogicalTaskContract(
        run_id=run_id,
        task_id=child_id,
        parent_task_id=worker1_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=_sample_binding(),
        budget=TaskBudget(),
        owned_paths=("src/counter.py",),
    )
    with pytest.raises(ValueError, match="cannot delegate"):
        validate_task_dag([worker1, child])


def test_validate_task_dag_rejects_dependency_cycle() -> None:
    run_id = uuid4()
    task1_id = uuid4()
    task2_id = uuid4()

    task1 = LogicalTaskContract(
        run_id=run_id,
        task_id=task1_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=_sample_binding(),
        budget=TaskBudget(),
        dependency_task_ids=(task2_id,),
    )
    task2 = LogicalTaskContract(
        run_id=run_id,
        task_id=task2_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=_sample_binding(),
        budget=TaskBudget(),
        dependency_task_ids=(task1_id,),
    )
    with pytest.raises(ValueError, match="contain a cycle"):
        validate_task_dag([task1, task2])


def test_validate_task_dag_rejects_out_of_run_dependency() -> None:
    run_id = uuid4()
    missing_id = uuid4()
    task = LogicalTaskContract(
        run_id=run_id,
        task_id=uuid4(),
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=_sample_binding(),
        budget=TaskBudget(),
        dependency_task_ids=(missing_id,),
    )
    with pytest.raises(ValueError, match="does not exist in run"):
        validate_task_dag([task])


# =========================================================================
# Acceptance Criterion 4: Typed Decisions, Handoffs, Attempt/Tool Identity
# =========================================================================


def test_decision_kind_and_failure_reason_closed_vocabularies() -> None:
    assert DecisionKind.DELEGATE.value == "delegate"
    assert DecisionKind.WAIT.value == "wait"
    assert DecisionKind.SCOPE_REQUEST.value == "scope_request"
    assert DecisionKind.SCOPE_RESPONSE.value == "scope_response"
    assert DecisionKind.ACCEPT.value == "accept"
    assert DecisionKind.REASSIGN.value == "reassign"
    assert DecisionKind.SELECT_REVIEW.value == "select_review"

    assert FailureReason.QUOTA_EXHAUSTED.value == "quota_exhausted"
    assert FailureReason.LOGIN_FAILED.value == "login_failed"
    assert FailureReason.UNSUPPORTED_MODEL.value == "unsupported_model"
    assert FailureReason.OUTAGE.value == "outage"
    assert FailureReason.BUDGET_EXHAUSTED.value == "budget_exhausted"
    assert FailureReason.UNCERTAIN_TERMINATION.value == "uncertain_termination"


def test_delegate_decision_requires_child_tasks_matching_parent_and_run() -> None:
    run_id = uuid4()
    parent_id = uuid4()
    child_id = uuid4()
    child = LogicalTaskContract(
        run_id=run_id,
        task_id=child_id,
        parent_task_id=parent_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=_sample_binding(),
        budget=TaskBudget(),
    )
    decision = DelegateDecision(
        run_id=run_id,
        parent_task_id=parent_id,
        child_tasks=(child,),
        rationale="Delegate routine writing",
    )
    assert len(decision.child_tasks) == 1

    with pytest.raises(ValueError, match="child_tasks must not be empty"):
        DelegateDecision(
            run_id=run_id,
            parent_task_id=parent_id,
            child_tasks=(),
            rationale="Empty",
        )


def test_wait_decision_requires_task_ids() -> None:
    run_id = uuid4()
    task_id = uuid4()
    child_id = uuid4()

    wait = WaitDecision(
        run_id=run_id,
        task_id=task_id,
        waiting_on_task_ids=(child_id,),
        reason="Waiting for child completion",
    )
    assert wait.waiting_on_task_ids == (child_id,)

    with pytest.raises(ValueError, match="waiting_on_task_ids must not be empty"):
        WaitDecision(
            run_id=run_id,
            task_id=task_id,
            waiting_on_task_ids=(),
            reason="Empty wait",
        )


def test_scope_request_and_response_decisions() -> None:
    run_id = uuid4()
    task_id = uuid4()

    req = ScopeRequestDecision(
        run_id=run_id,
        task_id=task_id,
        requested_paths=("src/new.py",),
        reason="Need additional file",
    )
    assert req.requested_paths == ("src/new.py",)

    resp = ScopeResponseDecision(
        run_id=run_id,
        task_id=task_id,
        granted_paths=("src/new.py",),
        denied_paths=(),
        reason="Approved by primary",
    )
    assert resp.granted_paths == ("src/new.py",)


def test_reassign_decision_validates_route_and_preserves_work() -> None:
    run_id = uuid4()
    task_id = uuid4()
    new_route = _sample_route(provider="openai", client="codex", model="luna")

    reassign = ReassignDecision(
        run_id=run_id,
        task_id=task_id,
        new_route=new_route,
        reason="Gemini quota exhausted, falling back to Luna",
        preserve_partial_work=True,
    )
    assert reassign.new_route.model == "luna"
    assert reassign.preserve_partial_work is True


def test_accept_decision_requires_evidence_and_candidate() -> None:
    run_id = uuid4()
    task_id = uuid4()
    tree = _sample_sha256()

    with pytest.raises(ValueError, match="acceptance requires evidence_receipt_ids"):
        AcceptDecision(
            run_id=run_id,
            task_id=task_id,
            candidate_commit=_sample_commit(),
            candidate_tree_digest=tree,
            evidence_receipt_ids=(),
            rationale="Blind acceptance",
        )

    accept = AcceptDecision(
        run_id=run_id,
        task_id=task_id,
        candidate_commit=_sample_commit(),
        candidate_tree_digest=tree,
        evidence_receipt_ids=("receipt-123",),
        rationale="Verified green tests",
    )
    assert accept.candidate_tree_digest == tree


def test_review_selection_when_no_review_requires_explicit_reason() -> None:
    run_id = uuid4()
    tree = _sample_sha256()

    with pytest.raises(
        ValueError, match="no-review selection requires an explicit no_review_reason"
    ):
        ReviewSelection(
            run_id=run_id,
            candidate_commit=_sample_commit(),
            candidate_tree_digest=tree,
            review_required=False,
            no_review_reason=None,
        )

    # Valid no-review selection
    selection = ReviewSelection(
        run_id=run_id,
        candidate_commit=_sample_commit(),
        candidate_tree_digest=tree,
        review_required=False,
        no_review_reason="trivial exact-text documentation correction",
    )
    assert selection.review_required is False
    assert selection.no_review_reason == "trivial exact-text documentation correction"


def test_review_selection_when_review_required_requires_route() -> None:
    run_id = uuid4()
    tree = _sample_sha256()

    with pytest.raises(ValueError, match="review_required requires reviewer_route"):
        ReviewSelection(
            run_id=run_id,
            candidate_commit=_sample_commit(),
            candidate_tree_digest=tree,
            review_required=True,
            reviewer_route=None,
        )

    route = _sample_route(
        provider="anthropic", model="claude-opus-5", effort=ReasoningEffort.MEDIUM
    )
    selection = ReviewSelection(
        run_id=run_id,
        candidate_commit=_sample_commit(),
        candidate_tree_digest=tree,
        review_required=True,
        reviewer_route=route,
    )
    assert selection.review_required is True
    assert selection.reviewer_route == route


def test_task_handoff_validates_completed_status_evidence() -> None:
    run_id = uuid4()
    task_id = uuid4()
    attempt_id = uuid4()

    # Completed handoff with empty evidence fails
    with pytest.raises(ValueError, match="completed handoff requires candidate_tree_digest"):
        TaskHandoff(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            status=HandoffStatus.COMPLETED,
            candidate_tree_digest="",
        )

    check_evidence = CheckResultEvidence(
        command_name="unit",
        exit_code=0,
        passed=True,
        output_digest=_sample_sha256(),
        duration_ms=450,
        receipt_id="check-rcpt-1",
    )
    handoff = TaskHandoff(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        status=HandoffStatus.COMPLETED,
        candidate_commit=_sample_commit(),
        candidate_tree_digest=_sample_sha256(),
        changed_paths=("src/counter.py",),
        check_results=(check_evidence,),
        evidence_receipt_ids=("rcpt-1",),
        summary="All tests passed",
    )
    assert handoff.status == HandoffStatus.COMPLETED
    assert len(handoff.check_results) == 1


def test_attempt_identity_validation() -> None:
    run_id = uuid4()
    task_id = uuid4()
    attempt_id = uuid4()

    identity = AttemptIdentity(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        attempt_number=2,
    )
    assert identity.attempt_number == 2

    with pytest.raises(ValueError, match="attempt_number must be an integer >= 1"):
        AttemptIdentity(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            attempt_number=0,
        )


def test_broker_authorization_binding_enforces_policy_and_tools() -> None:
    run_id = uuid4()
    task_id = uuid4()
    attempt_id = uuid4()

    # Valid binding
    binding = BrokerAuthorizationBinding(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worktree_id="worktree-1",
        role=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        policy_version=1,
        permitted_tools=frozenset({ToolName.REPOSITORY_READ_FILE, ToolName.REPOSITORY_WRITE_FILE}),
        broker_token="opaque-token-123",
    )
    assert binding.worktree_id == "worktree-1"

    # Unauthorized tool for role rejected
    with pytest.raises(ValueError, match="exceeds allowed tool permissions"):
        BrokerAuthorizationBinding(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            worktree_id="worktree-1",
            role=SpecialistPurpose.INDEPENDENT_REVIEW,
            policy_version=1,
            permitted_tools=frozenset({ToolName.REPOSITORY_WRITE_FILE}),
            broker_token="opaque-token-123",
        )


def test_tool_call_binding_distinguishes_provider_key_from_durable_operation() -> None:
    attempt_id = uuid4()
    op_id = uuid4()
    provider_call_key = "call_abcdef123456"

    binding = ToolCallBinding(
        attempt_id=attempt_id,
        provider_call_key=provider_call_key,
        durable_operation_id=op_id,
        tool_name=ToolName.REPOSITORY_WRITE_FILE,
        arguments_digest=_sample_sha256(),
    )
    assert binding.provider_call_key != str(binding.durable_operation_id)
    assert binding.tool_name == ToolName.REPOSITORY_WRITE_FILE


# =========================================================================
# Acceptance Criterion 5: Unknown Telemetry, Telemetry Policy and Budgets
# =========================================================================


def test_attempt_telemetry_defaults_tokens_and_costs_to_none_never_zero() -> None:
    telemetry = AttemptTelemetry()
    assert telemetry.input_tokens is None
    assert telemetry.output_tokens is None
    assert telemetry.cached_input_tokens is None
    assert telemetry.estimated_api_cost_minor is None
    assert telemetry.quota_status == QuotaStatus.UNKNOWN
    assert telemetry.is_token_telemetry_known is False
    assert telemetry.is_cost_known is False
    assert telemetry.is_quota_known is False

    # Duration and tool calls are measurable
    assert telemetry.duration_ms == 0
    assert telemetry.tool_call_count == 0


def test_unknown_telemetry_policy_validates_billing_mode_constraints() -> None:
    policy = UnknownTelemetryPolicy()
    unknown_telemetry = AttemptTelemetry()

    # In allowance_only, unknown cost is allowed
    policy.validate_telemetry(unknown_telemetry, BillingMode.ALLOWANCE_ONLY)

    # In paid_opt_in, unknown cost is disallowed
    with pytest.raises(
        ValueError, match="paid opt-in billing mode requires explicit cost telemetry"
    ):
        policy.validate_telemetry(unknown_telemetry, BillingMode.PAID_OPT_IN)

    # Strict token policy rejects unknown tokens
    strict_policy = UnknownTelemetryPolicy(allow_unknown_tokens=False)
    with pytest.raises(ValueError, match="unknown token telemetry is disallowed by policy"):
        strict_policy.validate_telemetry(unknown_telemetry, BillingMode.ALLOWANCE_ONLY)


def test_budget_pool_enforces_concurrent_reservations_immutably() -> None:
    total = TaskBudget(
        max_duration_seconds=1000,
        max_tool_calls=50,
        max_named_checks=10,
        max_provider_attempts=5,
        max_cost_minor=500,
    )
    pool = BudgetPool(total_budget=total)

    request1 = TaskBudget(
        max_duration_seconds=600,
        max_tool_calls=30,
        max_named_checks=5,
        max_provider_attempts=2,
        max_cost_minor=200,
    )
    assert pool.can_reserve(request1) is True
    pool1 = pool.reserve(request1)
    assert pool1.reserved_duration_seconds == 600
    assert pool.reserved_duration_seconds == 0  # Immutability preserved

    # Second request that exceeds remaining capacity fails
    request2 = TaskBudget(
        max_duration_seconds=500,  # 600 + 500 = 1100 > 1000
        max_tool_calls=10,
        max_named_checks=2,
        max_provider_attempts=1,
    )
    assert pool1.can_reserve(request2) is False
    with pytest.raises(ValueError, match="requested budget exceeds available capacity"):
        pool1.reserve(request2)

    # Release restores capacity
    pool2 = pool1.release(request1)
    assert pool2.reserved_duration_seconds == 0
    assert pool2.can_reserve(request2) is False  # unknown cost cannot reserve against finite cap


def test_envelope_freezes_nested_sequences_and_validates_fallbacks() -> None:
    routes = [(SpecialistPurpose.ROUTINE_IMPLEMENTATION, _sample_binding())]
    fallbacks = [(SpecialistPurpose.ROUTINE_IMPLEMENTATION, [_sample_route(model="luna")])]
    envelope = ExecutionEnvelope(
        run_id=uuid4(),
        profile_id=uuid4(),
        profile_version=1,
        safety_policy_version=1,
        routes=routes,  # type: ignore[arg-type]  # prove runtime freezing of mutable input
        allowed_fallbacks=fallbacks,  # type: ignore[arg-type]  # prove nested freezing
    )
    routes.clear()
    fallbacks[0][1].clear()
    assert envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION).effective.model == "astra"
    assert envelope.fallbacks_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION)[0].model == "luna"


def test_task_dag_allows_descendant_paths_and_rejects_invalid_roots_and_parent_cycles() -> None:
    run_id, primary_id, child_id = uuid4(), uuid4(), uuid4()
    primary = LogicalTaskContract(
        run_id=run_id,
        task_id=primary_id,
        purpose=SpecialistPurpose.PRIMARY,
        route=_sample_binding(),
        budget=TaskBudget(),
        owned_paths=("src",),
    )
    child = LogicalTaskContract(
        run_id=run_id,
        task_id=child_id,
        parent_task_id=primary_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=_sample_binding(),
        budget=TaskBudget(),
        owned_paths=("src/file.py",),
    )
    validate_task_dag((primary, child))
    worker_root = LogicalTaskContract(
        run_id=run_id,
        task_id=uuid4(),
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=_sample_binding(),
        budget=TaskBudget(),
    )
    with pytest.raises(ValueError, match="worker root"):
        validate_task_dag((primary, worker_root))


def test_budget_pool_requires_finite_reservations_and_rejects_unknown_release() -> None:
    pool = BudgetPool(total_budget=TaskBudget(max_input_tokens=10, max_repairs=2))
    assert not pool.can_reserve(TaskBudget(max_input_tokens=None, max_repairs=1))
    reserved = pool.reserve(TaskBudget(max_input_tokens=5, max_repairs=1))
    with pytest.raises(ValueError, match="cannot release an unreserved budget"):
        reserved.release(TaskBudget(max_input_tokens=6, max_repairs=1))


def test_evidence_and_fallback_policy_reject_contradictions() -> None:
    with pytest.raises(ValueError, match="passed must agree with exit_code"):
        CheckResultEvidence(
            command_name="unit",
            exit_code=1,
            passed=True,
            output_digest=_sample_sha256(),
            duration_ms=1,
        )
    assert permits_fallback(FailureReason.POLICY_DENIED) is False
    assert permits_fallback(FailureReason.UNSUPPORTED_CAPABILITY) is False
    assert permits_fallback(FailureReason.OUTAGE) is True


def test_versioned_subscription_record_roundtrip_rejects_unknown_schema() -> None:
    record = LogicalTaskContract(
        run_id=uuid4(),
        task_id=uuid4(),
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=_sample_binding(),
        budget=TaskBudget(),
        typed_acceptance=(AcceptanceCriterion(criterion_id="a", description="bounded"),),
    )
    encoded = encode_subscription_record(record)
    assert decode_subscription_record(encoded) == record
    with pytest.raises(ValueError, match="unsupported subscription record schema version"):
        decode_subscription_record({"schema_version": 2, "record": encoded["record"]})


@pytest.mark.parametrize(
    "record",
    [
        RouteSpec(provider="openai", client="codex", model="astra"),
        ModelMapping(
            requested_model="a", effective_model="b", approved_by="operator", reason="alias"
        ),
        RouteMapping(
            requested=_sample_route(),
            effective=_sample_route(model="luna"),
            approved_by="operator",
            approval_id="a1",
            reason="fallback",
        ),
        RolePreference(purpose=SpecialistPurpose.PLANNING, preferred_route=_sample_route()),
        OperatorProfile(profile_id=uuid4(), version=1, preferences=()),
        ExecutionEnvelope(
            run_id=uuid4(),
            profile_id=uuid4(),
            profile_version=1,
            safety_policy_version=1,
            routes=(),
        ),
        TaskLineage(task_id=uuid4(), lineage_root_task_id=uuid4()),
        AttemptTelemetry(),
        UnknownTelemetryPolicy(),
        TaskBudget(),
        BudgetPool(total_budget=TaskBudget()),
        AcceptanceCriterion(criterion_id="criterion", description="description"),
        LogicalTaskContract(
            run_id=uuid4(),
            task_id=uuid4(),
            purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
            route=_sample_binding(),
            budget=TaskBudget(),
        ),
        AttemptIdentity(run_id=uuid4(), task_id=uuid4(), attempt_id=uuid4()),
        ToolCallBinding(
            attempt_id=uuid4(),
            provider_call_key="call",
            durable_operation_id=uuid4(),
            tool_name=ToolName.REPOSITORY_READ_FILE,
            arguments_digest=_sample_sha256(),
        ),
        CheckResultEvidence(
            command_name="unit",
            exit_code=0,
            passed=True,
            output_digest=_sample_sha256(),
            duration_ms=1,
        ),
        TaskHandoff(
            run_id=uuid4(), task_id=uuid4(), attempt_id=uuid4(), status=HandoffStatus.FAILED
        ),
        _sample_delegate_decision(),
        WaitDecision(
            run_id=uuid4(), task_id=uuid4(), waiting_on_task_ids=(uuid4(),), reason="wait"
        ),
        ScopeRequestDecision(
            run_id=uuid4(), task_id=uuid4(), requested_paths=("src/file.py",), reason="scope"
        ),
        ScopeResponseDecision(run_id=uuid4(), task_id=uuid4(), reason="deny"),
        AcceptDecision(
            run_id=uuid4(),
            task_id=uuid4(),
            candidate_commit=None,
            candidate_tree_digest=_sample_sha256(),
            evidence_receipt_ids=("receipt",),
            rationale="accept",
        ),
        ReassignDecision(
            run_id=uuid4(), task_id=uuid4(), new_route=_sample_route(), reason="retry"
        ),
        ReviewSelection(
            run_id=uuid4(),
            candidate_commit=None,
            candidate_tree_digest=_sample_sha256(),
            review_required=False,
            no_review_reason="small change",
        ),
    ],
)
def test_subscription_record_codec_roundtrips_each_public_record_family(record: object) -> None:
    assert decode_subscription_record(encode_subscription_record(record)) == record


def test_subscription_record_codec_rejects_malformed_envelopes_and_mappings() -> None:
    record = encode_subscription_record(_sample_route())
    with pytest.raises(ValueError, match="unsupported subscription record schema version"):
        decode_subscription_record({"schema_version": True, "record": record["record"]})
    with pytest.raises(ValueError, match="unsupported subscription record schema version"):
        decode_subscription_record({"schema_version": 1, "record": record["record"], "extra": 1})
    with pytest.raises(ValueError, match="subscription payload must decode to a record"):
        decode_subscription_record({"schema_version": 1, "record": {"$tuple": []}})
    with pytest.raises(ValueError, match="invalid subscription mapping encoding"):
        decode_subscription_record({"schema_version": 1, "record": {"$mapping": [["a"]]}})
    with pytest.raises(ValueError, match="duplicate keys"):
        decode_subscription_record(
            {"schema_version": 1, "record": {"$mapping": [["a", 1], ["a", 2]]}}
        )
    malformed = dict(record)
    malformed["record"] = {"$record": "RouteSpec", "fields": [["provider", "openai"]]}
    with pytest.raises(ValueError, match="invalid subscription record field shape"):
        decode_subscription_record(malformed)


def test_subscription_record_codec_rejects_excessive_depth_before_recursion() -> None:
    deeply_nested: object = "leaf"
    for _ in range(65):
        deeply_nested = {"$tuple": [deeply_nested]}
    with pytest.raises(ValueError, match="maximum nesting depth"):
        decode_subscription_record({"schema_version": 1, "record": deeply_nested})


def test_mutation_tools_follow_write_capability_for_every_purpose() -> None:
    mutations = {
        ToolName.REPOSITORY_WRITE_FILE,
        ToolName.REPOSITORY_DELETE_FILE,
        ToolName.REPOSITORY_RENAME_FILE,
    }
    for purpose, capabilities in SPECIALIST_CAPABILITIES.items():
        allowed = SPECIALIST_ALLOWED_TOOLS[purpose]
        if Capability.WRITE in capabilities:
            assert mutations <= allowed, purpose
        else:
            assert mutations.isdisjoint(allowed), purpose


@pytest.mark.parametrize("field", ["currency", "subscription_allowance_charge"])
@pytest.mark.parametrize("value", [123, "", "x" * 4097])
def test_telemetry_optional_text_is_typed_and_bounded(field, value):
    with pytest.raises((ValueError, TypeError)):
        AttemptTelemetry(**{field: value})
