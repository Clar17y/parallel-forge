"""Capability evidence must belong to the exact configured official-client home."""

from dataclasses import replace

import pytest
from forge.agents.capability_verification import capability_scope
from forge.agents.gemini_gateway import GeminiCapabilityReport
from forge.application.ports.subscription_gateway import SubscriptionFailure
from test_gemini_gateway import _Broker, _gateway, _google_request, _Lifecycle


@pytest.mark.parametrize("home", [None, "different", "relative"])
async def test_unbound_capability_report_rejects_before_launch(tmp_path, home):
    if home == "different":
        other = tmp_path / "other-account-home"
        other.mkdir()
        home = str(other)
    elif home == "relative":
        home = "account-home"
    report = GeminiCapabilityReport(
        installed_version="0.59.0",
        subscription_auth=True,
        model="gemini-test",
        effort="medium",
        tools_disabled=True,
        billing_never=True,
        isolated_config=True,
        acp_mcp_supported=True,
        client_home=home,
    )
    broker, lifecycle = _Broker(), _Lifecycle()
    result = await _gateway(
        tmp_path, "no_tools", broker=broker, lifecycle=lifecycle, report=report
    ).execute(_google_request())
    assert result.failure is SubscriptionFailure.UNAVAILABLE
    assert result.launch_proof is result.quota_exhaustion is result.decision is None
    assert broker.revoked and not broker.calls and not lifecycle.intents
    assert not list(tmp_path.glob(".forge-gemini-*"))


def test_home_is_canonical_and_not_exposed_in_capability_representations(tmp_path):
    gateway = _gateway(tmp_path, "no_tools")
    installation = gateway._installation
    noncanonical = replace(installation, home=str(tmp_path / "account-home/../account-home"))
    scope = capability_scope(_google_request())
    report = gateway._verifier.verify(noncanonical, scope)
    assert noncanonical.home == installation.home and report.admits(noncanonical, scope)
    assert installation.home not in repr(installation)
    assert installation.home not in repr(report)


@pytest.mark.parametrize(
    "missing",
    [
        {"installed_version": "other"},
        {"subscription_auth": False},
        {"billing_never": False},
        {"tools_disabled": False},
        {"isolated_config": False},
        {"acp_mcp_supported": False},
        {"model": "gemini-other"},
        {"effort": "high"},
    ],
)
def test_home_binding_cannot_substitute_for_existing_capability_evidence(tmp_path, missing):
    gateway = _gateway(tmp_path, "no_tools")
    installation = gateway._installation
    scope = capability_scope(_google_request())
    report = gateway._verifier.verify(installation, scope)
    assert report.admits(installation, scope)
    assert not replace(report, **missing).admits(installation, scope)
