"""Brainstorm clients must match the frozen route and its actual quota pool."""

from __future__ import annotations

import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from forge.domain.subscription import AuthMode, BillingMode, ReasoningEffort, RouteSpec
from forge.domain.subscription_installations import (
    AntigravityInstallationSpec,
    ClaudeInstallationSpec,
    CodexInstallationSpec,
    GeminiInstallationSpec,
    SubscriptionInstallationManifest,
    quota_route_for,
)
from forge.domain.subscription_quota import QuotaPolicy, QuotaPoolKey
from forge.settings import Settings
from forge.worker.epic_brainstorm_composition import make_brainstorm_gateway_factory


@pytest.fixture(
    params=[
        (CodexInstallationSpec, "codex_app_server", "openai", "google"),
        (ClaudeInstallationSpec, "claude_code", "anthropic", "openai"),
        (GeminiInstallationSpec, "gemini_cli", "google", "openai"),
        (AntigravityInstallationSpec, "antigravity_cli", "google", "openai"),
    ],
    ids=["codex", "claude", "gemini", "antigravity"],
)
def configured_installation(tmp_path, request):
    installation_type, client, provider, other_provider = request.param
    installation = installation_type(
        client=client,
        model="scripted-brainstorm",
        effort="low",
        account="0" * 64,
        executable=sys.executable,
        executable_digest="0" * 64,
        client_version="1.0.0",
        cwd=str(tmp_path),
        home=str(tmp_path),
        quota={"account": "actual-account", "pool": "actual-pool"},
    )
    route = RouteSpec(
        provider=provider,
        client=installation.client,
        model=installation.model,
        effort=ReasoningEffort.LOW,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    return installation, route, other_provider


def _gateway(settings, route, *, installations=None):
    factory = make_brainstorm_gateway_factory(settings, installations=installations)
    snapshot = SimpleNamespace(route=SimpleNamespace(effective=route))
    gateway = factory(snapshot)
    assert snapshot.route.effective is route
    return gateway


def _manifest_settings(tmp_path, installations, policy=None):
    manifest = SubscriptionInstallationManifest(version=2, installations=installations)
    path = tmp_path / "installations.json"
    path.write_text(manifest.model_dump_json(), encoding="utf-8")
    return Settings(
        subscription_installations_path=path,
        subscription_quota_policy=policy if policy is not None else QuotaPolicy(),
        _env_file=None,
    )


@pytest.mark.parametrize("provider_matches", [False, True])
def test_gateway_factory_preserves_frozen_provider_identity(
    configured_installation, provider_matches
):
    installation, route, other_provider = configured_installation
    settings = Settings(
        subscription_quota_policy=QuotaPolicy(route_pools=(quota_route_for(installation),)),
        _env_file=None,
    )
    frozen_route = route if provider_matches else replace(route, provider=other_provider)
    gateway = _gateway(settings, frozen_route, installations=(installation,))
    assert gateway.installation is (installation if provider_matches else None)


@pytest.mark.parametrize("mapping", ["automatic", "matching", "account", "pool", "both", "generic"])
def test_gateway_factory_uses_manifest_quota_mapping(configured_installation, tmp_path, mapping):
    installation, route, _ = configured_installation
    expected = quota_route_for(installation)
    configured = replace(
        expected,
        account="different-account"
        if mapping in {"account", "both", "generic"}
        else expected.account,
        pool="different-pool" if mapping in {"pool", "both", "generic"} else expected.pool,
        model=None if mapping == "generic" else expected.model,
    )
    policy = QuotaPolicy(route_pools=() if mapping == "automatic" else (configured,))
    settings = _manifest_settings(tmp_path, (installation,), policy)
    admitted = mapping in {"automatic", "matching", "generic"}
    if not admitted:
        # The conflicting exact selector is retained, not silently overwritten.
        assert settings.subscription_quota_policy == policy
    reserved_key = settings.subscription_quota_policy.key_for(route)
    actual_key = QuotaPoolKey(expected.provider, expected.account, expected.pool)
    assert (reserved_key == actual_key) is admitted
    if mapping == "generic":
        # The merged model-specific selector wins over the generic policy entry.
        assert configured in settings.subscription_quota_policy.route_pools
        assert expected in settings.subscription_quota_policy.route_pools
    gateway = _gateway(settings, route)
    assert gateway.installation == (installation if admitted else None)


@pytest.mark.parametrize("mapping", ["matching", "generic", "conflicting", "absent"])
def test_injected_installations_use_the_same_quota_identity(configured_installation, mapping):
    installation, route, _ = configured_installation
    expected = quota_route_for(installation)
    configured = replace(
        expected,
        account="different-account" if mapping == "conflicting" else expected.account,
        model=None if mapping == "generic" else expected.model,
    )
    settings = Settings(
        subscription_quota_policy=QuotaPolicy(
            route_pools=() if mapping == "absent" else (configured,)
        ),
        _env_file=None,
    )
    gateway = _gateway(settings, route, installations=(installation,))
    assert gateway.installation is (installation if mapping in {"matching", "generic"} else None)


@pytest.mark.parametrize("field", ["client", "model", "auth_mode", "billing_mode"])
def test_matching_quota_pool_cannot_mask_a_different_frozen_route(configured_installation, field):
    installation, route, _ = configured_installation
    changed = replace(
        route,
        **{
            field: {
                "client": "different-client",
                "model": "different-model",
                "auth_mode": AuthMode.API_KEY,
                "billing_mode": BillingMode.PAID_OPT_IN,
            }[field]
        },
    )
    # Deliberately map the changed selector to the same pool to isolate route matching.
    configured = replace(
        quota_route_for(installation),
        client=changed.client,
        model=changed.model,
        auth_mode=changed.auth_mode,
        billing_mode=changed.billing_mode,
    )
    settings = Settings(
        subscription_quota_policy=QuotaPolicy(route_pools=(configured,)), _env_file=None
    )
    assert _gateway(settings, changed, installations=(installation,)).installation is None


@pytest.mark.parametrize(
    "effort", [ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH]
)
def test_quota_mapping_preserves_frozen_effort(configured_installation, effort):
    installation, route, _ = configured_installation
    settings = Settings(
        subscription_quota_policy=QuotaPolicy(route_pools=(quota_route_for(installation),)),
        _env_file=None,
    )
    gateway = _gateway(settings, replace(route, effort=effort), installations=(installation,))
    assert gateway.installation is (installation if effort is ReasoningEffort.LOW else None)


def test_conflict_on_another_route_keeps_the_matching_client_usable(
    configured_installation, tmp_path
):
    installation, route, _ = configured_installation
    other = installation.model_copy(update={"model": "another-model"})
    policy = QuotaPolicy(
        route_pools=(
            replace(quota_route_for(other), account="different-account"),
            quota_route_for(installation),
        )
    )
    settings = _manifest_settings(tmp_path, (other, installation), policy)
    assert settings.subscription_quota_policy == policy
    assert _gateway(settings, route).installation == installation
