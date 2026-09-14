"""Shared invocation binding for asynchronous capability evidence verifiers."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from forge.application.ports.subscription_gateway import SubscriptionInvocationRequest
from forge.domain.capability_evidence import (
    CapabilityEvidenceScope,
    validate_capability_installation_identity,
)


def capability_scope(request: SubscriptionInvocationRequest) -> CapabilityEvidenceScope:
    if type(request) is not SubscriptionInvocationRequest:
        raise TypeError("subscription request is required")
    return CapabilityEvidenceScope(
        route=request.task.route.effective,
        role=request.task.purpose,
        tool_surface=tuple(request.authorization.permitted_tools),
    )


async def capability_report[Report](
    candidate: Report | Awaitable[Report], expected_type: type[Report]
) -> Report:
    """Await a database-backed verifier while retaining simple deterministic fakes."""

    if isinstance(candidate, Awaitable):
        candidate = await candidate
    if type(candidate) is not expected_type:
        raise TypeError("capability verifier returned an invalid report")
    return candidate


def validate_installation_identity(account: Any, executable_digest: Any) -> None:
    """Reuse the closed evidence identity validators without accepting raw account data."""

    validate_capability_installation_identity(account, executable_digest)


__all__ = ["capability_report", "capability_scope", "validate_installation_identity"]
