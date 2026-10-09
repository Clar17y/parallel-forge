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
    TokenBudgetDefaults,
    decode_subscription_record,
    encode_subscription_record,
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


def test_role_token_defaults_freeze_with_selected_profile():
    from dataclasses import replace

    from forge.domain.subscription_envelope import freeze_profile

    selected = profile()
    configured = replace(
        selected,
        preferences=(
            replace(selected.preferences[0], token_budget=TokenBudgetDefaults(max_input_tokens=0)),
            selected.preferences[1],
        ),
    )
    envelope = freeze_profile(configured, run_id=uuid4(), safety_policy_version=7)
    assert envelope.token_budget_for(SpecialistPurpose.PRIMARY) == TokenBudgetDefaults(
        max_input_tokens=0
    )
    assert envelope.token_budget_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION) is None


def test_token_defaults_validate_and_preserve_legacy_record_shape():
    from dataclasses import replace

    from forge.domain.subscription_envelope import freeze_profile

    selected = profile()
    old_profile = encode_subscription_record(selected)
    old_envelope = encode_subscription_record(
        freeze_profile(selected, run_id=uuid4(), safety_policy_version=7)
    )
    assert encode_subscription_record(decode_subscription_record(old_profile)) == old_profile
    assert encode_subscription_record(decode_subscription_record(old_envelope)) == old_envelope
    for invalid in (-1, True, 1.5, "12"):
        with pytest.raises(ValueError):
            TokenBudgetDefaults(max_input_tokens=invalid)
    configured = replace(selected, preferences=(
        replace(selected.preferences[0], token_budget=TokenBudgetDefaults(max_output_tokens=9)),
        selected.preferences[1],
    ))
    new_profile = encode_subscription_record(configured)
    assert new_profile != old_profile
    assert decode_subscription_record(new_profile) == configured
    new_envelope = freeze_profile(configured, run_id=uuid4(), safety_policy_version=7)
    assert decode_subscription_record(encode_subscription_record(new_envelope)) == new_envelope


def test_primary_budget_default_yields_to_explicit_whole_settings_budget(monkeypatch):
    from dataclasses import replace

    from forge.application.services.subscription_planning import SubscriptionPlanningService
    from forge.domain.subscription import TaskBudget
    from forge.domain.subscription_envelope import freeze_profile
    from forge.settings import Settings

    monkeypatch.delenv("FORGE_SUBSCRIPTION_PRIMARY_BUDGET", raising=False)

    selected = profile()
    configured = replace(selected, preferences=(
        replace(selected.preferences[0], token_budget=TokenBudgetDefaults(
            max_input_tokens=100, max_output_tokens=0
        )),
        selected.preferences[1],
    ))
    envelope = freeze_profile(configured, run_id=uuid4(), safety_policy_version=7)
    base = TaskBudget(max_provider_attempts=64)
    implicit_settings = Settings(_env_file=None)
    assert "subscription_primary_budget" not in implicit_settings.model_fields_set
    assert SubscriptionPlanningService(
        implicit_settings.subscription_primary_budget,
        explicit_primary_budget="subscription_primary_budget" in implicit_settings.model_fields_set,
    )._primary_budget(envelope).max_input_tokens == 100
    explicit_finite = replace(base, max_input_tokens=7, max_output_tokens=8)
    explicit_settings = Settings(_env_file=None, subscription_primary_budget=explicit_finite)
    assert "subscription_primary_budget" in explicit_settings.model_fields_set
    assert SubscriptionPlanningService(
        explicit_settings.subscription_primary_budget,
        explicit_primary_budget="subscription_primary_budget" in explicit_settings.model_fields_set,
    )._primary_budget(envelope) == explicit_finite
    unlimited_settings = Settings(_env_file=None, subscription_primary_budget=base)
    assert SubscriptionPlanningService(
        unlimited_settings.subscription_primary_budget,
        explicit_primary_budget="subscription_primary_budget" in unlimited_settings.model_fields_set,
    )._primary_budget(envelope) == base


def test_selected_review_uses_frozen_default_bounded_by_primary():
    from dataclasses import replace

    from forge.domain.subscription import LogicalTaskContract, ReviewSelection, TaskBudget
    from forge.domain.subscription_envelope import freeze_profile
    from forge.persistence.repositories.subscription_decisions import _selected_review_task

    selected = profile()
    reviewer = RolePreference(
        purpose=SpecialistPurpose.INDEPENDENT_REVIEW,
        preferred_route=selected.preferences[0].preferred_route,
        token_budget=TokenBudgetDefaults(max_input_tokens=80, max_output_tokens=12),
    )
    configured = replace(selected, preferences=selected.preferences + (reviewer,))
    run_id = uuid4()
    envelope = freeze_profile(configured, run_id=run_id, safety_policy_version=7)
    parent = LogicalTaskContract(
        run_id=run_id, task_id=uuid4(), purpose=SpecialistPurpose.PRIMARY,
        route=envelope.route_for(SpecialistPurpose.PRIMARY),
        budget=TaskBudget(max_input_tokens=40, max_output_tokens=20),
    )
    selection = ReviewSelection(
        run_id=run_id, candidate_commit=None, candidate_tree_digest="a" * 64,
        review_required=True, reviewer_route=reviewer.preferred_route,
    )
    child = _selected_review_task(parent, selection, envelope, uuid4())
    assert child is not None
    assert (child.budget.max_input_tokens, child.budget.max_output_tokens) == (40, 12)
