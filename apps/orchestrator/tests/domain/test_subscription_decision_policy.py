from uuid import uuid4

from forge.domain.run import RunState
from forge.domain.subscription import (
    AcceptanceCriterion,
    LogicalTaskContract,
    RouteBinding,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
)
from forge.domain.subscription_decision_policy import (
    PRIMARY_DECISIONS,
    WORKER_DECISIONS,
    decision_allowed,
    is_approved_plan_primary_contract,
)


def _make_contract(
    purpose: SpecialistPurpose = SpecialistPurpose.PRIMARY,
    criteria: tuple[AcceptanceCriterion, ...] = (),
) -> LogicalTaskContract:
    route = RouteSpec(provider="openai", client="codex_app_server", model="gpt-6-astra")
    return LogicalTaskContract(
        run_id=uuid4(),
        task_id=uuid4(),
        purpose=purpose,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(),
        typed_acceptance=criteria,
    )


def test_is_approved_plan_primary_contract_matches():
    contract = _make_contract(
        purpose=SpecialistPurpose.PRIMARY,
        criteria=(
            AcceptanceCriterion(
                criterion_id="approved-plan",
                description="Produce an evidence-bound plan.",
            ),
        ),
    )
    assert is_approved_plan_primary_contract(contract) is True


def test_is_approved_plan_primary_contract_rejects_non_primary():
    contract = _make_contract(
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        criteria=(
            AcceptanceCriterion(
                criterion_id="approved-plan",
                description="Produce an evidence-bound plan.",
            ),
        ),
    )
    assert is_approved_plan_primary_contract(contract) is False


def test_is_approved_plan_primary_contract_rejects_other_criteria():
    contract = _make_contract(
        purpose=SpecialistPurpose.PRIMARY,
        criteria=(
            AcceptanceCriterion(
                criterion_id="approved-implementation",
                description="Implement the plan.",
            ),
        ),
    )
    assert is_approved_plan_primary_contract(contract) is False


def test_decision_kind_sets_partition_and_properties():
    assert "plan" in PRIMARY_DECISIONS
    assert "handoff" in WORKER_DECISIONS
    assert PRIMARY_DECISIONS.isdisjoint(WORKER_DECISIONS)
    assert decision_allowed("plan", SpecialistPurpose.PRIMARY, RunState.PLANNING) is True
    assert decision_allowed("plan", SpecialistPurpose.PRIMARY, RunState.IMPLEMENTING) is False
    assert (
        decision_allowed("handoff", SpecialistPurpose.ROUTINE_IMPLEMENTATION, RunState.IMPLEMENTING)
        is True
    )
    assert decision_allowed("handoff", SpecialistPurpose.PRIMARY, RunState.IMPLEMENTING) is False
