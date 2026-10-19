"""Command output retains useful evidence across capture, redaction and storage."""

import io
import json
import sys
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from forge.application.ports.repository import ProcessResult
from forge.tools import runner
from forge.tools.paths import CanonicalRoot
from forge.tools.process import ProcessRunner, _BoundedStream

from apps.orchestrator.tests.tools.test_docker_runner import _FakeArtifacts


@pytest.mark.parametrize(
    "credential",
    [
        "Bearer " + "sensitiveFragment" * 30,
        "ghp_" + "sensitiveFragment" * 30,
        "password=" + "sensitiveFragment" * 30,
        "postgresql://user:" + "sensitiveFragment" * 30 + "@db/",
        "-----BEGIN PRIVATE KEY-----\n" + "sensitiveFragment\n" * 30
        + "-----END PRIVATE KEY-----",
        "password='" + "sensitiveFragment\n" * 30 + "'",
    ],
    ids=["bearer", "github", "assignment", "url", "pem", "quoted"],
)
async def test_output_artifact_redacts_unregistered_credential_at_capture_cut(
    monkeypatch, tmp_path, credential
):
    monkeypatch.setattr(runner, "_OUTPUT_TEXT_MAX_BYTES", 512)
    content = ("BEGIN\n" + "x" * 300 + "\n" + credential + "\nFAILED: final summary\n").encode()
    result = ProcessRunner(CanonicalRoot(tmp_path), stdout_max_bytes=256).run_argv(
        (sys.executable, "-c", f"import sys; sys.stdout.buffer.write({content!r})"),
        cwd=".", environment={},
    )
    artifacts = _FakeArtifacts()
    await runner.persist_output_artifacts(artifacts, result, secrets={})
    envelope = json.loads(artifacts.values[0])
    assert "sensitiveFragment" not in envelope["text"]
    assert envelope["text"].startswith("BEGIN\n")
    assert envelope["text"].endswith("\nFAILED: final summary\n")
    assert envelope["original_byte_count"] == len(content)
    assert envelope["captured_byte_count"] == len(envelope["text"].encode()) <= 512
    assert envelope["truncated"] is True


