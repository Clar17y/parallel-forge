from __future__ import annotations

import pytest
from forge.agents.antigravity_capability_probe import (
    AntigravityCapabilityProbe,
    AntigravityProbeError,
    AntigravityProbeInstallation,
    parse_zero_turn_frames,
)


@pytest.fixture
def installation() -> AntigravityProbeInstallation:
    return AntigravityProbeInstallation(
        executable="C:/clients/agy.exe",
        executable_digest="a" * 64,
        client_version="1.2.7",
        home="C:/managed/antigravity",
        model="gemini-3.8-flash",
        effort="medium",
        account="b" * 64,
    )


def _frames(**changes: object) -> list[dict[str, object]]:
    init: dict[str, object] = {
        "type": "initialized",
        "client_version": "1.2.7",
        "executable_digest": "a" * 64,
        "agent": "forge-isolation-probe",
        "account_kind": "google_subscription",
        "account_identity_digest": "b" * 64,
        "model": "gemini-3.8-flash",
        "effort": "medium",
        "useG1Credits": False,
        "components": [],
    }
    init.update(changes)
    return [init, {"type": "stopped", "confirmed": True}]


def test_1_2_7_zero_turn_fixture_is_the_only_clean_offline_candidate(installation) -> None:
    observation = parse_zero_turn_frames(_frames(), installation)
    assert observation.client_version == "1.2.7" and observation.components == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"client_version": "1.2.8"},
        {"executable_digest": "c" * 64},
        {"components": ["shell"]},
        {"useG1Credits": True},
        {"useG1Credits": None},
        {"account_kind": "api_key"},
        {"model": "fallback"},
        {"effort": "high"},
        {"unexpected": False},
    ],
)
def test_any_identity_credit_surface_or_route_drift_fails_closed(installation, changes) -> None:
    with pytest.raises(AntigravityProbeError):
        parse_zero_turn_frames(_frames(**changes), installation)


@pytest.mark.parametrize(
    "bad",
    [
        [{"type": "turn", "model": "gemini-3.8-flash"}],
        [{"type": "initialized"}],
        _frames() + [{"type": "stopped", "confirmed": True}],
        [{**_frames()[0]}, {"type": "stopped", "confirmed": False}],
    ],
)
def test_turn_unknown_and_uncertain_stop_frames_are_rejected(installation, bad) -> None:
    with pytest.raises(AntigravityProbeError):
        parse_zero_turn_frames(bad, installation)


async def test_no_live_launch_exists_even_with_authorization(installation) -> None:
    probe = AntigravityCapabilityProbe(installation)
    with pytest.raises(AntigravityProbeError, match="provider_contact_not_authorized"):
        await probe.observe()
    with pytest.raises(AntigravityProbeError, match="forge_callback_unproved"):
        await probe.observe(authorize_provider_contact=True)
