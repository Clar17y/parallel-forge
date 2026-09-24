"""The accepted limitation cannot disappear when an Antigravity route is ready."""

from dataclasses import replace

import pytest
from forge.domain.subscription import ReasoningEffort, RouteSpec
from forge.domain.subscription_readiness import (
    ReadinessReason,
    ReadinessWarning,
    SubscriptionRouteReadiness,
)


@pytest.mark.parametrize("reason", [ReadinessReason.READY, ReadinessReason.EVIDENCE_MISSING])
def test_antigravity_worker_snapshot_always_carries_the_structured_warning(reason):
    readiness = SubscriptionRouteReadiness(
        route=RouteSpec(
            provider="google",
            client="antigravity_cli",
            model="gemini-3.8-flash-medium",
            effort=ReasoningEffort.MEDIUM,
        ),
        configured=True,
        admitted=reason is ReadinessReason.READY,
        reason=reason,
    )
    assert readiness.warnings == (ReadinessWarning.APPROVED_TOOLS_UNPROVED,)
    assert readiness.wire()["warnings"] == ["approved_tools_unproved"]
    assert replace(readiness, warnings=()).warnings == readiness.warnings
