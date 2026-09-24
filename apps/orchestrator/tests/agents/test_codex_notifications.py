"""Official notification order, with fake clients and controlled observation time."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from forge.agents.capability_verification import capability_scope
from forge.agents.codex_gateway import _TerminalError
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInterrupted,
)
from forge.domain.tool import ToolName
from test_codex_gateway import _Broker, _gateway, _report, _Verifier
from test_subscription_protocol import _request

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)
RESET = NOW + timedelta(hours=2)


def gateway(scenario, *, pool="codex", broker=None, duration=5):
    result = _gateway("success", quota_limit_id=pool, broker=broker, duration=duration)
    result._installation = replace(
        result._installation,
        script=(
            str(Path(__file__).with_name("codex_notification_peer.py")),
            scenario,
            "gpt-5.6-luna",
            "medium",
            str(int(RESET.timestamp())),
        ),
    )
    result._now = lambda: NOW
    return result


@pytest.mark.parametrize(
    "scenario,pool,known",
    [
        ("quota", "codex", True),
        ("before_ack", "codex", True),
        ("quota", None, False),
        ("unrelated_pool", "codex", False),
    ],
)
async def test_terminal_usage_uses_only_explicit_matching_pool_reset(scenario, pool, known):
    result = await gateway(scenario, pool=pool).execute(_request())
    assert result.failure is SubscriptionFailure.QUOTA and result.decision is None
    assert result.quota_exhaustion is not None
    assert result.quota_exhaustion.observed_at == NOW
    assert result.quota_exhaustion.reset_at == (RESET if known else None)
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    assert result.telemetry.input_tokens == 13


@pytest.mark.parametrize("scenario", ["retry", "global_success"])
async def test_sparse_quota_and_retry_notice_cannot_confirm_exhaustion(scenario):
    result = await gateway(scenario).execute(_request())
    assert result.failure is None and result.decision is not None
    assert result.quota_exhaustion is None


@pytest.mark.parametrize("scenario", ["startup_advisory", "turn_advisory"])
async def test_client_status_notifications_do_not_abort_the_attempt(scenario):
    broker = _Broker()
    result = await gateway(scenario, broker=broker).execute(_request())
    assert result.failure is None and result.decision is not None
    assert result.quota_exhaustion is None and result.telemetry.input_tokens == 13
    assert result.launch_proof.stop_confirmed and broker.revoked and broker.calls == []


@pytest.mark.parametrize("scenario", ["late_thread_start", "late_thread_start_stream"])
async def test_thread_start_notification_may_follow_its_response(scenario):
    result = await gateway(scenario).execute(_request())
    assert result.failure is None and result.decision is not None
    assert result.launch_proof.stop_confirmed


@pytest.mark.parametrize("scenario", ["warning_before_ack", "warning_in_stream"])
async def test_warning_for_the_bound_thread_is_advisory(scenario):
    result = await gateway(scenario).execute(_request())
    assert result.failure is None and result.decision is not None
    assert result.launch_proof.stop_confirmed


@pytest.mark.parametrize(
    "scenario,failure",
    [
        ("rateLimitExceeded", SubscriptionFailure.THROTTLED),
        ("unauthorized", SubscriptionFailure.AUTHENTICATION),
        ("serverOverloaded", SubscriptionFailure.OUTAGE),
        ("sessionBudgetExceeded", SubscriptionFailure.BUDGET),
    ],
)
async def test_non_quota_terminal_error_keeps_its_classification(scenario, failure):
    result = await gateway(scenario).execute(_request())
    assert result.failure is failure and result.quota_exhaustion is None


@pytest.mark.parametrize(
    "scenario",
    [
        "foreign_error",
        "foreign_thread",
        "error_request",
        "missing_retry",
        "global_request",
        "malformed_global",
        "wrong_ack",
        "advisory_request",
        "malformed_advisory",
        "changed_auth",
        "foreign_late_thread_start",
        "late_thread_start_request",
        "foreign_warning",
        "warning_request",
    ],
)
async def test_notifications_cannot_bypass_identity_and_frame_validation(scenario):
    result = await gateway(scenario).execute(_request())
    assert result.failure is SubscriptionFailure.PROTOCOL and result.quota_exhaustion is None
    assert result.decision is None


@pytest.mark.parametrize("scenario", ["eof", "contradiction", "tool_after_error"])
async def test_lost_or_conflicting_completion_retains_exhaustion_and_no_new_effect(scenario):
    broker = _Broker()
    result = await gateway(scenario, broker=broker).execute(
        _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is SubscriptionFailure.UNCERTAIN and result.decision is None
    assert result.quota_exhaustion is not None and result.quota_exhaustion.reset_at == RESET
    assert broker.revoked and broker.calls == []
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed


@pytest.mark.parametrize("cancel", [False, True])
async def test_deadline_or_cancel_after_terminal_error_retains_evidence(monkeypatch, cancel):
    observed = asyncio.Event()
    original = _TerminalError.notification

    def observe(self, params, now):
        original(self, params, now)
        observed.set()

    monkeypatch.setattr(_TerminalError, "notification", observe)
    client = gateway("hang", duration=5 if cancel else 1)
    task = asyncio.create_task(client.execute(_request()))
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
    assert result.quota_exhaustion is not None and result.quota_exhaustion.reset_at == RESET
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed


def test_duplicate_error_does_not_move_observation_or_relative_reset():
    signal = _TerminalError()
    error = {
        "message": "Usage limit reached. Resets in 2 minutes.",
        "codexErrorInfo": "usageLimitExceeded",
    }
    signal.observe(error, NOW)
    signal.observe(error, NOW + timedelta(seconds=45))
    assert signal.quota.observed_at == NOW
    assert signal.quota.reset_at == NOW + timedelta(minutes=2)


def test_sparse_updates_never_shorten_already_observed_terminal_block():
    signal = _TerminalError("codex")

    def update(window):
        signal.account_notification(
            {
                "method": "account/rateLimits/updated",
                "params": {"rateLimits": {"limitId": "codex", "primary": window}},
            },
            NOW,
        )

    update({"usedPercent": 100, "resetsAt": int(RESET.timestamp())})
    assert signal.quota is None
    signal.observe({"codexErrorInfo": "usageLimitExceeded"}, NOW)
    update(None)
    update({"usedPercent": 0, "resetsAt": int((RESET + timedelta(hours=1)).timestamp())})
    update({"usedPercent": 100, "resetsAt": int((RESET - timedelta(hours=1)).timestamp())})
    assert signal.quota.reset_at == RESET
    update({"usedPercent": 100, "resetsAt": int((RESET + timedelta(hours=1)).timestamp())})
    assert signal.quota.reset_at == RESET + timedelta(hours=1)


def test_quota_window_mapping_is_part_of_exact_capability_admission():
    installation = gateway("quota")._installation
    scope = capability_scope(_request())
    other = _Verifier(_report(quota_limit_id="another-pool")).verify(installation, scope)
    codex = _Verifier(_report(quota_limit_id="codex")).verify(installation, scope)
    assert not other.admits(installation, scope)
    assert codex.admits(installation, scope)
    for invalid in ("", "account@example.com", "untrusted/provider", 123):
        with pytest.raises(ValueError, match="opaque"):
            replace(installation, quota_limit_id=invalid)


async def test_provider_interruption_keeps_terminal_quota_evidence():
    result = await gateway("interrupted").execute(_request())
    assert result.failure is SubscriptionFailure.INTERRUPTED and result.decision is None
    assert result.quota_exhaustion is not None and result.quota_exhaustion.reset_at == RESET


async def test_partial_controlled_tool_receipt_is_retained_before_exhaustion():
    broker = _Broker()
    result = await gateway("partial_tool", broker=broker).execute(
        _request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )
    assert result.failure is SubscriptionFailure.QUOTA and result.decision is None
    assert [call.call_key for call in broker.calls] == ["read-once"] and broker.revoked
    assert result.telemetry.tool_call_count == 1 and result.telemetry.named_check_count == 0


@pytest.mark.parametrize(
    "window",
    [
        {"usedPercent": 100, "resetsAt": None},
        {"usedPercent": True, "resetsAt": int(RESET.timestamp())},
        {"usedPercent": 50, "resetsAt": int(RESET.timestamp())},
        {"usedPercent": 100, "resetsAt": int(NOW.timestamp())},
        {"usedPercent": 100, "resetsAt": "2099-01-01T00:00:00"},
    ],
)
def test_window_does_not_invent_an_unambiguous_exhausted_pool_reset(window):
    signal = _TerminalError("codex")
    signal.account_notification(
        {
            "method": "account/rateLimits/updated",
            "params": {"rateLimits": {"limitId": "codex", "primary": window}},
        },
        NOW,
    )
    signal.observe({"codexErrorInfo": "usageLimitExceeded"}, NOW)
    assert signal.quota is not None and signal.quota.reset_at is None


def test_multiple_exhausted_windows_use_latest_reset_and_new_usage_clears_only_pending_snapshot():
    signal = _TerminalError("codex")

    def update(**windows):
        signal.account_notification(
            {
                "method": "account/rateLimits/updated",
                "params": {"rateLimits": {"limitId": "codex", **windows}},
            },
            NOW,
        )

    later = RESET + timedelta(hours=1)
    update(
        primary={"usedPercent": 100, "resetsAt": int(RESET.timestamp())},
        secondary={"usedPercent": 100, "resetsAt": int(later.timestamp())},
    )
    update(primary={"usedPercent": 0})
    signal.observe({"codexErrorInfo": "usageLimitExceeded"}, NOW)
    assert signal.quota.reset_at == later
