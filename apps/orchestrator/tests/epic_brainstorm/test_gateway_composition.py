"""Frozen brainstorm routes must identify the actual installed provider."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from forge.domain.subscription import AuthMode, BillingMode, ReasoningEffort, RouteSpec
from forge.domain.subscription_installations import (
    AntigravityInstallationSpec,
    ClaudeInstallationSpec,
    CodexInstallationSpec,
    GeminiInstallationSpec,
)
from forge.settings import Settings
from forge.worker.epic_brainstorm_composition import make_brainstorm_gateway_factory


@pytest.mark.parametrize(
    ("installation_type", "client", "provider", "other_provider"),
    [
        (CodexInstallationSpec, "codex_app_server", "openai", "google"),
        (ClaudeInstallationSpec, "claude_code", "anthropic", "openai"),
        (GeminiInstallationSpec, "gemini_cli", "google", "openai"),
        (AntigravityInstallationSpec, "antigravity_cli", "google", "openai"),
    ],
    ids=["codex", "claude", "gemini", "antigravity"],
)
@pytest.mark.parametrize("provider_matches", [False, True])
def test_gateway_factory_preserves_frozen_provider_identity(
    tmp_path, installation_type, client, provider, other_provider, provider_matches
):
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
        quota={"account": "test-account", "pool": "test-pool"},
    )
    route = RouteSpec(
        provider=provider if provider_matches else other_provider,
        client=installation.client,
        model=installation.model,
        effort=ReasoningEffort.LOW,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    factory = make_brainstorm_gateway_factory(Settings(), installations=(installation,))
    gateway = factory(SimpleNamespace(route=SimpleNamespace(effective=route)))
    assert gateway.installation is (installation if provider_matches else None)
