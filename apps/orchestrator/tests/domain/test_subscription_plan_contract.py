from uuid import uuid4

import pytest
from forge.domain.plan import PlanOutput, ScopedPlanOutput
from forge.domain.subscription import (
    AcceptanceCriterion,
    LogicalTaskContract,
    RouteBinding,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
)
from forge.domain.subscription_plan_contract import approved_implementation_contract


def _sample_contract(owned_paths=("src",)) -> LogicalTaskContract:
    route = RouteSpec(provider="openai", client="codex_app_server", model="gpt-6-astra")
    return LogicalTaskContract(
        run_id=uuid4(),
        task_id=uuid4(),
        purpose=SpecialistPurpose.PRIMARY,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(),
        owned_paths=owned_paths,
        untrusted_context_refs=("task:initial",),
    )


def test_approved_implementation_contract_scoped_plan():
    original = _sample_contract(owned_paths=("src",))
    plan = ScopedPlanOutput(
        summary="Plan summary",
        assumptions=("assume 1",),
        affected_components=("comp 1",),
        steps=("step 1",),
        risks=("risk 1",),
        security_considerations=(),
        dependency_changes=(),
        required_checks=("lint", "test"),
        owned_paths=("apps/orchestrator", "packages/common"),
    )
    attempt_id = uuid4()
    approval_id = uuid4()
    digest = "a" * 64

    contract = approved_implementation_contract(original, plan, attempt_id, digest, approval_id)

    assert contract.owned_paths == ("apps/orchestrator", "packages/common")
    assert contract.named_checks == ("lint", "test")
    assert contract.typed_acceptance == (
        AcceptanceCriterion(
            criterion_id="approved-implementation",
            description=(
                "Implement the approved plan outcomes within the approved scope "
                "and provide required validation evidence."
            ),
            required_check_names=("lint", "test"),
        ),
    )
    assert contract.untrusted_context_refs == (
        "task:initial",
        f"approved-plan:{attempt_id}:{digest}:{approval_id}",
    )
    # Original contract is unmodified
    assert original.owned_paths == ("src",)


def test_approved_implementation_contract_unscoped_legacy_plan():
    original = _sample_contract(owned_paths=("src", "docs"))
    plan = PlanOutput(
        summary="Legacy summary",
        assumptions=("assume 1",),
        affected_components=("comp 1",),
        steps=("step 1",),
        risks=("risk 1",),
        security_considerations=(),
        dependency_changes=(),
        required_checks=("unit",),
    )
    attempt_id = uuid4()
    approval_id = uuid4()
    digest = "b" * 64

    contract = approved_implementation_contract(original, plan, attempt_id, digest, approval_id)

    # Legacy approvals preserve frozen task scope
    assert contract.owned_paths == ("src", "docs")
    assert contract.named_checks == ("unit",)
    assert contract.typed_acceptance == (
        AcceptanceCriterion(
            criterion_id="approved-implementation",
            description=(
                "Implement the approved plan outcomes within the approved scope "
                "and provide required validation evidence."
            ),
            required_check_names=("unit",),
        ),
    )
    assert contract.untrusted_context_refs == (
        "task:initial",
        f"approved-plan:{attempt_id}:{digest}:{approval_id}",
    )


@pytest.mark.parametrize("count", [32, 33, 100])
def test_approved_implementation_contract_retains_every_bounded_check(count):
    checks = tuple(f"check_{index:03d}" for index in range(count))
    plan = PlanOutput(
        summary="Plan summary",
        assumptions=(),
        affected_components=(),
        steps=("Implement",),
        risks=("risk",),
        security_considerations=(),
        dependency_changes=(),
        required_checks=checks,
    )
    contract = approved_implementation_contract(
        _sample_contract(), plan, uuid4(), "a" * 64, uuid4()
    )
    assert contract.named_checks == checks
    assert tuple(
        check for criterion in contract.typed_acceptance for check in criterion.required_check_names
    ) == checks
    assert all(len(criterion.required_check_names) <= 32 for criterion in contract.typed_acceptance)
    assert len({criterion.criterion_id for criterion in contract.typed_acceptance}) == len(
        contract.typed_acceptance
    )
    assert contract.typed_acceptance[0].criterion_id == "approved-implementation"
