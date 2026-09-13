"""Versioned provider evidence is bounded and preserves historical operation replay."""

from types import SimpleNamespace

import pytest
from forge.application.adapters.named_check import _outcome, _output_preview
from test_named_check_receipts import _make_command_result


@pytest.mark.parametrize("version", [1, 2, 3])
def test_only_new_receipts_expose_verified_duration_and_output(version):
    result = _make_command_result(duration_ms=125)
    receipt = {
        "receipt_version": version,
        "caller_cancelled": False,
        "tool_call_id": "tool",
        "candidate_tree_digest_before": "a" * 64,
        "candidate_tree_digest_after": "a" * 64,
    }
    outputs = _output_preview(
        "stdout", {"text": "token=[REDACTED]\nassertion failed", "truncated": False}
    )
    outputs.update(_output_preview("stderr", {"text": "", "truncated": False}))
    value = _outcome(SimpleNamespace(), receipt, result, outputs=outputs).payload
    expected = {
        "caller_cancelled",
        "command_result_digest",
        "exit_code",
        "receipt_digest",
        "stderr_digest",
        "stdout_digest",
        "timed_out",
        "tool_call_id",
    }
    if version >= 2:
        expected |= {"candidate_tree_digest_before", "candidate_tree_digest_after"}
    if version == 3:
        expected |= {"command_duration_ms", *outputs}
        assert value["command_duration_ms"] == 125
        assert value["stdout_text"] == "token=[REDACTED]\nassertion failed"
    assert set(value) == expected


@pytest.mark.parametrize(
    "text,truncated",
    [("", False), ("a" * 4096, False), ("a" * 4097, False), ("€" * 2000, False), ("short", True)],
)
def test_output_preview_bounds_utf8_and_discloses_truncation(text, truncated):
    result = _output_preview("stdout", {"text": text, "truncated": truncated})
    assert len(result["stdout_text"].encode()) <= 4096
    assert text.startswith(result["stdout_text"])
    assert result["stdout_preview_truncated"] is (truncated or len(text.encode()) > 4096)
