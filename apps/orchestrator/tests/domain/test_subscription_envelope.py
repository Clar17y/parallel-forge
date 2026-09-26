"""Run routing freezes operator choices without resolving or substituting models."""

from uuid import uuid4

import pytest
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    OperatorProfile,
    ReasoningEffort,
    RolePreference,
    RouteSpec,
    SpecialistPurpose,
)


def profile():
    primary = RouteSpec(
        provider="openai",
        client="codex",
        model="astra",
        effort=ReasoningEffort.LOW,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    from dataclasses import replace

    worker = replace(primary, provider="google", client="agy", model="gemini-3.8-flash")
    fallback = replace(primary, model="luna", effort=ReasoningEffort.MEDIUM)
    return OperatorProfile(
        profile_id=uuid4(),
        version=2,
        preferences=(
            RolePreference(purpose=SpecialistPurpose.PRIMARY, preferred_route=primary),
            RolePreference(
                purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
                preferred_route=worker,
                fallback_routes=(fallback,),
            ),
        ),
    )


def test_freeze_preserves_exact_primary_worker_and_fallback_choices():
    from forge.domain.subscription_envelope import freeze_profile

    selected = profile()
    run_id = uuid4()
    envelope = freeze_profile(selected, run_id=run_id, safety_policy_version=7)
    assert (
        envelope.run_id,
        envelope.profile_id,
        envelope.profile_version,
        envelope.safety_policy_version,
    ) == (run_id, selected.profile_id, 2, 7)
    for preference in selected.preferences:
        binding = envelope.route_for(preference.purpose)
        assert binding.requested == binding.effective == preference.preferred_route
        assert binding.is_primary == (preference.purpose is SpecialistPurpose.PRIMARY)
        assert envelope.fallbacks_for(preference.purpose) == preference.fallback_routes


def test_freeze_requires_primary_preference():
    from dataclasses import replace

    from forge.domain.subscription_envelope import freeze_profile

    selected = profile()
    with pytest.raises(ValueError, match="primary"):
        freeze_profile(
            replace(selected, preferences=selected.preferences[1:]),
            run_id=uuid4(),
            safety_policy_version=1,
        )
