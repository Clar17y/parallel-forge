"""Subscription quota notifications with fake peers and controlled time."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from forge.agents.capability_verification import capability_scope
from forge.agents.claude_gateway import _QuotaState
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInterrupted,
)
from forge.domain.tool import ToolName
from test_claude_supervised import (
    _anthropic_request,
    _Broker,
    _gateway,
    _report,
    _UncertainLifecycle,
    _Verifier,
)

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)
RESET = NOW + timedelta(hours=2)


WINDOWS = frozenset({"five_hour", "seven_day"})


def gateway(scenario, *, windows=WINDOWS, **kwargs):
    kwargs.setdefault("report", _report(quota_limit_types=windows))
    client = _gateway("success", **kwargs)
    client._installation = replace(
        client._installation,
        quota_limit_types=windows,
        script=(
            str(Path(__file__).with_name("claude_notification_peer.py")),
            scenario,
            str(int(RESET.timestamp())),
        ),
    )
    client._now = lambda: NOW
    return client


async def test_terminal_api_error_in_official_result_field_confirms_exhaustion():
    result = await gateway("result_only").execute(_anthropic_request())
    assert result.failure is SubscriptionFailure.QUOTA and result.decision is None
    assert result.quota_exhaustion is not None
    assert result.quota_exhaustion.observed_at == NOW
    assert result.quota_exhaustion.reset_at is None
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed


@pytest.mark.parametrize(
    "scenario,windows,known",
    [
        ("quota", WINDOWS, True),
        ("generic_429", WINDOWS, True),
        ("sparse_after_quota", WINDOWS, True),
        ("quota", frozenset(), False),
        ("unrelated", WINDOWS, False),
    ],
)
async def test_verified_rejected_windows_retain_only_applicable_absolute_reset(
    scenario, windows, known
):
    result = await gateway(scenario, windows=windows).execute(_anthropic_request())
    assert result.failure is SubscriptionFailure.QUOTA and result.decision is None
    assert result.quota_exhaustion.observed_at == NOW
    assert result.quota_exhaustion.reset_at == (RESET if known else None)
    assert result.quota_exhaustion.reason == "claude_account_usage_exhausted"
    assert "fixture-secret" not in repr(result)
    assert result.telemetry.input_tokens is None and result.telemetry.output_tokens is None
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed


@pytest.mark.parametrize("scenario", ["allowed", "warning", "overage"])
async def test_usage_or_credit_telemetry_alone_does_not_confirm_exhaustion(scenario):
    result = await gateway(scenario).execute(_anthropic_request())
    assert result.failure is None and result.decision is not None
    assert result.quota_exhaustion is None
    assert result.telemetry.input_tokens is None and result.telemetry.output_tokens is None


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("throttled", SubscriptionFailure.THROTTLED),
        ("authentication", SubscriptionFailure.AUTHENTICATION),
        ("outage", SubscriptionFailure.OUTAGE),
        ("unsupported", SubscriptionFailure.UNSUPPORTED),
    ],
)
async def test_warning_does_not_override_unrelated_failure_classification(scenario, expected):
    result = await gateway(scenario).execute(_anthropic_request())
    assert result.failure is expected and result.quota_exhaustion is None
    assert result.decision is None


@pytest.mark.parametrize("scenario", ["foreign", "missing_identity", "bad_status"])
async def test_notification_cannot_bypass_codec_identity_or_shape(scenario):
    result = await gateway(scenario).execute(_anthropic_request())
    assert result.failure is SubscriptionFailure.PROTOCOL and result.quota_exhaustion is None


@pytest.mark.parametrize(
    "scenario",
    ["eof", "contradiction", "tool_after_quota", "malformed_usage", "quota_then_authentication"],
)
async def test_lost_or_conflicting_completion_keeps_exhaustion_without_new_effect(scenario):
    broker = _Broker()
    result = await gateway(scenario, broker=broker).execute(
        _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is SubscriptionFailure.UNCERTAIN and result.decision is None
    assert result.quota_exhaustion.reset_at == RESET
    assert broker.revoked and broker.calls == []
    assert result.telemetry.tool_call_count == result.telemetry.named_check_count == 0
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed


@pytest.mark.parametrize("cancel", [False, True])
async def test_interruption_and_deadline_retain_observed_rejection(monkeypatch, cancel):
    observed = asyncio.Event()
    original = _QuotaState.notification

    def observe(self, frame, now):
        original(self, frame, now)
        if self.quota is not None:
            observed.set()

    monkeypatch.setattr(_QuotaState, "notification", observe)
    task = asyncio.create_task(
        gateway("hang", duration=5 if cancel else 1).execute(_anthropic_request())
    )
    await asyncio.wait_for(observed.wait(), 3)
    if cancel:
        task.cancel()
        with pytest.raises(SubscriptionInterrupted) as caught:
            await task
        result = caught.value.result
    else:
        result = await task
    assert result.failure is (
        SubscriptionFailure.INTERRUPTED if cancel else SubscriptionFailure.DEADLINE
    )
    assert result.quota_exhaustion.reset_at == RESET and result.decision is None
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed


async def test_partial_tool_and_uncertain_settlement_preserve_evidence():
    broker = _Broker()
    lifecycle = _UncertainLifecycle()
    result = await gateway("partial_tool", broker=broker, lifecycle=lifecycle).execute(
        _anthropic_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is SubscriptionFailure.UNCERTAIN and result.decision is None
    assert result.quota_exhaustion.reset_at == RESET
    assert len(broker.calls) == 1 and broker.revoked
    assert result.telemetry.tool_call_count == 1 and result.telemetry.named_check_count == 0
    assert lifecycle.result is not None and lifecycle.result.stop_confirmed


def test_quota_windows_are_an_exact_verified_installation_binding():
    installation = gateway("quota")._installation
    scope = capability_scope(_anthropic_request())

    def bound(report):
        return _Verifier(report).verify(installation, scope)

    assert bound(_report(quota_limit_types=WINDOWS)).admits(installation, scope)
    assert not bound(_report()).admits(installation, scope)
    assert not bound(_report(quota_limit_types=frozenset({"seven_day_sonnet"}))).admits(
        installation, scope
    )
    for invalid in (
        "five_hour",
        {"five_hour"},
        frozenset({"unknown"}),
        frozenset({"overage"}),
        frozenset({"seven_day_overage_included"}),
    ):
        with pytest.raises(ValueError, match="allowance windows"):
            replace(installation, quota_limit_types=invalid)


async def test_missing_window_verification_never_launches_client():
    result = await gateway("quota", report=_report()).execute(_anthropic_request())
    assert result.failure is SubscriptionFailure.UNAVAILABLE
    assert result.launch_proof is None and result.quota_exhaustion is None


@pytest.mark.parametrize(
    "reset", [None, True, 120, "2099-01-01T00:00:00", "in 2 hours", float("inf")]
)
def test_ambiguous_reset_retains_confirmed_quota_with_unknown_reset(reset):
    state = _QuotaState(WINDOWS)
    state.notification(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "rejected",
                "rateLimitType": "five_hour",
                "resetsAt": reset,
            },
        },
        NOW,
    )
    assert state.quota.observed_at == NOW and state.quota.reset_at is None


def test_later_sparse_duplicate_or_shorter_window_never_clears_or_shortens_block():
    state = _QuotaState(WINDOWS)

    def notify(info, now=NOW):
        state.notification({"type": "rate_limit_event", "rate_limit_info": info}, now)

    info = {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": RESET.timestamp()}
    notify(info)
    notify(info, NOW + timedelta(minutes=2))
    notify({"status": "allowed", "rateLimitType": "five_hour"})
    notify(
        {
            "status": "rejected",
            "rateLimitType": "five_hour",
            "resetsAt": (RESET - timedelta(hours=1)).timestamp(),
        }
    )
    assert state.quota.observed_at == NOW and state.quota.reset_at == RESET
    notify(
        {
            "status": "rejected",
            "rateLimitType": "seven_day",
            "resetsAt": (RESET + timedelta(hours=1)).timestamp(),
        }
    )
    assert state.quota.observed_at == NOW and state.quota.reset_at == RESET + timedelta(hours=1)


def test_repeated_terminal_relative_reset_uses_first_confirmed_observation():
    state = _QuotaState(WINDOWS)
    terminal = {"result": "You've hit your session limit · resets in 2h"}
    assert state.terminal(terminal, NOW) is SubscriptionFailure.QUOTA
    state.terminal(terminal, NOW + timedelta(minutes=2))
    assert state.quota.observed_at == NOW and state.quota.reset_at == RESET


def test_relative_reset_uses_terminal_arrival_without_moving_exhaustion_observation():
    state = _QuotaState(WINDOWS)
    state.notification(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "rejected",
                "rateLimitType": "five_hour",
            },
        },
        NOW,
    )
    terminal = {"result": "You've hit your session limit · resets in 2h"}
    arrival = NOW + timedelta(minutes=10)
    state.terminal(terminal, arrival)
    assert state.quota.observed_at == NOW
    assert state.quota.reset_at == arrival + timedelta(hours=2)
    state.terminal(terminal, arrival + timedelta(minutes=1))
    assert state.quota.reset_at == arrival + timedelta(hours=2)
