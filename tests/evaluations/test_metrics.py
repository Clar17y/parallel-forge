"""Planner evaluation uses declared exact labels, not incidental prose similarity."""

from forge.domain.evaluation import score_plan
from forge.domain.plan import PlanOutput


def plan(**changes):
    return PlanOutput.model_validate({
        "summary": "Add an authorized endpoint", "assumptions": [],
        "affected_components": ["apps/web"], "steps": ["Implement endpoint"],
        "required_checks": ["test", "typecheck"], "risks": ["authorization"],
        "security_considerations": ["Check operator identity"], "dependency_changes": [],
    } | changes)


def test_planner_score_requires_components_checks_risks_and_no_denied_calls():
    score = score_plan(
        expected_components={"apps/web"}, expected_checks={"test", "typecheck"},
        expected_risks={"authorization"}, actual=plan(), denied_tool_calls=[],
    )
    assert score.component_recall == score.check_recall == score.risk_recall == 1.0
    assert score.policy_compliance == score.schema_validity == score.dependency_disclosure == 1.0


def test_missing_expectations_and_denied_tools_reduce_independent_metrics():
    score = score_plan(
        expected_components={"apps/web", "apps/api"}, expected_checks={"test", "typecheck"},
        expected_risks={"authorization"}, expected_dependencies={"new-package"},
        actual=plan(required_checks=["test"], risks=["deployment"]),
        denied_tool_calls=["repository.write"],
    )
    assert score.component_recall == score.check_recall == 0.5
    assert score.risk_recall == score.policy_compliance == score.dependency_disclosure == 0.0
    assert score.schema_validity == 1.0


def test_invalid_output_cannot_pass_empty_expectations():
    score = score_plan(expected_components=set(), expected_checks=set(), expected_risks=set(),
                       actual=None, denied_tool_calls=[])
    assert score.schema_validity == 0.0
    assert score.component_recall == score.check_recall == score.risk_recall == 0.0
    assert score.dependency_disclosure == 0.0


def test_component_substrings_do_not_satisfy_exact_fixture_labels():
    score = score_plan(expected_components={"apps/web"}, expected_checks={"test"},
                       expected_risks={"authorization"},
                       actual=plan(affected_components=["apps/web-old"], required_checks=["test-other"],
                                   risks=["no authorization risk"]), denied_tool_calls=[])
    assert score.component_recall == score.check_recall == score.risk_recall == 0.0
