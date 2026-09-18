"""The pinned client version is operator configuration, not a source constant.

Official clients ship near-daily. Requiring a source edit per release made the
pin drift instead of move, so the exact version an installation must report
belongs in the operator manifest beside the executable digest it identifies.
The safety property is unchanged: a client whose handshake reports a different
version than its installation declares is still refused.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from forge.agents.claude_gateway import ClaudeInstallation, claude_init_matches
from forge.agents.codex_gateway import CodexInstallation
from forge.domain.capability_evidence import capability_identity
from forge.domain.subscription import SPECIALIST_ALLOWED_TOOLS, SpecialistPurpose
from forge.domain.subscription_installations import (
    ClaudeInstallationSpec,
    SubscriptionInstallationManifest,
)
from forge.domain.tool import ToolName

NEWER = "2.1.268"
OLDER = "2.1.263"


def _installation(tmp_path: Path, version: str) -> ClaudeInstallation:
    executable = tmp_path / "claude.cmd"
    executable.write_text("echo claude\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return ClaudeInstallation(
        executable=str(executable),
        cwd=str(tmp_path),
        model="claude-opus-5",
        effort="medium",
        client_home=str(home),
        account="test-account",
        executable_digest=hashlib.sha256(b"claude").hexdigest(),
        client_version=version,
    )


def test_an_installation_declares_the_exact_version_it_must_report(tmp_path: Path) -> None:
    installation = _installation(tmp_path, NEWER)

    assert installation.client_version == NEWER


@pytest.mark.parametrize("version", ("", "2.1", "2.1.263-beta", "latest", "2.1.263 ", "x.y.z"))
def test_an_installation_refuses_a_version_that_is_not_an_exact_release(
    tmp_path: Path, version: str
) -> None:
    with pytest.raises(ValueError):
        _installation(tmp_path, version)


def test_the_handshake_accepts_only_the_version_its_installation_declares() -> None:
    tools = frozenset({ToolName.REPOSITORY_READ_FILE})
    init = {
        "session_id": "session-1",
        "claude_code_version": NEWER,
        "model": "claude-opus-5",
        "permissionMode": "dontAsk",
        "tools": ["mcp__forge__repository_read_file", "StructuredOutput"],
        "slash_commands": [],
        "skills": [],
        "plugins": [],
        "mcp_servers": [{"name": "forge", "status": "connected"}],
    }

    assert claude_init_matches(
        init,
        model="claude-opus-5",
        tools=tools,
        session_id="session-1",
        client_version=NEWER,
    )
    # A client reporting a different build than its installation declares is
    # still refused, so configuration cannot admit an unverified binary.
    assert not claude_init_matches(
        init,
        model="claude-opus-5",
        tools=tools,
        session_id="session-1",
        client_version=OLDER,
    )


def test_a_new_version_needs_its_own_evidence_identity(tmp_path: Path) -> None:
    from forge.agents.claude_verification import required_claude_verification_scopes

    home = tmp_path / "client-home"
    home.mkdir()
    scope = required_claude_verification_scopes()[0].evidence_scope()
    common = {
        "scope": scope,
        "executable_digest": "a" * 64,
        "client_home": str(home),
        "account": "test-account",
    }

    older = capability_identity(client_version=OLDER, **common)
    newer = capability_identity(client_version=NEWER, **common)

    # Upgrading a client cannot silently inherit the prior build's proof.
    assert older != newer


def test_the_manifest_carries_the_version_beside_the_digest() -> None:
    manifest = SubscriptionInstallationManifest.model_validate_json(
        json.dumps(
            {
                "version": 1,
                "installations": [
                    {
                        "client": "claude_code",
                        "executable": "C:/clients/claude-2.1.268/claude.cmd",
                        "cwd": "C:/work/isolated",
                        "home": "C:/clients/claude-home",
                        "model": "claude-opus-5",
                        "effort": "medium",
                        "account": "personal",
                        "executable_digest": "b" * 64,
                        "client_version": NEWER,
                        "quota": {"account": "personal", "pool": "weekly"},
                    }
                ],
            }
        )
    )

    (spec,) = manifest.installations
    assert isinstance(spec, ClaudeInstallationSpec)
    assert spec.client_version == NEWER


def test_codex_installations_declare_their_version_the_same_way(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_text("echo codex\n", encoding="utf-8")
    home = tmp_path / "codex-home"
    home.mkdir()

    installation = CodexInstallation(
        executable=str(executable),
        cwd=str(tmp_path),
        client_home=str(home),
        model="gpt-6-astra",
        effort="low",
        account="c" * 64,
        executable_digest=hashlib.sha256(b"codex").hexdigest(),
        client_version="0.154.0",
    )

    assert installation.client_version == "0.154.0"
    assert SpecialistPurpose.INDEPENDENT_REVIEW in SPECIALIST_ALLOWED_TOOLS
