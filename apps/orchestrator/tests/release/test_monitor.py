from dataclasses import replace

import pytest
from forge.domain.github import CheckSnapshot, MergeProtection, ReviewSnapshot
from forge.release.monitor import assess_checks


def check(name="ci", conclusion="success", status="completed"):
    return CheckSnapshot(name, status, conclusion, head_sha="a" * 40)


def protection(*names):
    return MergeProtection(True, False, False, "strict", required_check_names=names)


def test_required_context_must_be_present_even_when_other_checks_pass():
    result = assess_checks("a" * 40, (check("lint"),), (), protection("ci"))
    assert result.disposition == "pending"
    assert result.reason == "required_checks_pending"


@pytest.mark.parametrize("checks", [(check(),), (check("status:ci", status="success"),)])
def test_successful_required_check_or_status_is_ready(checks):
    assert assess_checks("a" * 40, checks, (), protection("ci")).disposition == "ready"


@pytest.mark.parametrize(
    "checks",
    [
        (check(), check()),
        (replace(check(), head_sha="b" * 40),),
        (replace(check(), head_sha=None),),
    ],
)
def test_ambiguous_or_other_head_evidence_requires_intervention(checks):
    assert assess_checks("a" * 40, checks, (), protection("ci")).disposition == "intervene"


def test_required_failure_and_review_feedback_request_remediation():
    failed = assess_checks("a" * 40, (check(conclusion="failure"),), (), protection("ci"))
    assert failed.disposition == "remediate"
    review = ReviewSnapshot("reviewer", "CHANGES_REQUESTED", None, requested_changes=True)
    assert (
        assess_checks("a" * 40, (check(),), (review,), protection("ci")).disposition == "remediate"
    )


def test_unsafe_protection_takes_precedence_over_remediation():
    unsafe = replace(protection("ci"), actor_can_bypass=True)
    assert (
        assess_checks("a" * 40, (check(conclusion="failure"),), (), unsafe).disposition
        == "intervene"
    )


def test_no_required_checks_does_not_create_vacuous_merge_readiness():
    assert assess_checks("a" * 40, (), (), protection()).disposition == "intervene"


def test_both_status_and_check_with_same_required_name_must_succeed():
    checks = (check(), check("status:ci", "pending", "pending"))
    assert assess_checks("a" * 40, checks, (), protection("ci")).disposition == "pending"
