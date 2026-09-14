from datetime import UTC, datetime, timedelta

import pytest
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.domain.provider_quota import (
    QuotaExhaustion,
    classify_claude_error,
    classify_codex_error,
)
from test_subscription_protocol import _request

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def test_claude_http_429_is_throttled_without_account_exhaustion() -> None:
    failure, evidence = classify_claude_error(status=429, errors=["rate limit"], now=NOW)
    assert failure == "throttled"
    assert evidence is None


def test_claude_explicit_account_usage_has_sanitized_evidence() -> None:
    failure, evidence = classify_claude_error(
        status=429, errors=["monthly quota exceeded", "secret-token"], now=NOW
    )
    assert failure == "quota"
    assert evidence == QuotaExhaustion(observed_at=NOW, reason="claude_account_usage_exhausted")


def test_explicit_quota_code_takes_precedence_over_generic_rate_limit_wording():
    failure, evidence = classify_claude_error(
        status=429, errors={"code": "QUOTA_EXHAUSTED", "message": "Rate limit reached"}, now=NOW
    )
    assert failure == "quota" and evidence is not None


def test_codex_classifies_usage_rate_and_session_limits_separately() -> None:
    failure, evidence = classify_codex_error(
        {"codexErrorInfo": "usageLimitExceeded", "resetAfter": 3600}, now=NOW
    )
    assert failure == "quota"
    assert evidence is not None and evidence.reset_at == NOW + timedelta(hours=1)
    assert classify_codex_error({"codexErrorInfo": "rateLimitExceeded"}, now=NOW) == (
        "throttled",
        None,
    )
    assert classify_codex_error({"codexErrorInfo": "sessionBudgetExceeded"}, now=NOW) == (
        "budget",
        None,
    )


@pytest.mark.parametrize(
    "code,expected",
    [
        ("serverOverloaded", "outage"),
        ("internalServerError", "outage"),
        ("unauthorized", "authentication"),
        ("cyberPolicy", "policy_denied"),
        ({"httpConnectionFailed": {"httpStatusCode": 429}}, "throttled"),
        ({"responseStreamConnectionFailed": {"httpStatusCode": 401}}, "authentication"),
        ({"responseStreamDisconnected": {"httpStatusCode": 503}}, "outage"),
    ],
)
def test_codex_schema_failure_variants_do_not_block_allowance(code, expected):
    assert classify_codex_error({"codexErrorInfo": code}, now=NOW) == (expected, None)


def test_quota_evidence_requires_aware_future_reset() -> None:
    with pytest.raises(ValueError):
        QuotaExhaustion(observed_at=NOW, reason="safe", reset_at=NOW)
    with pytest.raises(ValueError):
        QuotaExhaustion(observed_at=NOW.replace(tzinfo=None), reason="safe")


def test_invocation_result_keeps_legacy_quota_and_rejects_mismatched_evidence() -> None:
    request = _request()
    legacy = SubscriptionInvocationResult(
        attempt=request.attempt, failure=SubscriptionFailure.QUOTA
    )
    assert legacy.quota_exhaustion is None
    evidence = QuotaExhaustion(observed_at=NOW, reason="safe")
    result = SubscriptionInvocationResult(
        attempt=request.attempt,
        failure=SubscriptionFailure.QUOTA,
        quota_exhaustion=evidence,
    )
    assert result.quota_exhaustion is evidence
    with pytest.raises(ValueError):
        SubscriptionInvocationResult(
            attempt=request.attempt,
            failure=SubscriptionFailure.THROTTLED,
            quota_exhaustion=evidence,
        )


@pytest.mark.parametrize(
    "message",
    [
        "You've hit your weekly limit · resets Sep 13, 2am (Europe/London)",
        "You’ve hit your session limit",
        "You've hit your Sonnet limit · resets 3:45pm",
        "5-hour limit reached - resets in 1h30m",
    ],
)
def test_claude_recognizes_terminal_plan_limits(message):
    failure, evidence = classify_claude_error(status=429, errors=[message], now=NOW)
    assert failure == "quota" and evidence is not None


