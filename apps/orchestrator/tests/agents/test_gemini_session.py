"""Pinned ACP metadata must not invent usage or permit late/ambiguous requests."""

from dataclasses import replace

import pytest
from forge.agents.gemini_session import GeminiResponseFailure, GeminiSession
from forge.agents.subscription_protocol import ProtocolError
from forge.application.ports.subscription_gateway import SubscriptionFailure
from test_gemini_gateway import _google_request


def _usage(*, model="gemini-test", source=10, target=4):
    return {
        "_meta": {
            "quota": {
                "token_count": {"input_tokens": source, "output_tokens": target},
                "model_usage": [
                    {
                        "model": model,
                        "token_count": {"input_tokens": source, "output_tokens": target},
                    }
                ],
            }
        }
    }


@pytest.mark.parametrize("source,target", [(0, 0), (10, 4)])
def test_measured_usage_keeps_known_values(source, target):
    session = GeminiSession(_google_request(), None, ".")
    session._usage(_usage(source=source, target=target))
    assert (session.telemetry().input_tokens, session.telemetry().output_tokens) == (source, target)
    assert session.telemetry().subscription_allowance_charge is None


@pytest.mark.parametrize("source", [True, -1, 1.5, "10", None])
def test_invalid_token_measurement_is_rejected(source):
    session = GeminiSession(_google_request(), None, ".")
    with pytest.raises(ProtocolError):
        session._usage(_usage(source=source))
    assert session.telemetry().input_tokens is None


def test_actual_unapproved_model_fails_without_discarding_valid_usage():
    session = GeminiSession(_google_request(), None, ".")
    with pytest.raises(GeminiResponseFailure) as raised:
        session._usage(_usage(model="unapproved-model"))
    assert raised.value.failure is SubscriptionFailure.UNSUPPORTED
    assert (session.telemetry().input_tokens, session.telemetry().output_tokens) == (10, 4)


def test_inconsistent_aggregate_is_not_counted_as_measured_usage():
    usage = _usage()
    usage["_meta"]["quota"]["model_usage"][0]["token_count"]["input_tokens"] = 9
    session = GeminiSession(_google_request(), None, ".")
    with pytest.raises(ProtocolError, match="inconsistent"):
        session._usage(usage)
    assert session.telemetry().input_tokens is None


def test_token_limit_failure_preserves_measured_usage():
    request = _google_request()
    request = replace(
        request, task=replace(request.task, budget=replace(request.budget, max_input_tokens=9))
    )
    session = GeminiSession(request, None, ".")
    with pytest.raises(GeminiResponseFailure) as raised:
        session._usage(_usage())
    assert raised.value.failure is SubscriptionFailure.BUDGET
    assert session.telemetry().input_tokens == 10


@pytest.mark.parametrize("ident", [True, None, "", "x" * 256, "a\nb", 10**255])
async def test_metadata_request_identity_is_bounded(ident):
    session = GeminiSession(_google_request(), None, ".")
    with pytest.raises(ProtocolError):
        await session._mcp({"jsonrpc": "2.0", "id": ident, "method": "ping"})


async def test_closed_session_cannot_serve_late_metadata_or_tool_callbacks():
    session = GeminiSession(_google_request(), None, ".")
    await session.revoke()
    await session.close()
    for method in ("ping", "tools/call"):
        with pytest.raises(ProtocolError):
            await session._mcp({"jsonrpc": "2.0", "id": 1, "method": method})


@pytest.mark.parametrize("ident", [True, "1", 2])
def test_response_requires_exact_requested_integer_identity(ident):
    with pytest.raises(ProtocolError):
        GeminiSession._result({"jsonrpc": "2.0", "id": ident, "result": {}}, 1)


async def test_metadata_chatter_is_bounded_without_spending_tool_budget():
    session = GeminiSession(_google_request(), None, ".")
    for ident in range(64):
        assert await session._mcp({"jsonrpc": "2.0", "id": ident, "method": "ping"})
    with pytest.raises(ProtocolError):
        await session._mcp({"jsonrpc": "2.0", "id": 65, "method": "ping"})
    assert session.telemetry().tool_call_count == 0
