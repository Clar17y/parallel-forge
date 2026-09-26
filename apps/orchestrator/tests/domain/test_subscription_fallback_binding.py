"""Frozen fallback membership does not waive primary, request or billing boundaries."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    ExecutionEnvelope,
    RouteBinding,
    RouteMapping,
    RouteSpec,
    SpecialistPurpose,
)

PURPOSE = SpecialistPurpose.ROUTINE_IMPLEMENTATION


def _route(model: str) -> RouteSpec:
    return RouteSpec(provider="openai", client="codex", model=model)


def _binding(requested: RouteSpec, effective: RouteSpec) -> RouteBinding:
    return RouteBinding(
        requested=requested,
        effective=effective,
        mapping_applied=RouteMapping(
            requested=requested,
            effective=effective,
            approved_by="operator",
            approval_id="frozen-profile",
            reason="Approved specialist fallback",
        ),
    )


def _envelope(*, fallback: RouteSpec | None = None) -> ExecutionEnvelope:
    primary = _route("primary")
    return ExecutionEnvelope(
        run_id=uuid4(),
        profile_id=uuid4(),
        profile_version=1,
        safety_policy_version=1,
        billing_mode=BillingMode.PAID_OPT_IN,
        routes=(
            (
                SpecialistPurpose.PRIMARY,
                RouteBinding(
                    requested=primary,
                    effective=primary,
                    is_primary=True,
                ),
            ),
            (PURPOSE, _binding(_route("requested"), _route("preferred"))),
        ),
        allowed_fallbacks=((PURPOSE, (fallback or _route("fallback"),)),),
    )


def test_exact_preferred_and_primary_bindings_remain_valid():
    envelope = _envelope()
    assert all(envelope.permits_route(purpose, binding) for purpose, binding in envelope.routes)
    changed = replace(envelope.route_for(SpecialistPurpose.PRIMARY), is_primary=False)
    assert not envelope.permits_route(SpecialistPurpose.PRIMARY, changed)


def test_frozen_specialist_fallback_preserves_original_requested_route():
    envelope = _envelope()
    assert envelope.permits_route(PURPOSE, _binding(_route("requested"), _route("fallback")))
    assert not envelope.permits_route(PURPOSE, _binding(_route("preferred"), _route("fallback")))
    assert not envelope.permits_route(PURPOSE, _binding(_route("requested"), _route("unlisted")))
    assert not envelope.permits_route(
        SpecialistPurpose.PRIMARY, _binding(_route("primary"), _route("fallback"))
    )
    assert not envelope.permits_route(
        SpecialistPurpose.SECURITY, _binding(_route("requested"), _route("fallback"))
    )


@pytest.mark.parametrize(
    "change",
    [
        {"billing_mode": BillingMode.PAID_OPT_IN},
        {"auth_mode": AuthMode.API_KEY},
    ],
)
def test_even_a_listed_fallback_cannot_cross_authentication_or_billing_boundary(change):
    fallback = replace(_route("fallback"), **change)
    envelope = _envelope(fallback=fallback)
    assert fallback in envelope.fallbacks_for(PURPOSE)
    assert not envelope.permits_route(PURPOSE, _binding(_route("requested"), fallback))