@pytest.mark.parametrize(
    "status,message,expected",
    [
        (429, "Quota exceeded per minute", "throttled"),
        (None, "Task failed while explaining 'monthly limit' in a README", "protocol"),
        (
            429,
            "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited",
            "throttled",
        ),
        (503, "monthly quota exceeded", "outage"),
        (None, "API Error: Rate limit reached", "throttled"),
        (None, "Not logged in · Please run /login", "authentication"),
        (None, "Invalid API key", "authentication"),
        (None, "Model not found", "unsupported"),
    ],
)
def test_only_terminal_confirmed_exhaustion_blocks_account(status, message, expected):
    assert classify_claude_error(status=status, errors=[message], now=NOW) == (expected, None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("resetAt", 120),
        ("resetAt", float("inf")),
        ("resetAt", float("nan")),
        ("resetAfter", float("inf")),
        ("resetAt", True),
        ("resetAt", "2026-09-13T14:00:00"),
        ("retryAfter", 120),
    ],
)
def test_ambiguous_or_invalid_reset_uses_unknown_cooldown(field, value):
    failure, evidence = classify_codex_error(
        {"codexErrorInfo": "usageLimitExceeded", field: value}, now=NOW
    )
    assert failure == "quota" and evidence is not None and evidence.reset_at is None


def test_explicit_reset_and_relative_reset_are_distinct_from_retry_backoff():
    for fields, expected in [
        ({"resetsAt": (NOW + timedelta(hours=2)).timestamp()}, NOW + timedelta(hours=2)),
        ({"resetAfterSeconds": 120}, NOW + timedelta(seconds=120)),
        (
            {"message": "You've hit your limit · resets in 2h3m"},
            NOW + timedelta(hours=2, minutes=3),
        ),
    ]:
        _, evidence = classify_codex_error(
            {"codexErrorInfo": "usageLimitExceeded", **fields}, now=NOW
        )
        assert evidence is not None and evidence.reset_at == expected


def test_unambiguous_human_reset_uses_zone_but_dst_fold_stays_unknown():
    _, evidence = classify_claude_error(
        status=429,
        errors=["You've hit your weekly limit · resets Sep 13, 2am (Europe/London)"],
        now=NOW,
    )
    assert evidence is not None and evidence.reset_at == datetime(2026, 9, 13, 1, tzinfo=UTC)
    _, ambiguous = classify_claude_error(
        status=429,
        errors=["You've hit your limit · resets Oct 25, 1:30am (Europe/London)"],
        now=NOW,
    )
    assert ambiguous is not None and ambiguous.reset_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("close_raises", [False, True])
async def test_gateways_keep_exhaustion_when_client_stop_is_uncertain(
    monkeypatch, provider, close_raises
):
    from forge.agents.client_process import (
        ClientProcessReceipt,
        ClientProcessResult,
        ClientSettlementUncertain,
    )
    from test_claude_supervised import _anthropic_request
    from test_claude_supervised import _gateway as claude_gateway
    from test_codex_gateway import _gateway as codex_gateway

    evidence = QuotaExhaustion(NOW, "provider_usage_exhausted")
    request = _anthropic_request() if provider == "claude" else _request()
    gateway = claude_gateway("success") if provider == "claude" else codex_gateway("success")

    class Session:
        def pinned_path(self, argument_placeholder):
            return "fake-pinned-catalog"

        async def close(self, **kwargs):
            receipt = ClientProcessResult(
                ClientProcessReceipt("fake-launch", 42, "fake-process", 0),
                None,
                (),
                0,
                "",
                0,
                False,
                False,
                "stop_uncertain",
                False,
            )
            if close_raises:
                raise ClientSettlementUncertain(receipt)
            return receipt

    class Supervisor:
        async def start(self, *args, **kwargs):
            return Session()

    async def exchange(*args):
        return SubscriptionInvocationResult(
            attempt=request.attempt, failure=SubscriptionFailure.QUOTA, quota_exhaustion=evidence
        )

    monkeypatch.setattr(gateway, "_supervisor", Supervisor())
    monkeypatch.setattr(gateway, "_exchange", exchange)
    result = await gateway.execute(request)
    assert result.failure is SubscriptionFailure.UNCERTAIN
    assert result.quota_exhaustion == evidence
