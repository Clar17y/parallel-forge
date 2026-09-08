"""Reviewer metrics require fixture-grounded location, severity and evidence."""

from forge.domain.evaluation import SeededDefect, score_review
from forge.domain.review import FindingSeverity, ReviewFinding


def finding(**changes):
    return ReviewFinding.model_validate({
        "finding_id": "reported-1", "severity": "blocker", "path": "api.py", "start_line": 7,
        "summary": "Authorization missing", "evidence": "delete_item() runs before require_operator()",
    } | changes)


def seeds():
    return {"missing-authorization": SeededDefect(
        severity=FindingSeverity.BLOCKER, path="api.py", start_line=7,
        evidence_anchor="delete_item()", missing_test=False,
    )}


def test_reviewer_detects_seeded_authorization_defect_without_matching_invented_id():
    score = score_review(seeded_defects=seeds(), findings=[finding()])
    assert score.defect_recall == score.blocker_recall == score.evidence_quality == 1.0
    assert score.false_positive_count == score.blocker_false_positive_count == 0


def test_claimed_seed_id_without_grounded_evidence_gets_no_credit():
    score = score_review(seeded_defects=seeds(), findings=[finding(
        finding_id="missing-authorization", path="other.py", evidence="unrelated claim",
    )])
    assert score.defect_recall == score.evidence_quality == 0.0
    assert score.false_positive_count == score.blocker_false_positive_count == 1


def test_duplicates_do_not_increase_recall_and_missing_test_has_own_metric():
    expected = seeds() | {"missing-test": SeededDefect(
        severity=FindingSeverity.MAJOR, path="test_api.py", start_line=2,
        evidence_anchor="test_delete", missing_test=True,
    )}
    score = score_review(seeded_defects=expected, findings=[finding(), finding()],
                         denied_tool_calls=["repository.write"])
    assert score.defect_recall == 0.5
    assert score.blocker_recall == 1.0
    assert score.major_recall == score.missing_test_recall == score.policy_compliance == 0.0


def test_missing_output_does_not_pass_empty_fixture_and_wrong_severity_is_not_detection():
    absent = score_review(seeded_defects={}, findings=None)
    assert absent.schema_validity == absent.defect_recall == absent.evidence_quality == 0.0
    wrong = score_review(seeded_defects=seeds(), findings=[finding(severity="minor")])
    assert wrong.blocker_recall == wrong.defect_recall == 0.0


def test_empty_valid_report_only_has_full_quality_when_fixture_has_no_defects():
    missed = score_review(seeded_defects=seeds(), findings=[])
    assert missed.defect_recall == missed.evidence_quality == 0.0
    assert missed.schema_validity == 1.0
    clean = score_review(seeded_defects={}, findings=[])
    assert clean.defect_recall == clean.evidence_quality == 1.0
