from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from forge.agents.antigravity_configuration import (
    ANTIGRAVITY_ISOLATION_AGENT,
    AntigravityIsolationError,
    AntigravityIsolationHome,
    antigravity_launch_argv,
)


def test_managed_home_is_a_closed_canonical_tree(tmp_path: Path) -> None:
    home = AntigravityIsolationHome(tmp_path, uuid4())
    environment = home.prepare()
    assert environment["HOME"] == str(home.path.resolve())
    assert {path.relative_to(home.path).as_posix() for path in home.expected_files} == {
        "agents/forge-isolation-probe.md",
        "mcp.json",
        "hooks.json",
        "settings.json",
    }
    agent = (home.path / "agents" / f"{ANTIGRAVITY_ISOLATION_AGENT}.md").read_text()
    assert "excludeDefaultComponents: true" in agent
    assert "commandExecutionPolicy: off" in agent
    assert '"useG1Credits":false' in (home.path / "settings.json").read_text()
    home.cleanup()
    assert not home.path.exists()


@pytest.mark.parametrize("drift", ["extra", "changed", "link"])
def test_home_rejects_drift_links_and_unowned_contents(tmp_path: Path, drift: str) -> None:
    home = AntigravityIsolationHome(tmp_path, uuid4())
    home.prepare()
    if drift == "extra":
        (home.path / "plugin.json").write_text("{}")
    elif drift == "changed":
        (home.path / "mcp.json").write_text('{"ambient":true}')
    else:
        try:
            (home.path / "link").symlink_to(home.path / "mcp.json")
        except OSError, NotImplementedError:
            pytest.skip("symlinks unavailable")
    with pytest.raises(AntigravityIsolationError):
        home.validate()


def test_existing_or_relative_managed_home_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AntigravityIsolationError):
        AntigravityIsolationHome("relative", uuid4())
    # The exact target collision is checked before any write.
    ident = uuid4()
    target = tmp_path / f".forge-antigravity-{ident}"
    target.mkdir()
    with pytest.raises(AntigravityIsolationError):
        AntigravityIsolationHome(tmp_path, ident).prepare()


def test_closed_argv_and_environment_cannot_use_ambient_paid_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    home = AntigravityIsolationHome(tmp_path, uuid4())
    environment = home.prepare()
    monkeypatch.setenv("GEMINI_API_KEY", "do-not-inherit")
    monkeypatch.setenv("VERTEXAI_PROJECT", "do-not-inherit")
    assert not {"GEMINI_API_KEY", "VERTEXAI_PROJECT", "GOOGLE_API_KEY"} & set(environment)
    argv = antigravity_launch_argv(str(tmp_path / "agy"), "gemini-3.8-flash", "medium")
    assert argv == (
        str(tmp_path / "agy"),
        "--agent",
        ANTIGRAVITY_ISOLATION_AGENT,
        "--model",
        "gemini-3.8-flash",
        "--effort",
        "medium",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--sandbox",
        "off",
        "--project",
        "none",
        "--slash-commands",
        "off",
    )
    assert "--no-mcp" not in argv
    home.cleanup()
