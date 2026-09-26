"""Launch-file ownership and the client configuration boundary, without providers."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from forge.agents.gemini_configuration import GeminiLaunchDirectory
from forge.agents.gemini_gateway import GeminiInstallation


def _prepare(tmp_path):
    home = tmp_path / "dedicated-auth"
    home.mkdir(exist_ok=True)
    files = GeminiLaunchDirectory(str(tmp_path), uuid4())
    files.prepare(home=str(home), model="gemini-3.8-flash", effort="medium", prompt="Trusted")
    return files


def test_two_attempts_share_auth_home_without_sharing_launch_files(tmp_path):
    first, second = _prepare(tmp_path), _prepare(tmp_path)
    home = tmp_path / "dedicated-auth"
    sentinel = home / "client-owned-sentinel"
    sentinel.write_bytes(b"fake-client-owned-state")
    second_files = {p: p.read_bytes() for p in second.path.rglob("*") if p.is_file()}
    first.cleanup()
    assert not first.path.exists()
    assert all(path.read_bytes() == contents for path, contents in second_files.items())
    second.cleanup()
    first.cleanup()  # A completed cleanup is idempotent.
    assert sentinel.read_bytes() == b"fake-client-owned-state"


def test_unknown_client_evidence_preserves_all_files_for_reconciliation(tmp_path):
    files = _prepare(tmp_path)
    extra = files.path / ".gemini" / "retained-result.json"
    extra.write_text("{}")
    before = {p: p.read_bytes() for p in files.path.rglob("*") if p.is_file()}
    with pytest.raises(OSError, match="unexpected evidence"):
        files.cleanup()
    assert all(path.read_bytes() == contents for path, contents in before.items())


def test_replaced_directory_is_not_deleted_even_if_its_path_matches(tmp_path):
    files = _prepare(tmp_path)
    retained = tmp_path / "retained-original"
    files.path.rename(retained)
    files.path.mkdir()
    foreign = files.path / "system.md"
    foreign.write_text("Operator evidence")
    with pytest.raises(OSError, match="identity changed"):
        files.cleanup()
    assert foreign.read_text() == "Operator evidence"
    assert (retained / "system.md").read_text() == "Trusted"


def test_partial_materialization_can_clean_owned_files_without_touching_auth(tmp_path, monkeypatch):
    home = tmp_path / "auth"
    home.mkdir()
    sentinel = home / "client-owned-state"
    sentinel.write_bytes(b"unchanged")
    files = GeminiLaunchDirectory(str(tmp_path), uuid4())
    real_open = os.open

    def reject_settings(path, *args, **kwargs):
        if Path(path).name == "settings.json":
            raise OSError("fake disk failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", reject_settings)
    with pytest.raises(OSError, match="fake disk failure"):
        files.prepare(home=str(home), model="gemini-test", effort=None, prompt="Trusted")
    files.cleanup()
    assert not files.path.exists()
    assert sentinel.read_bytes() == b"unchanged"


@pytest.mark.parametrize("model", ["auto", "auto-gemini-3", "flash", "gemini-test --yolo", ""])
def test_installation_requires_explicit_model_identity(tmp_path, model):
    with pytest.raises(ValueError):
        GeminiInstallation(
            executable=str(Path(__file__).resolve()),
            cwd=str(tmp_path),
            home=str(tmp_path),
            model=model,
            account="test-account",
            executable_digest="c" * 64,
        )


@pytest.mark.parametrize("effort", ["max", "xhigh", "auto", "", False])
def test_unimplemented_effort_is_not_silently_mapped(tmp_path, effort):
    with pytest.raises(ValueError):
        GeminiInstallation(
            executable=str(Path(__file__).resolve()),
            cwd=str(tmp_path),
            home=str(tmp_path),
            model="gemini-test",
            account="test-account",
            executable_digest="c" * 64,
            effort=effort,
        )


@pytest.mark.parametrize(
    "flag",
    [
        "--ignore-env",
        "--extensions=unsafe",
        "--allowed-mcp-server-names=unsafe",
        "--model=auto",
        "--yolo",
    ],
)
def test_installation_cannot_add_flags_that_override_forge_controls(tmp_path, flag):
    with pytest.raises(ValueError):
        GeminiInstallation(
            executable=str(Path(__file__).resolve()),
            cwd=str(tmp_path),
            home=str(tmp_path),
            model="gemini-test",
            account="test-account",
            executable_digest="c" * 64,
            script=(flag, "--acp"),
        )
