"""Pure contract construction for approved implementation tasks."""

from dataclasses import replace
from uuid import UUID

from forge.domain.plan import PlanOutput, ScopedPlanOutput
from forge.domain.subscription import AcceptanceCriterion, LogicalTaskContract


def approved_implementation_contract(
    original: LogicalTaskContract,
    plan: PlanOutput,
    plan_attempt_id: UUID,
    plan_digest: str,
    approval_id: UUID,
) -> LogicalTaskContract:
    """Build the approved implementation task contract from the approved plan output.

    Legacy approvals predate explicit plan scope. Their frozen task scope
    remains authoritative; do not manufacture writable paths from prose.
    """
    paths = plan.owned_paths if isinstance(plan, ScopedPlanOutput) else original.owned_paths
    return replace(
        original,
        owned_paths=paths,
        named_checks=plan.required_checks,
        typed_acceptance=(
            AcceptanceCriterion(
                criterion_id="approved-implementation",
                description=(
                    "Implement the approved plan outcomes within the approved scope "
                    "and provide required validation evidence."
                ),
                required_check_names=plan.required_checks,
            ),
        ),
        untrusted_context_refs=(
            *original.untrusted_context_refs,
            f"approved-plan:{plan_attempt_id}:{plan_digest}:{approval_id}",
        ),
    )


__all__ = [
    "approved_implementation_contract",
]
