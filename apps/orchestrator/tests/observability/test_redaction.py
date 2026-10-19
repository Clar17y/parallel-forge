"""Focused tests for bounded structured redaction."""

from __future__ import annotations

import json

import pytest
from forge.observability.redaction import RedactionPolicy, Redactor, redact_value


@pytest.mark.parametrize("limit", [64, 65, 128])
def test_head_tail_string_bounding_is_opt_in_utf8_safe_and_redacts_before_cut(limit):
    text = "START secret-value\n" + "é😀" * 200 + "\nsecret-value END"
    redactor = Redactor(
        secrets=("secret-value",),
        policy=RedactionPolicy(max_string_bytes=limit, string_bounding="head_tail"),
    )
    bounded, truncated = redactor.redact_text(text)
    assert truncated
    assert len(bounded.encode()) <= limit
    assert "secret-value" not in bounded
    assert bounded.startswith("START ") and bounded.endswith(" END")
    assert bounded.count("[TRUNCATED ") == 1
    assert "\ufffd" not in bounded
    prefix = Redactor(secrets=("secret-value",), policy=RedactionPolicy(max_string_bytes=limit))
    assert not prefix.redact(text).endswith(" END")
    assert (
        redactor.with_minimum_policy(RedactionPolicy(max_string_bytes=256)).policy.string_bounding
        == "head_tail"
    )


def test_redact_text_reports_post_redaction_truncation_not_original_length():
    redactor = Redactor(secrets=("x",), policy=RedactionPolicy(max_string_bytes=64))
    assert redactor.redact_text("x" * 20)[1] is True
    assert redactor.redact_text("visible") == ("visible", False)


@pytest.mark.parametrize(
    ("head", "tail", "expected_head", "expected_tail"),
    [
        ("BEGIN private-", "END", "BEGIN [REDACTED]", "[REDACTED]"),
        ("BEGIN", "ve-value END", "BEGIN", "[REDACTED] END"),
        ("BEGIN private-", "ve-value END", "BEGIN [REDACTED]", "[REDACTED] END"),
        ("private-", "sensitive-value", "[REDACTED]", "[REDACTED]"),
    ],
)
def test_capture_cuts_redact_known_fragments_before_joining_with_omission_marker(
    head, tail, expected_head, expected_tail
):
    redactor = Redactor(secrets=("private-sensitive-value",))
    safe, bounded = redactor.redact_text(
        head + tail, capture_truncated=True, capture_boundary=len(head)
    )
    assert safe == expected_head + "\n[... output omitted ...]\n" + expected_tail
    assert not bounded


def test_capture_edge_redaction_cannot_split_an_overlapping_complete_secret():
    head, tail = "START ababab", "bababa END"
    safe, _ = Redactor(secrets=("ababa",)).redact_text(
        head + tail, capture_truncated=True, capture_boundary=len(head)
    )
    assert safe == "START [REDACTED]\n[... output omitted ...]\n[REDACTED] END"


@pytest.mark.parametrize(
    "credential",
    [
        "Bearer " + "Z" * 30,
        "Basic\n" + "Z" * 30,
        "ghp_" + "Z" * 30,
        "github_pat_" + "Z" * 30,
        "password\n=\n" + "Z" * 30,
        "access-token: Bearer\n" + "Z" * 30,
        "postgresql://user:" + "Z" * 30 + "@db/",
        "password='" + "Z" * 15 + "\n" + "Z" * 15 + "'",
        "-----BEGIN RSA PRIVATE KEY-----\n" + "Z" * 15 + "\n" + "Z" * 15
        + "\n-----END RSA PRIVATE KEY-----",
    ],
    ids=["bearer", "basic", "github", "pat", "assignment", "auth-value", "url", "quoted", "pem"],
)
def test_pattern_credentials_are_redacted_at_every_capture_cut(credential):
    redactor = Redactor()
    for cut in range(1, len(credential)):
        head, tail = "BEGIN\n" + credential[:cut], "\npublic context\nFINAL SUMMARY\n"
        safe, _ = redactor.redact_text(
            head + tail, capture_truncated=True, capture_boundary=len(head)
        )
        assert "Z" not in safe, ("head", cut, safe)
        assert safe.startswith("BEGIN\n") and safe.endswith("\nFINAL SUMMARY\n")
        head, tail = "BEGIN\n", credential[cut:] + "\nFINAL SUMMARY\n"
        safe, _ = redactor.redact_text(
            head + tail, capture_truncated=True, capture_boundary=len(head)
        )
        assert "Z" not in safe, ("tail", cut, safe)
        assert safe.startswith("BEGIN\n") and safe.endswith("\nFINAL SUMMARY\n")


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


def test_with_minimum_policy_keeps_secrets_and_only_raises_bounds() -> None:
    from forge.observability.redaction import RedactionPolicy, Redactor

    base = Redactor(secrets=["s3cr3t-token-value"])
    raised = base.with_minimum_policy(RedactionPolicy(max_string_bytes=64 * 1024))
    assert raised.policy.max_string_bytes == 64 * 1024
    assert raised.policy.max_collection_items == base.policy.max_collection_items
    assert "s3cr3t-token-value" not in str(raised.redact({"text": "x s3cr3t-token-value y"}))
    long_text = "a" * 10_000
    assert raised.redact(long_text) == long_text
    larger = Redactor(policy=RedactionPolicy(max_string_bytes=128 * 1024))
    assert larger.with_minimum_policy(RedactionPolicy(max_string_bytes=64 * 1024)).policy == (
        larger.policy
    )


def test_tool_service_keeps_its_result_bound_with_the_shared_worker_redactor() -> None:
    """The worker passed its 4 KiB observability redactor, truncating read_file results."""
    from forge.application.services.tools import ControlledToolService
    from forge.observability.redaction import Redactor

    service = ControlledToolService(lambda: None, redactor=Redactor(secrets=["k3y-value-123"]))  # type: ignore[arg-type,return-value]
    assert service._redactor.policy.max_string_bytes >= 64 * 1024
    assert "k3y-value-123" not in str(service._redactor.redact("k3y-value-123"))