async def test_output_artifact_removes_known_secret_suffix_at_capture_cut(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_OUTPUT_TEXT_MAX_BYTES", 256)
    secret = "test-only-sensitive-value-" * 8
    content = ("public-context\n" + "x" * 128 + "credential=" + secret + "\nEND\n").encode()
    result = ProcessRunner(CanonicalRoot(tmp_path), stdout_max_bytes=64).run_argv(
        (sys.executable, "-c", f"import sys; sys.stdout.buffer.write({content!r})"),
        cwd=".",
        environment={},
    )
    assert result.stdout_capture_boundary == 32
    artifacts = _FakeArtifacts()
    await runner.persist_output_artifacts(artifacts, result, secrets={"INPUT": secret})
    envelope = json.loads(artifacts.values[0])
    assert secret[-20:] not in envelope["text"]
    assert envelope["text"].startswith("public-context\n")
    assert envelope["text"].endswith("\nEND\n")
    assert envelope["original_byte_count"] == len(content)
    assert envelope["truncated"] is True


@pytest.mark.parametrize("capture_truncated", [False, True])
async def test_output_artifact_keeps_tail_when_redaction_expands_captured_text(
    monkeypatch, capture_truncated
):
    monkeypatch.setattr(runner, "_OUTPUT_TEXT_MAX_BYTES", 128)
    text = "BEGIN\n" + "x" * 90 + "\nFAILED: final summary\n"
    artifacts = _FakeArtifacts()
    result = ProcessResult(
        return_code=1,
        stdout=text,
        stderr="",
        timed_out=False,
        stdout_original_byte_count=10000 if capture_truncated else len(text.encode()),
        stderr_original_byte_count=0,
        stdout_truncated=capture_truncated,
        stderr_truncated=False,
        stdout_capture_boundary=len(text) // 2 if capture_truncated else None,
    )
    await runner.persist_output_artifacts(artifacts, result, secrets={"INPUT": "x"})
    envelope = json.loads(artifacts.values[0])
    assert envelope["text"].startswith("BEGIN\n")
    assert envelope["text"].endswith("FAILED: final summary\n")
    assert "x" not in envelope["text"]
    assert envelope["captured_byte_count"] == len(envelope["text"].encode()) <= 128
    assert envelope["original_byte_count"] == result.stdout_original_byte_count
    assert envelope["truncated"] is True


async def test_stream_capture_artifact_keeps_summary_and_redacts_both_ends(monkeypatch):
    monkeypatch.setattr(runner, "_OUTPUT_TEXT_MAX_BYTES", 128)
    content = b"BEGIN token=private-value\n" + b"\xff" * 1000 + b"\nsecret=private-value\nFAILED\n"
    capture = _BoundedStream(io.BytesIO(content), 128)
    capture.drain()
    text, boundary = capture.decode()
    result = ProcessResult(
        return_code=1,
        stdout=text,
        stderr="",
        timed_out=False,
        stdout_original_byte_count=capture.original_byte_count,
        stderr_original_byte_count=0,
        stdout_truncated=capture.truncated,
        stderr_truncated=False,
        stdout_capture_boundary=boundary,
    )
    artifacts = _FakeArtifacts()
    await runner.persist_output_artifacts(artifacts, result, secrets={"INPUT": "private-value"})
    envelope = json.loads(artifacts.values[0])
    assert "private-value" not in envelope["text"]
    assert envelope["text"].startswith("BEGIN token=[REDACTED]")
    # The tail's first token has lost its preceding context, including the
    # invalid UTF-8 bytes at the edge; conservatively remove its label too.
    assert envelope["text"].endswith("[REDACTED]\nFAILED\n")
    assert envelope["captured_byte_count"] == len(envelope["text"].encode()) <= 128
    assert envelope["original_byte_count"] == len(content)
    assert envelope["truncated"] is True


@pytest.mark.parametrize("limit", [1, 2, 3, 15, 16, 17, 31, 32, 33])
async def test_output_artifact_redacts_secret_spanning_both_utf8_capture_cuts(monkeypatch, limit):
    monkeypatch.setattr(runner, "_OUTPUT_TEXT_MAX_BYTES", 64)
    secret = "é🔑-private-尾-" * 20
    content = secret.encode()
    capture = _BoundedStream(io.BytesIO(content), limit)
    capture.drain()
    text, boundary = capture.decode()
    result = ProcessResult(
        return_code=0,
        stdout=text,
        stderr="",
        timed_out=False,
        stdout_original_byte_count=len(content),
        stderr_original_byte_count=0,
        stdout_truncated=True,
        stderr_truncated=False,
        stdout_capture_boundary=boundary,
    )
    artifacts = _FakeArtifacts()
    await runner.persist_output_artifacts(artifacts, result, secrets={"INPUT": secret})
    envelope = json.loads(artifacts.values[0])
    safe = envelope["text"].replace("[REDACTED]", "").replace("\n[... output omitted ...]\n", "")
    assert safe == ""
    assert envelope["captured_byte_count"] == len(envelope["text"].encode()) <= 64
    assert envelope["original_byte_count"] == len(content) and envelope["truncated"]
    assert set(envelope) == {
        "captured_byte_count",
        "encoding",
        "original_byte_count",
        "stream",
        "text",
        "truncated",
    }


@pytest.mark.parametrize("boundary", [True, False, -1, 4, 1.5, "1"])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_invalid_capture_boundary_is_rejected_before_artifact_storage(boundary, stream):
    result = ProcessResult(
        return_code=0,
        stdout="abc",
        stderr="abc",
        timed_out=False,
        stdout_original_byte_count=10,
        stderr_original_byte_count=10,
        stdout_truncated=True,
        stderr_truncated=True,
    )
    with pytest.raises(ValueError, match="capture boundary"):
        replace(result, **{f"{stream}_capture_boundary": boundary})
    # Alternate process-port implementations cannot bypass the storage check.
    invalid = SimpleNamespace(**asdict(result))
    setattr(invalid, f"{stream}_capture_boundary", boundary)
    artifacts = _FakeArtifacts()
    with pytest.raises(runner.RunnerExecutionError):
        await runner.persist_output_artifacts(artifacts, invalid, secrets={})
    assert len(artifacts.values) == (0 if stream == "stdout" else 1)


async def test_legacy_prefix_capture_redacts_cut_value_and_untruncated_text_is_exact():
    secret = "private-sensitive-value"
    legacy = SimpleNamespace(
        return_code=0,
        stdout="context " + secret[:9],
        stderr="é😀 exact\n",
        timed_out=False,
        stdout_original_byte_count=100,
        stderr_original_byte_count=len("é😀 exact\n".encode()),
        stdout_truncated=True,
        stderr_truncated=False,
    )
    artifacts = _FakeArtifacts()
    await runner.persist_output_artifacts(artifacts, legacy, secrets={"INPUT": secret})
    stdout, stderr = (json.loads(value) for value in artifacts.values)
    assert stdout["text"] == "context [REDACTED]\n[... output omitted ...]\n"
    assert stdout["original_byte_count"] == 100 and stdout["truncated"]
    assert stderr["text"] == legacy.stderr and not stderr["truncated"]
    plain = ProcessResult(**vars(legacy))
    assert plain.stdout_capture_boundary is None and plain.stderr_capture_boundary is None
    with pytest.raises(ValueError, match="capture boundary"):
        replace(plain, stderr_capture_boundary=0)
