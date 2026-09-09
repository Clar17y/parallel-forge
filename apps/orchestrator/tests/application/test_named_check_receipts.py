"""Tests for the bounded pure named-check result and stream receipt codec."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from types import MappingProxyType
from zoneinfo import ZoneInfo

import pytest
from forge.application.adapters.named_check_receipts import (
    NamedCheckReceiptError,
    decode_command_result,
    encode_command_result,
    verify_output_envelope,
)
from forge.application.ports.runner import CommandResult
from forge.domain.policy import RunnerMode, StepKind

_DUMMY_CMD_DIGEST = "a" * 64
_DUMMY_STDOUT_DIGEST = "b" * 64
_DUMMY_STDERR_DIGEST = "c" * 64
_DUMMY_IMAGE_DIGEST = "sha256:" + ("d" * 64)


def _make_command_result(
    *,
    command_name: str = "pytest",
    kind: StepKind = StepKind.TEST,
    command_digest: str = _DUMMY_CMD_DIGEST,
    policy_version: int = 1,
    exit_code: int | None = 0,
    timed_out: bool = False,
    started_at: datetime | None = None,
    duration_ms: int = 120,
    stdout_digest: str = _DUMMY_STDOUT_DIGEST,
    stderr_digest: str = _DUMMY_STDERR_DIGEST,
    runner_mode: RunnerMode = RunnerMode.DOCKER,
    image_digest: str | None = _DUMMY_IMAGE_DIGEST,
    network_enabled: bool = False,
    stdout_original_byte_count: int = 15,
    stderr_original_byte_count: int = 0,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    unsandboxed: bool = False,
) -> CommandResult:
    if started_at is None:
        started_at = datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC)
    return CommandResult(
        command_name=command_name,
        kind=kind,
        command_digest=command_digest,
        policy_version=policy_version,
        exit_code=exit_code,
        timed_out=timed_out,
        started_at=started_at,
        duration_ms=duration_ms,
        stdout_digest=stdout_digest,
        stderr_digest=stderr_digest,
        runner_mode=runner_mode,
        image_digest=image_digest,
        network_enabled=network_enabled,
        stdout_original_byte_count=stdout_original_byte_count,
        stderr_original_byte_count=stderr_original_byte_count,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        unsandboxed=unsandboxed,
    )


def _make_stream_envelope_bytes(
    *,
    stream: str = "stdout",
    text: str = "all tests passed\n",
    captured_byte_count: int | None = None,
    encoding: str = "utf-8-replacement",
    original_byte_count: int = 17,
    truncated: bool = False,
) -> bytes:
    encoded_text = text.encode("utf-8")
    if captured_byte_count is None:
        captured_byte_count = len(encoded_text)
    payload = {
        "captured_byte_count": captured_byte_count,
        "encoding": encoding,
        "original_byte_count": original_byte_count,
        "stream": stream,
        "text": text,
        "truncated": truncated,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


class TestNamedCheckReceiptError:
    """Fixed safe error message without raw values or traceback leakage."""

    def test_fixed_safe_message(self) -> None:
        err = NamedCheckReceiptError()
        assert str(err) == "invalid named check receipt"
        assert issubclass(NamedCheckReceiptError, ValueError)

    def test_no_leakage_on_custom_message(self) -> None:
        err = NamedCheckReceiptError("secret_token_12345")
        assert "secret_token_12345" not in str(err)
        assert str(err) == "invalid named check receipt"


class TestCommandResultCodecGoldenAndRoundtrip:
    """Golden literal bytes, digest verification, and round-trip fidelity."""

    def test_encode_matches_evidence_digest_exactly(self) -> None:
        result = _make_command_result()
        encoded = encode_command_result(result)
        computed_sha = hashlib.sha256(encoded).hexdigest()
        assert computed_sha == result.evidence_digest

    def test_golden_literal_bytes(self) -> None:
        result = _make_command_result(
            command_name="check-fmt",
            kind=StepKind.LINT,
            command_digest="1" * 64,
            policy_version=2,
            exit_code=0,
            timed_out=False,
            started_at=datetime(2026, 9, 7, 8, 30, 0, tzinfo=UTC),
            duration_ms=45,
            stdout_digest="2" * 64,
            stderr_digest="3" * 64,
            runner_mode=RunnerMode.TRUSTED_HOST,
            image_digest=None,
            network_enabled=False,
            stdout_original_byte_count=100,
            stderr_original_byte_count=0,
            stdout_truncated=False,
            stderr_truncated=False,
            unsandboxed=True,
        )
        encoded = encode_command_result(result)
        expected_json = (
            '{"command_digest":"' + "1" * 64 + '",'
            '"command_name":"check-fmt",'
            '"duration_ms":45,'
            '"exit_code":0,'
            '"image_digest":null,'
            '"kind":"lint",'
            '"network_enabled":false,'
            '"policy_version":2,'
            '"runner_mode":"trusted_host",'
            '"started_at":"2026-09-07T08:30:00+00:00",'
            '"stderr_digest":"' + "3" * 64 + '",'
            '"stderr_original_byte_count":0,'
            '"stderr_truncated":false,'
            '"stdout_digest":"' + "2" * 64 + '",'
            '"stdout_original_byte_count":100,'
            '"stdout_truncated":false,'
            '"timed_out":false,'
            '"unsandboxed":true}'
        )
        assert encoded == expected_json.encode("utf-8")
        assert hashlib.sha256(encoded).hexdigest() == result.evidence_digest

        decoded = decode_command_result(encoded)
        assert decoded == result

    def test_timezone_offset_roundtrip(self) -> None:
        # Test UTC (+00:00)
        res_utc = _make_command_result(started_at=datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC))
        enc_utc = encode_command_result(res_utc)
        dec_utc = decode_command_result(enc_utc)
        assert dec_utc.started_at == res_utc.started_at
        assert dec_utc.started_at.isoformat() == "2026-09-07T10:00:00+00:00"

        # Test positive offset (+01:00)
        tz_plus_1 = ZoneInfo("Europe/London")
        dt_plus_1 = datetime(2026, 9, 7, 11, 0, 0, tzinfo=tz_plus_1)
        res_p1 = _make_command_result(started_at=dt_plus_1)
        enc_p1 = encode_command_result(res_p1)
        dec_p1 = decode_command_result(enc_p1)
        assert dec_p1.started_at.utcoffset() == dt_plus_1.utcoffset()
        assert dec_p1.started_at.isoformat() == dt_plus_1.isoformat()

        # Test negative offset (-05:00)
        tz_minus_5 = ZoneInfo("America/New_York")
        dt_m5 = datetime(2026, 9, 7, 6, 0, 0, tzinfo=tz_minus_5)
        res_m5 = _make_command_result(started_at=dt_m5)
        enc_m5 = encode_command_result(res_m5)
        dec_m5 = decode_command_result(enc_m5)
        assert dec_m5.started_at.utcoffset() == dt_m5.utcoffset()
        assert dec_m5.started_at.isoformat() == dt_m5.isoformat()


class TestCommandResultValidationAndRejection:
    """Strict rejection of malformed, unknown, duplicate, noncanonical, and oversized data."""

    def test_rejects_non_bytes(self) -> None:
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result("not bytes")  # type: ignore[arg-type]

    def test_rejects_empty_data(self) -> None:
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(b"")

    def test_rejects_oversized_data(self) -> None:
        oversized = b"{" + b" " * (64 * 1024 + 1) + b"}"
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(oversized)

    def test_rejects_invalid_utf8(self) -> None:
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(b"\xff\xfe\xfd")

    def test_rejects_invalid_json(self) -> None:
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(b"{not json")

    def test_rejects_json_constants_nan_inf(self) -> None:
        res = _make_command_result()
        valid_json = encode_command_result(res).decode("utf-8")
        nan_json = valid_json.replace('"duration_ms":120', '"duration_ms":NaN')
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(nan_json.encode("utf-8"))

        inf_json = valid_json.replace('"duration_ms":120', '"duration_ms":Infinity')
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(inf_json.encode("utf-8"))

    def test_rejects_duplicate_keys(self) -> None:
        res = _make_command_result()
        valid_json = encode_command_result(res).decode("utf-8")
        dup_json = valid_json[:-1] + ',"command_name":"duplicate"}'
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(dup_json.encode("utf-8"))

    def test_rejects_extra_unknown_keys(self) -> None:
        res = _make_command_result()
        valid_json = encode_command_result(res).decode("utf-8")
        extra_json = valid_json[:-1] + ',"extra_field":"unauthorized"}'
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(extra_json.encode("utf-8"))

    def test_rejects_missing_keys(self) -> None:
        res = _make_command_result()
        data = json.loads(encode_command_result(res).decode("utf-8"))
        del data["command_name"]
        raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(raw)

    def test_rejects_schema_and_type_aliases(self) -> None:
        res = _make_command_result()
        data = json.loads(encode_command_result(res).decode("utf-8"))
        data["$schema"] = "https://example.com/schema.json"
        raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(raw)

    def test_rejects_noncanonical_bytes_whitespace(self) -> None:
        res = _make_command_result()
        canonical = encode_command_result(res)
        non_canonical = canonical.replace(b":", b": ", 1)
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(non_canonical)

    def test_rejects_noncanonical_bytes_unsorted_keys(self) -> None:
        res = _make_command_result()
        data = json.loads(encode_command_result(res).decode("utf-8"))
        reversed_data = dict(reversed(tuple(data.items())))
        unsorted = json.dumps(reversed_data, separators=(",", ":"), sort_keys=False).encode("utf-8")
        sorted_bytes = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
        assert unsorted != sorted_bytes
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(unsorted)

    def test_rejects_noncanonical_z_timestamp(self) -> None:
        res = _make_command_result()
        canonical = encode_command_result(res).decode("utf-8")
        z_json = canonical.replace("+00:00", "Z")
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(z_json.encode("utf-8"))

    def test_rejects_invalid_field_types(self) -> None:
        res = _make_command_result()
        data = json.loads(encode_command_result(res).decode("utf-8"))

        bad = copy.deepcopy(data)
        bad["duration_ms"] = True
        raw = json.dumps(bad, separators=(",", ":"), sort_keys=True).encode("utf-8")
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(raw)

        bad = copy.deepcopy(data)
        bad["timed_out"] = 1
        raw = json.dumps(bad, separators=(",", ":"), sort_keys=True).encode("utf-8")
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(raw)

        bad = copy.deepcopy(data)
        bad["kind"] = "not_a_step_kind"
        raw = json.dumps(bad, separators=(",", ":"), sort_keys=True).encode("utf-8")
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(raw)

    def test_encode_revalidates_bypassed_object(self) -> None:
        res = _make_command_result()
        object.__setattr__(res, "duration_ms", -1)
        with pytest.raises(NamedCheckReceiptError):
            encode_command_result(res)

    def test_encode_rejects_non_command_result(self) -> None:
        with pytest.raises(NamedCheckReceiptError):
            encode_command_result("not a command result")  # type: ignore[arg-type]

    def test_encode_rejects_oversized_result(self) -> None:
        res = _make_command_result(command_name="x" * (64 * 1024))
        with pytest.raises(NamedCheckReceiptError):
            encode_command_result(res)

    def test_decode_rejects_naive_datetime(self) -> None:
        res = _make_command_result()
        data = json.loads(encode_command_result(res).decode("utf-8"))
        data["started_at"] = "2026-09-07T10:00:00"
        raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(raw)

    def test_decode_rejects_invalid_digest_format(self) -> None:
        res = _make_command_result()
        data = json.loads(encode_command_result(res).decode("utf-8"))
        data["command_digest"] = "not-a-valid-sha256"
        raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(raw)

    def test_decode_rejects_runner_disclosure_mismatch(self) -> None:
        res = _make_command_result()
        data = json.loads(encode_command_result(res).decode("utf-8"))
        # Docker without image_digest
        data["image_digest"] = None
        raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
        with pytest.raises(NamedCheckReceiptError):
            decode_command_result(raw)


class TestOutputEnvelopeVerification:
    """Stream envelope validation, digest matching, and semantics."""

    def test_valid_stdout_stream_envelope(self) -> None:
        raw_text = "test pass\n"
        env_bytes = _make_stream_envelope_bytes(
            stream="stdout",
            text=raw_text,
            original_byte_count=10,
            truncated=False,
        )
        stdout_digest = hashlib.sha256(env_bytes).hexdigest()
        result = _make_command_result(
            stdout_digest=stdout_digest,
            stdout_original_byte_count=10,
            stdout_truncated=False,
        )

        verified = verify_output_envelope(env_bytes, stream="stdout", result=result)
        assert isinstance(verified, MappingProxyType)
        assert verified["stream"] == "stdout"
        assert verified["text"] == raw_text
        assert verified["captured_byte_count"] == len(raw_text.encode("utf-8"))
        assert verified["original_byte_count"] == 10
        assert verified["encoding"] == "utf-8-replacement"
        assert verified["truncated"] is False

        with pytest.raises(TypeError):
            verified["text"] = "mutated"  # type: ignore[index]

    def test_valid_stderr_stream_envelope(self) -> None:
        raw_text = "warning: deprecated\n"
        env_bytes = _make_stream_envelope_bytes(
            stream="stderr",
            text=raw_text,
            original_byte_count=20,
            truncated=False,
        )
        stderr_digest = hashlib.sha256(env_bytes).hexdigest()
        result = _make_command_result(
            stderr_digest=stderr_digest,
            stderr_original_byte_count=20,
            stderr_truncated=False,
        )

        verified = verify_output_envelope(env_bytes, stream="stderr", result=result)
        assert verified["stream"] == "stderr"
        assert verified["text"] == raw_text
        assert verified["original_byte_count"] == 20

    def test_stream_mismatch_rejected(self) -> None:
        env_bytes = _make_stream_envelope_bytes(stream="stdout")
        digest = hashlib.sha256(env_bytes).hexdigest()
        result = _make_command_result(stdout_digest=digest, stderr_digest=digest)

        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(env_bytes, stream="stderr", result=result)

    def test_digest_mismatch_rejected(self) -> None:
        env_bytes = _make_stream_envelope_bytes(stream="stdout")
        result = _make_command_result(stdout_digest="f" * 64)
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(env_bytes, stream="stdout", result=result)

    def test_original_byte_count_mismatch_rejected(self) -> None:
        env_bytes = _make_stream_envelope_bytes(
            stream="stdout",
            original_byte_count=100,
        )
        digest = hashlib.sha256(env_bytes).hexdigest()
        result = _make_command_result(
            stdout_digest=digest,
            stdout_original_byte_count=50,
        )
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(env_bytes, stream="stdout", result=result)

    def test_captured_byte_count_mismatch_rejected(self) -> None:
        env_bytes = _make_stream_envelope_bytes(
            stream="stdout",
            text="hello",
            captured_byte_count=999,
        )
        digest = hashlib.sha256(env_bytes).hexdigest()
        result = _make_command_result(stdout_digest=digest)
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(env_bytes, stream="stdout", result=result)

    def test_captured_greater_than_original_is_accepted_expansion(self) -> None:
        expanded_text = "[REDACTED_SECRET_VALUE_XYZ]"
        env_bytes = _make_stream_envelope_bytes(
            stream="stdout",
            text=expanded_text,
            captured_byte_count=len(expanded_text.encode("utf-8")),
            original_byte_count=5,
            truncated=False,
        )
        digest = hashlib.sha256(env_bytes).hexdigest()
        result = _make_command_result(
            stdout_digest=digest,
            stdout_original_byte_count=5,
            stdout_truncated=False,
        )
        verified = verify_output_envelope(env_bytes, stream="stdout", result=result)
        assert verified["captured_byte_count"] > verified["original_byte_count"]

    def test_extra_envelope_truncation_accepted_when_process_not_truncated(self) -> None:
        env_bytes = _make_stream_envelope_bytes(
            stream="stdout",
            text="some text",
            original_byte_count=9,
            truncated=True,
        )
        digest = hashlib.sha256(env_bytes).hexdigest()
        result = _make_command_result(
            stdout_digest=digest,
            stdout_original_byte_count=9,
            stdout_truncated=False,
        )
        verified = verify_output_envelope(env_bytes, stream="stdout", result=result)
        assert verified["truncated"] is True

    def test_envelope_not_truncated_rejected_when_process_is_truncated(self) -> None:
        env_bytes = _make_stream_envelope_bytes(
            stream="stdout",
            text="partial text",
            original_byte_count=1000,
            truncated=False,
        )
        digest = hashlib.sha256(env_bytes).hexdigest()
        result = _make_command_result(
            stdout_digest=digest,
            stdout_original_byte_count=1000,
            stdout_truncated=True,
        )
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(env_bytes, stream="stdout", result=result)

    def test_envelope_oversized_data_rejected(self) -> None:
        oversized_len = 6 * 1024 * 1024 + 1025
        oversized = b"{" + b" " * (oversized_len - 2) + b"}"
        digest = hashlib.sha256(oversized).hexdigest()
        result = _make_command_result(stdout_digest=digest)
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(oversized, stream="stdout", result=result)

    def test_envelope_text_cap_1mib(self) -> None:
        oversized_text = "a" * (1024 * 1024 + 1)
        env_bytes = _make_stream_envelope_bytes(
            stream="stdout",
            text=oversized_text,
            original_byte_count=len(oversized_text),
            truncated=True,
        )
        digest = hashlib.sha256(env_bytes).hexdigest()
        result = _make_command_result(
            stdout_digest=digest,
            stdout_original_byte_count=len(oversized_text),
            stdout_truncated=True,
        )
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(env_bytes, stream="stdout", result=result)

    def test_envelope_encoding_must_be_utf8_replacement(self) -> None:
        env_bytes = _make_stream_envelope_bytes(
            stream="stdout",
            encoding="utf-8",
        )
        digest = hashlib.sha256(env_bytes).hexdigest()
        result = _make_command_result(stdout_digest=digest)
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(env_bytes, stream="stdout", result=result)

    def test_envelope_noncanonical_bytes_rejected(self) -> None:
        env_bytes = _make_stream_envelope_bytes(stream="stdout")
        non_canonical = env_bytes.replace(b":", b": ", 1)
        digest = hashlib.sha256(non_canonical).hexdigest()
        result = _make_command_result(stdout_digest=digest)
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(non_canonical, stream="stdout", result=result)

    def test_envelope_duplicate_keys_rejected(self) -> None:
        raw_text = '{"captured_byte_count":5,"captured_byte_count":5,"encoding":"utf-8-replacement","original_byte_count":5,"stream":"stdout","text":"hello","truncated":false}'
        raw_bytes = raw_text.encode("utf-8")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        result = _make_command_result(stdout_digest=digest, stdout_original_byte_count=5)
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(raw_bytes, stream="stdout", result=result)

    def test_envelope_rejects_invalid_stream_argument(self) -> None:
        env_bytes = _make_stream_envelope_bytes(stream="stdout")
        result = _make_command_result(stdout_digest=hashlib.sha256(env_bytes).hexdigest())
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(env_bytes, stream="stdin", result=result)  # type: ignore[arg-type]

    def test_envelope_rejects_non_command_result(self) -> None:
        env_bytes = _make_stream_envelope_bytes(stream="stdout")
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(env_bytes, stream="stdout", result="not_a_result")  # type: ignore[arg-type]

    def test_envelope_rejects_non_bytes_or_empty(self) -> None:
        result = _make_command_result()
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope("string", stream="stdout", result=result)  # type: ignore[arg-type]
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(b"", stream="stdout", result=result)

    def test_envelope_rejects_invalid_utf8(self) -> None:
        bad_utf8 = b"\xff\xfe\xfd"
        result = _make_command_result(stdout_digest=hashlib.sha256(bad_utf8).hexdigest())
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(bad_utf8, stream="stdout", result=result)

    def test_envelope_rejects_extra_keys(self) -> None:
        raw_text = '{"captured_byte_count":5,"encoding":"utf-8-replacement","extra":"bad","original_byte_count":5,"stream":"stdout","text":"hello","truncated":false}'
        raw_bytes = raw_text.encode("utf-8")
        result = _make_command_result(
            stdout_digest=hashlib.sha256(raw_bytes).hexdigest(), stdout_original_byte_count=5
        )
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(raw_bytes, stream="stdout", result=result)

    def test_envelope_rejects_missing_keys(self) -> None:
        raw_text = '{"captured_byte_count":5,"encoding":"utf-8-replacement","stream":"stdout","text":"hello","truncated":false}'
        raw_bytes = raw_text.encode("utf-8")
        result = _make_command_result(stdout_digest=hashlib.sha256(raw_bytes).hexdigest())
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(raw_bytes, stream="stdout", result=result)

    def test_envelope_rejects_nan_constant(self) -> None:
        raw_text = '{"captured_byte_count":NaN,"encoding":"utf-8-replacement","original_byte_count":5,"stream":"stdout","text":"hello","truncated":false}'
        raw_bytes = raw_text.encode("utf-8")
        result = _make_command_result(stdout_digest=hashlib.sha256(raw_bytes).hexdigest())
        with pytest.raises(NamedCheckReceiptError):
            verify_output_envelope(raw_bytes, stream="stdout", result=result)
