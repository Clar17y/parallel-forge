"""Bounded offline conformance for the pinned official Codex app-server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest
from forge.agents.codex_conformance import (
    CodexOfficialConformanceHarness,
    _LoopbackResponses,
    _response_sequence,
    _user_agent_matches_client_version,
    required_codex_live_scopes,
)
from forge.agents.codex_gateway import CodexInstallation, codex_account_identity
from forge.domain.subscription import SpecialistPurpose


def test_required_live_checks_keep_each_openai_role_and_use_case_separate():
    scopes = required_codex_live_scopes()

    assert [scope.name for scope in scopes] == [
        "astra-primary",
        "astra-direct-repair",
        "sol-planning-adversarial",
        "sol-high-risk-correctness",
        "sol-security",
    ]
    assert [(scope.model, scope.effort) for scope in scopes] == [
        ("gpt-6-astra", "low"),
        ("gpt-6-astra", "low"),
        ("gpt-5.6-sol", "low"),
        ("gpt-5.6-sol", "high"),
        ("gpt-5.6-sol", "high"),
    ]
    assert [scope.role for scope in scopes] == [
        SpecialistPurpose.PRIMARY,
        SpecialistPurpose.PRIMARY,
        SpecialistPurpose.PLANNING,
        SpecialistPurpose.VERIFICATION,
        SpecialistPurpose.SECURITY,
    ]
    assert len({scope.identity_key for scope in scopes}) == len(scopes)


def test_offline_sequence_exercises_native_hosted_custom_and_mcp_paths(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    executable = Path(sys.executable).resolve(strict=True)
    scope = required_codex_live_scopes()[0]
    installation = CodexInstallation(
        executable=str(executable),
        cwd=str(tmp_path),
        model=scope.model,
        effort=scope.effort,
        client_home=str(home),
        account=codex_account_identity("codex@example.invalid"),
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )

    first = _response_sequence(installation, tmp_path / "native-canary")[0].decode("utf-8")
    events = [
        json.loads(line.removeprefix("data: "))
        for line in first.splitlines()
        if line.startswith("data: ")
    ]
    items = [event["item"] for event in events if event["type"] == "response.output_item.done"]

    assert {item["type"] for item in items} >= {
        "function_call",
        "custom_tool_call",
        "local_shell_call",
        "web_search_call",
        "image_generation_call",
    }
    assert any(item.get("name") == "mcp__inherited_canary__touch" for item in items)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "partial",
    [
        b"POST /v1/responses HTTP/1.1\r\nContent-Length: 2\r\n",
        b"POST /v1/responses HTTP/1.1\r\nContent-Length: 20\r\n\r\n{}",
    ],
    ids=("headers", "body"),
)
async def test_loopback_shutdown_closes_stalled_connections(partial: bytes) -> None:
    responses = await _LoopbackResponses((b"unused",)).__aenter__()
    _, writer = await asyncio.open_connection(
        "127.0.0.1", int(responses.base_url.rsplit(":", 1)[1].split("/", 1)[0])
    )
    writer.write(partial)
    await writer.drain()
    closing = asyncio.create_task(responses.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(asyncio.shield(closing), timeout=0.5)
    finally:
        writer.close()
        await writer.wait_closed()
        if not closing.done():
            await asyncio.wait_for(closing, timeout=1)


@pytest.mark.official_client
@pytest.mark.parametrize(
    "scope",
    required_codex_live_scopes(),
    ids=lambda scope: scope.name,
)
async def test_official_client_denies_native_surfaces_and_round_trips_one_forge_tool(
    tmp_path, scope
):
    root = Path(__file__).parents[4]
    executable = (
        root
        / ".llm-output/codex-client-0.153.4/node_modules/@openai/codex-win32-x64"
        / "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    )
    if not executable.is_file():
        pytest.skip("workspace-local official Codex 0.153.4 is not installed")

    cwd, home = tmp_path / "repository", tmp_path / "client-home"
    cwd.mkdir()
    home.mkdir()
    (cwd / "README.md").write_text("offline conformance fixture\n", encoding="utf-8")
    mcp_marker, hook_marker, notify_marker = (
        tmp_path / "mcp-ran",
        tmp_path / "hook-ran",
        tmp_path / "notify-ran",
    )
    python = str(Path(sys.executable).resolve(strict=True))

    def marker_command(marker: Path) -> str:
        return f"from pathlib import Path;Path({str(marker)!r}).write_text('ran')"

    home.joinpath("config.toml").write_text(
        "\n".join(
            (
                "notify = " + json.dumps([python, "-c", marker_command(notify_marker)]),
                "[mcp_servers.inherited_canary]",
                "command = " + json.dumps(python),
                "args = " + json.dumps(["-c", marker_command(mcp_marker)]),
                "enabled = true",
                "[[hooks.UserPromptSubmit]]",
                "[[hooks.UserPromptSubmit.hooks]]",
                'type = "command"',
                "command = " + json.dumps(f'{python} -c "{marker_command(hook_marker)}"'),
            )
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    installation = CodexInstallation(
        executable=str(executable),
        cwd=str(cwd),
        model=scope.model,
        effort=scope.effort,
        client_home=str(home),
        account=codex_account_identity("codex@example.invalid"),
        executable_digest=digest,
        disabled_mcp_servers=("inherited_canary",),
        duration_seconds=20,
    )

    result = await CodexOfficialConformanceHarness(
        side_effect_markers=(mcp_marker, hook_marker, notify_marker)
    ).run(installation, scope)

    assert result.environmentless
    assert result.configuration_isolated and result.only_forge_tools_advertised
    assert set(result.forbidden_tool_calls_denied) == {
        "apply_patch",
        "custom:apply_patch",
        "custom:functions.exec",
        "exec_command",
        "hosted:image_generation_call",
        "hosted:web_search_call",
        "mcpServer/tool/call",
        "mcp__inherited_canary__touch",
        "native:local_shell_call",
        "read_file",
        "request_user_input",
        "spawn_agent",
        "web_search",
    }
    assert result.callback_identity_bound and result.callback_result_forwarded
    assert result.side_effects_absent and not result.credentials_sent
    assert result.terminal_proof.permits_decision
    assert not result.publishable and not result.live_provider_call
    assert result.client_version == installation.client_version
    sanitized = result.sanitized_payload()
    assert sanitized["installation"]["client_version"] == installation.client_version
    assert sanitized["verifier"]["version"] != "1"
    publication = sanitized["publication"]
    assert sanitized["schema_version"] == 1
    assert publication["billing_enforcement_proven"] is False
    assert "subscription_route_binding_proven" not in publication
    payload = json.dumps(sanitized, sort_keys=True)
    assert str(tmp_path) not in payload and "codex@example.invalid" not in payload


@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        ("codex/0.154.0 (x86_64-pc-windows-msvc)", True),
        ("codex/0.154.0", True),
        ("codex/0.153.4 (x86_64-pc-windows-msvc)", False),
        ("codex/0.99.0 (x86_64-pc-windows-msvc)", False),
        ("codex/10.154.0", False),
        ("codex/0.154.0.1", False),
        ("unknown-client/1.0", False),
        (None, False),
        (12345, False),
    ],
    ids=[
        "configured_match",
        "bare_version_match",
        "stale_default",
        "different_version",
        "prefix_version_mismatch",
        "suffix_version_mismatch",
        "no_version",
        "none",
        "non_string",
    ],
)
def test_user_agent_matches_client_version(user_agent: object, expected: bool) -> None:
    assert _user_agent_matches_client_version(user_agent, "0.154.0") is expected
