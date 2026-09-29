"""Structural decision legality shared by provider and durable application paths.

This rule does not establish target, candidate, usage, or approval authority.
Those remain the responsibility of the decision application.
"""

from forge.domain.plan import PlanOutput
from forge.domain.run import RunState
from forge.domain.subscription import (
    AcceptDecision,
    BoundReassignDecision,
    BoundScopeResponseDecision,
    DelegateDecision,
    ForwardFeedbackDecision,
    ReassignDecision,
    ReviewSelection,
    ScopeRequestDecision,
    ScopeResponseDecision,
    SpecialistPurpose,
    TaskHandoff,
    WaitDecision,
)

PRIMARY_DECISIONS = frozenset(
    {
        "plan",
        "delegate",
        "wait",
        "scope_response",
        "accept",
        "reassign",
        "review_selection",
        "forward_feedback",
    }
)
WORKER_DECISIONS = frozenset({"handoff", "scope_request"})


def decision_allowed(kind: str, purpose: SpecialistPurpose, phase: RunState | None) -> bool:
    """Return whether this role may issue this decision in the observed phase."""
    if purpose is SpecialistPurpose.PRIMARY:
        if kind not in PRIMARY_DECISIONS:
            return False
        return (kind == "plan") if phase is RunState.PLANNING else kind != "plan"
    if kind not in WORKER_DECISIONS:
        return False
    return phase is not RunState.PLANNING


def decision_kind(decision: object) -> str | None:
    """Map typed decisions, including legacy bound variants, to protocol kinds."""
    for kind, cls in (
        ("plan", PlanOutput),
        ("delegate", DelegateDecision),
        ("wait", WaitDecision),
        ("handoff", TaskHandoff),
        ("scope_request", ScopeRequestDecision),
        ("scope_response", (BoundScopeResponseDecision, ScopeResponseDecision)),
        ("accept", AcceptDecision),
        ("reassign", (BoundReassignDecision, ReassignDecision)),
        ("review_selection", ReviewSelection),
        ("forward_feedback", ForwardFeedbackDecision),
    ):
        if isinstance(decision, cls):
            return kind
    return None
