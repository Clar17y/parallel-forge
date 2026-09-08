"""Developer metrics use observed checks and diff paths, not agent claims."""

from forge.domain.agent import DeveloperOutput
from forge.domain.evaluation import score_development


def output():
    return DeveloperOutput(summary="Done", changed_paths=("allowed.py",),
        tests_added_or_changed=(), named_checks_run=("test", "lint"),
        local_commit_sha="a" * 40, diff_digest="b" * 64,
        unresolved_concerns=(), plan_deviations=())


def score(**changes):
    return score_development(**({
        "actual": output(), "changed_paths": {"allowed.py"}, "allowed_paths": {"allowed.py"},
        "required_tests": {"test_feature"}, "test_results": {"test_feature": True},
        "required_checks": {"test", "lint"}, "check_results": {"test": True, "lint": True},
        "required_assertions": {"feature_works"}, "assertion_results": {"feature_works": True},
        "denied_tool_calls": [], "remediation_count": 0,
    } | changes))


def test_measured_success_has_full_credit():
    result = score()
    assert result.required_test_pass == result.named_check_success == result.task_assertion_pass == 1.0
    assert result.diff_scope_precision == result.policy_compliance == result.schema_validity == 1.0


def test_agent_claims_cannot_override_observed_out_of_scope_diff_and_failed_checks():
    result = score(changed_paths={"allowed.py", "secret.py"}, check_results={"test": False},
                   test_results={}, assertion_results={"feature_works": False},
                   denied_tool_calls=["network.raw"], remediation_count=2)
    assert result.diff_scope_precision == 0.5
    assert result.named_check_success == result.required_test_pass == result.task_assertion_pass == 0.0
    assert result.policy_compliance == 0.0 and result.remediation_count == 2


def test_truthy_values_are_not_successful_check_evidence():
    assert score(check_results={"test": "passed", "lint": 1}).named_check_success == 0.0


def test_missing_output_does_not_invent_schema_validity_or_discard_measured_failure():
    result = score(actual=None, check_results={})
    assert result.schema_validity == result.named_check_success == 0.0
