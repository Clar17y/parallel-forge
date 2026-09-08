"""Pure decisions over one exact-head GitHub observation.

The caller persists observations and applies duration/remediation budgets. Remote
text remains evidence for the developer; it never selects controller actions.
"""

from dataclasses import dataclass
from typing import Literal

from forge.domain.github import CheckSnapshot, MergeProtection, ReviewSnapshot


@dataclass(frozen=True, slots=True)
class CheckAssessment:
    disposition: Literal["ready", "pending", "remediate", "intervene"]
    reason: str


def required_check_results(
    checks: tuple[CheckSnapshot, ...], protection: MergeProtection
) -> dict[str, str]:
    """Preserve GitHub's check/status namespace in the frozen required results."""
    names = {key for name in protection.required_check_names for key in (name, f"status:{name}")}
    return {check.name: check.conclusion or "pending" for check in checks if check.name in names}


def assess_checks(
    head_sha: str,
    checks: tuple[CheckSnapshot, ...],
    reviews: tuple[ReviewSnapshot, ...],
    protection: MergeProtection,
) -> CheckAssessment:
    if not protection.safe_for_managed_merge or not protection.required_check_names:
        return CheckAssessment("intervene", "unsafe_merge_protection")
    observed: dict[str, CheckSnapshot] = {}
    for check in checks:
        if not check.name or check.name in observed or check.head_sha != head_sha:
            return CheckAssessment("intervene", "ambiguous_check_evidence")
        observed[check.name] = check
    if any(review.blocks_merge for review in reviews):
        return CheckAssessment("remediate", "blocking_remote_review")
    pending = False
    for name in protection.required_check_names:
        # GitHub requires both if a check run and legacy status share a context.
        matches = [observed[key] for key in (name, f"status:{name}") if key in observed]
        if not matches:
            pending = True
        for check in matches:
            if check.status in {"queued", "in_progress", "pending", "waiting", "requested"}:
                pending = True
            elif check.status in {"completed", "success", "failure", "error"}:
                if check.conclusion != "success":
                    return CheckAssessment("remediate", "required_check_failed")
            else:
                return CheckAssessment("intervene", "unknown_check_status")
    if pending:
        return CheckAssessment("pending", "required_checks_pending")
    return CheckAssessment("ready", "required_checks_passed")
