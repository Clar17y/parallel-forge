"""Focused tests for bounded structured redaction."""

from __future__ import annotations

import json

from forge.observability.redaction import RedactionPolicy, Redactor, redact_value


def test_redacts_nested_case_insensitive_keys_and_durable_text_patterns() -> None:
    value = {
        "token": "ghp_secret",
        "headers": {"Authorization": "Bearer abcdefghijkl"},
        "nested": [{"database_url": "postgresql://forge:password@db/forge"}],
        "safe": "visible",
    }

    result = redact_value(value)

    assert result == {
        "token": "[REDACTED]",
        "headers": {"Authorization": "[REDACTED]"},
        "nested": [{"database_url": "[REDACTED]"}],
        "safe": "visible",
    }
    assert value["headers"]["Authorization"] == "Bearer abcdefghijkl"


def test_registered_literals_are_replaced_longest_first_without_global_state() -> None:
    first = Redactor(secrets=("secret", "secret-value"))
    second = Redactor()

    assert first.redact({"message": "secret-value and secret"}) == {
        "message": "[REDACTED] and [REDACTED]"
    }
    assert second.redact({"message": "secret-value"}) == {"message": "secret-value"}


def test_registered_literal_is_also_removed_from_mapping_keys() -> None:
    result = Redactor(secrets=("literal-secret",)).redact({"prefix-literal-secret": "safe"})

    assert "literal-secret" not in str(result)


def test_empty_registered_secret_is_rejected() -> None:
    try:
        Redactor(secrets=("",))
    except ValueError as error:
        assert "secret" in str(error).lower()
    else:  # pragma: no cover - assertion makes the intended failure explicit
        raise AssertionError("empty secrets must be rejected")


def test_string_and_collection_bounds_are_utf8_safe_and_deterministic() -> None:
    policy = RedactionPolicy(
        max_string_bytes=64,
        max_collection_items=2,
        max_depth=8,
        max_nodes=100,
    )

    result = redact_value(
        {"text": "é" * 40, "items": [1, 2, 3]},
        policy=policy,
    )

    assert len(result["text"].encode()) <= 64
    assert "80B" in result["text"]
    assert result["items"][-1]["__forge_truncated__"] == 3


def test_lone_surrogates_are_normalized_to_json_safe_text() -> None:
    malformed = "before" + chr(0xD800) + "after"

    result = redact_value({"message": malformed})

    assert result == {"message": "before�after"}
    assert chr(0xD800) not in result["message"]
    assert json.dumps(result, ensure_ascii=False).encode("utf-8")


def test_depth_nodes_cycles_and_unsupported_values_are_bounded() -> None:
    policy = RedactionPolicy(max_string_bytes=64, max_collection_items=8, max_depth=1, max_nodes=4)
    cyclic: list[object] = []
    cyclic.append(cyclic)

    result = redact_value({"cycle": cyclic, "unsupported": object()}, policy=policy)

    assert result["cycle"]
    assert result["unsupported"] == "[UNSUPPORTED]"
    assert "cycle" not in str(result).lower() or "truncated" in str(result).lower()


def test_collection_marker_does_not_overwrite_a_caller_key() -> None:
    policy = RedactionPolicy(
        max_string_bytes=64,
        max_collection_items=2,
        max_depth=4,
        max_nodes=20,
    )

    result = redact_value(
        {"__forge_truncated__": "caller-value", "second": 2, "discarded": 3},
        policy=policy,
    )

    assert result["__forge_truncated__"] == "caller-value"
    assert 3 in result.values()
    assert len(result) == 2
