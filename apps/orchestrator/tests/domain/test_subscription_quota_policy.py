"""Quota configuration separates account identity from model/billing approval."""

from dataclasses import replace

import pytest
from forge.domain.subscription import AuthMode, BillingMode, RouteSpec
from forge.domain.subscription_quota import QuotaPolicy, QuotaPoolKey, QuotaRoutePool
from forge.settings import Settings


def test_models_share_default_account_but_modes_do_not():
    route = RouteSpec(provider="openai", client="codex_app_server", model="one")
    policy = QuotaPolicy()
    assert policy.key_for(route) == policy.key_for(replace(route, model="two"))
    assert policy.key_for(route) != policy.key_for(
        replace(route, auth_mode=AuthMode.API_KEY, billing_mode=BillingMode.PAID_OPT_IN)
    )


def test_explicit_accounts_and_specific_pool_override_are_stable():
    policy = QuotaPolicy(
        route_pools=(
            QuotaRoutePool("anthropic", "claude_code", "work", "all"),
            QuotaRoutePool("anthropic", "claude_code", "work", "opus", model="opus"),
            QuotaRoutePool("anthropic", "other-client", "personal", "all"),
        )
    )
    route = RouteSpec(provider="anthropic", client="claude_code", model="opus")
    assert policy.key_for(route) == QuotaPoolKey("anthropic", "work", "opus")
    assert policy.key_for(replace(route, model="other")) == QuotaPoolKey("anthropic", "work", "all")
    assert policy.key_for(replace(route, client="other-client")) == QuotaPoolKey(
        "anthropic", "personal", "all"
    )
    with pytest.raises(ValueError, match="duplicate"):
        QuotaPolicy(route_pools=(policy.route_pools[0], policy.route_pools[0]))


@pytest.mark.parametrize("value", [0, -1, 59, 86401, True, float("inf"), float("nan")])
def test_probe_cooldown_is_finite_bounded_configuration(value):
    with pytest.raises(ValueError):
        QuotaPolicy(unknown_reset_cooldown_seconds=value)


@pytest.mark.parametrize(
    "value", ["Bearer secret", "token=secret", "me@example.org", "../account", "a" * 97]
)
def test_account_labels_exclude_credential_diagnostics(value):
    with pytest.raises(ValueError):
        QuotaPoolKey("openai", value, "weekly")


def test_settings_loads_shared_policy_without_changing_route_approval(monkeypatch):
    monkeypatch.setenv(
        "FORGE_SUBSCRIPTION_QUOTA_POLICY",
        '{"unknown_reset_cooldown_seconds":120,"route_pools":[{"provider":"anthropic","client":"claude_code","account":"work","pool":"weekly"}]}',
    )
    settings = Settings(_env_file=None)
    assert settings.subscription_quota_policy.unknown_reset_cooldown_seconds == 120
    assert settings.subscription_quota_policy.route_pools[0].account == "work"
