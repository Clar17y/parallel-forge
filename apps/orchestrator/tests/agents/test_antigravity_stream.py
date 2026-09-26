"""Synthetic documented streams, never live subscription evidence."""

from copy import deepcopy

import pytest
from forge.agents.antigravity_stream import (
    AntigravityStreamError,
    parse_antigravity_stream,
)
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof

MODEL = "gemini-3.8-flash-medium"
OUTPUT = {"probe": "forge-conformance"}
SCHEMA = {
    "type": "object",
    "properties": {"probe": {"type": "string", "const": "forge-conformance"}},
    "required": ["probe"],
    "additionalProperties": False,
}


def frames():
    return [
        {
            "event": "init",
            "conversation_id": "conversation-1",
            "init": {
                "cwd": "private-path-must-not-be-retained",
                "model": MODEL,
                "tools": ["run_command", "write_to_file"],
                "permission_mode": "request-review",
                "json_schema": deepcopy(SCHEMA),
            },
        },
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "conversation-1",
                "step_index": 0,
                "state": "DONE",
                "step_type": "user_input",
            },
        },
        {
            "event": "result",
            "result": {
                "conversation_id": "conversation-1",
                "status": "SUCCESS",
                "response": '{"probe":"forge-conformance"}',
                "structured_output": deepcopy(OUTPUT),
                "json_schema": deepcopy(SCHEMA),
                "num_turns": 1,
                "duration_seconds": 1.5,
                "usage": {
                    "input_tokens": 15,
                    "output_tokens": 5,
                    "thinking_tokens": 2,
                    "cache_read_tokens": 100,
                    "total_tokens": 20,
                },
            },
        },
    ]


def proof(**changes):
    values = {
        "launch_id": "launch-1",
        "pid": 42,
        "process_identity": "process-1",
        "outcome": "exited",
        "return_code": 0,
        "stop_confirmed": True,
        "stdout_bytes": 1024,
        "stderr_bytes": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    values.update(changes)
    return SubscriptionLaunchTerminalProof(**values)


def parse(value=None, terminal=None):
    return parse_antigravity_stream(
        frames() if value is None else value,
        expected_model=MODEL,
        expected_output=OUTPUT,
        output_schema=SCHEMA,
        terminal=proof() if terminal is None else terminal,
        max_tokens=1000,
        max_duration_seconds=30,
    )


def test_real_stream_shape_retains_bounded_usage_without_claiming_capability():
    observed = parse()
    assert observed.total_tokens == 20
    assert observed.cache_read_tokens == 100  # cached input is not part of input_tokens
    assert observed.warnings == ("approved_tools_unproved",)
    assert observed.publishable is False
    assert observed.quota_exhausted is False
    assert "private-path" not in repr(observed)


@pytest.mark.parametrize("field", ["structured_output", "json_schema", "usage"])
def test_response_text_cannot_replace_missing_structured_evidence(field):
    value = frames()
    del value[-1]["result"][field]
    with pytest.raises(AntigravityStreamError):
        parse(value)


@pytest.mark.parametrize("value", [True, -1, 1.5, "20", 1001, float("nan")])
def test_usage_is_strict_bounded_integer_data(value):
    events = frames()
    events[-1]["result"]["usage"]["total_tokens"] = value
    with pytest.raises(AntigravityStreamError):
        parse(events)


@pytest.mark.parametrize("change", ["conversation", "model", "schema", "output", "turns"])
def test_foreign_identity_or_structured_result_is_rejected(change):
    value = frames()
    if change == "conversation":
        value[1]["step_update"]["conversation_id"] = "foreign"
    elif change == "model":
        value[0]["init"]["model"] = "fallback"
    elif change == "schema":
        value[-1]["result"]["json_schema"] = {}
    elif change == "output":
        value[-1]["result"]["structured_output"] = {"probe": "wrong"}
    else:
        value[-1]["result"]["num_turns"] = 2
    with pytest.raises(AntigravityStreamError):
        parse(value)


@pytest.mark.parametrize(
    "change", ["duplicate_init", "duplicate_result", "early_result", "extra", "missing"]
)
def test_incomplete_duplicate_or_unordered_stream_fails_closed(change):
    value = frames()
    if change == "duplicate_init":
        value.insert(1, value[0])
    elif change == "duplicate_result":
        value.append(value[-1])
    elif change == "early_result":
        value.reverse()
    elif change == "extra":
        value.append({"event": "unknown"})
    else:
        value.pop()
    with pytest.raises(AntigravityStreamError):
        parse(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"stop_confirmed": False},
        {"outcome": "cancelled"},
        {"outcome": "timeout"},
        {"stdout_truncated": True},
        {"stderr_truncated": True},
        {"return_code": 1},
    ],
)
def test_provider_success_cannot_substitute_for_supervisor_settlement(changes):
    with pytest.raises(AntigravityStreamError):
        parse(terminal=proof(**changes))


def test_generic_429_is_throttling_without_exhaustion_or_reset_evidence():
    value = frames()
    result = value[-1]["result"]
    result["status"] = "ERROR"
    result["error"] = "HTTP 429: too many requests; private-account"
    observed = parse(value)
    assert observed.failure == "throttled"
    assert observed.quota_exhausted is False
    assert "private-account" not in repr(observed)


def test_frame_count_is_bounded_even_for_an_unending_source():
    def source():
        yield frames()[0]
        for index in range(5000):
            value = frames()[1]
            value["step_update"]["step_index"] = index
            yield value

    with pytest.raises(AntigravityStreamError):
        parse(source())


@pytest.mark.parametrize("field", ["event", "status", "state"])
def test_unhashable_protocol_tags_have_a_sanitized_failure(field):
    value = frames()
    if field == "event":
        value[0][field] = {"private": "value"}
    elif field == "status":
        value[-1]["result"][field] = ["private"]
    else:
        value[1]["step_update"][field] = {"private": "value"}
    with pytest.raises(AntigravityStreamError, match="^antigravity_stream_unproved$"):
        parse(value)


def test_intermediate_usage_is_bounded_even_if_the_result_claims_lower_usage():
    value = frames()
    value[1]["step_update"]["usage"] = {**value[-1]["result"]["usage"], "cache_read_tokens": 1001}
    with pytest.raises(AntigravityStreamError):
        parse(value)
