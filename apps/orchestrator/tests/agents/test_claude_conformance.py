"""Bounded offline conformance for the pinned official Claude client."""

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest
from forge.agents.claude_conformance import (
    ClaudeConformanceError,
    ClaudeOfficialConformanceHarness,
    _await_initialization,
    _await_settings,
    _await_terminal,
    _LoopbackMessages,
    _validate_configuration,
    required_claude_live_scopes,
)
from forge.agents.claude_gateway import ClaudeInstallation, claude_tool_alias
from forge.agents.claude_protocol import ClaudeStreamCodec
from forge.domain.subscription import SpecialistPurpose


def test_only_the_opus_review_scope_requires_an_authorized_live_proof() -> None:
    scopes = required_claude_live_scopes()

    assert [(scope.name, scope.model, scope.effort, scope.role) for scope in scopes] == [
        (
            "opus-independent-review",
            "claude-opus-5",
            "medium",
            SpecialistPurpose.INDEPENDENT_REVIEW,
        )
    ]


async def test_managed_policy_is_rejected_before_the_client_starts(tmp_path) -> None:
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"official-client-fixture")
    home = tmp_path / "home"
    home.mkdir()
    home.joinpath("managed-settings.json").write_text("{}", encoding="utf-8")
    scope = required_claude_live_scopes()[0]
    installation = ClaudeInstallation(
        executable=str(executable),
        cwd=str(tmp_path),
        model=scope.model,
        effort=scope.effort,
        client_home=str(home),
        account="claude-test-account",
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )

    class NoLaunch:
        async def start(self, *_args, **_kwargs):
            raise AssertionError("managed policy must be rejected before launch")

    with pytest.raises(ClaudeConformanceError, match="managed policy"):
        await ClaudeOfficialConformanceHarness(supervisor=NoLaunch()).run(installation, scope)


def test_applied_effort_drift_is_rejected(tmp_path) -> None:
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"official-client-fixture")
    home = tmp_path / "home"
    home.mkdir()
    scope = required_claude_live_scopes()[0]
    installation = ClaudeInstallation(
        executable=str(executable),
        cwd=str(tmp_path),
        model=scope.model,
        effort=scope.effort,
        client_home=str(home),
        account="claude-test-account",
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    managed = {
        "allowManagedHooksOnly": True,
        "disableClaudeAiConnectors": True,
        "disableCommandPluginSources": True,
        "syncClaudeAiPlugins": False,
        "syncClaudeAiSkills": False,
    }
    init = {
        "claude_code_version": "2.1.263",
        "session_id": "session",
        "model": scope.model,
        "permissionMode": "dontAsk",
        "tools": [*(claude_tool_alias(tool) for tool in scope.tool_surface), "StructuredOutput"],
        "slash_commands": [],
        "skills": [],
        "plugins": [],
        "mcp_servers": [{"name": "forge", "status": "connected"}],
    }
    settings = {
        "applied": {
            "advisor": None,
            "effort": "low",
            "model": scope.model,
            "ultracode": False,
        },
        "effective": managed,
        "sources": [{"source": "policySettings", "settings": managed}],
    }

    with pytest.raises(ClaudeConformanceError, match="unisolated capability"):
        _validate_configuration(init, settings, installation, scope, "session")


async def test_late_initialization_requires_the_complete_mcp_handshake(monkeypatch) -> None:
    scope = required_claude_live_scopes()[0]

    async def handle(_call):
        return {"status": "succeeded"}

    codec = ClaudeStreamCodec(
        "session",
        "turn",
        handle,
        frozenset(tool.value for tool in scope.tool_surface),
    )

    class Session:
        calls = 0

        async def receive(self):
            self.calls += 1
            if self.calls == 1:
                return {"type": "system", "subtype": "init"}
            raise AssertionError("initialization must be rejected immediately")

    monkeypatch.setattr(
        "forge.agents.claude_conformance._validate_configuration",
        lambda *_args: None,
    )
    with pytest.raises(ClaudeConformanceError, match="before MCP handshake completion"):
        await _await_terminal(
            Session(),
            codec,
            None,
            {},
            None,
            scope,
            "session",
            asyncio.Event(),
        )


@pytest.mark.parametrize("phase", ["initialization", "settings"])
async def test_setup_phases_never_dispatch_forge_callbacks(phase: str) -> None:
    calls = []

    async def handle(call):
        calls.append(call)
        return {"status": "succeeded"}

    def control(request_id, message):
        return {
            "type": "control_request",
            "request_id": request_id,
            "request": {
                "subtype": "mcp_message",
                "server_name": "forge",
                "message": message,
            },
        }

    frames = [
        control(
            "mcp-init",
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
        ),
        control(
            "mcp-ready",
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ),
        control(
            "mcp-list",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ),
        control(
            "early-call",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "repository.read_file",
                    "arguments": {"path": "README.md"},
                    "_meta": {
                        "claudecode/toolUseId": "forge-call-early",
                        "progressToken": 1,
                    },
                },
            },
        ),
    ]
    frames.append(
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "forge_initialize" if phase == "initialization" else "forge_settings",
                "response": {},
            },
        }
    )

    class Session:
        async def receive(self):
            return frames.pop(0)

        async def send(self, _value):
            return None

    codec = ClaudeStreamCodec("session", "turn", handle, frozenset({"repository.read_file"}))
    with pytest.raises(ClaudeConformanceError, match="before capability admission"):
        if phase == "initialization":
            await _await_initialization(Session(), codec)
        else:
            await _await_settings(Session(), codec)

    assert calls == []


@pytest.mark.parametrize(
    "partial",
    [
        b"POST /v1/messages HTTP/1.1\r\nContent-Length: 2\r\n",
        b"POST /v1/messages HTTP/1.1\r\nContent-Length: 20\r\n\r\n{}",
    ],
    ids=("headers", "body"),
)
async def test_loopback_shutdown_closes_stalled_connections(partial: bytes) -> None:
    messages = await _LoopbackMessages((b"unused",)).__aenter__()
    _, writer = await asyncio.open_connection("127.0.0.1", int(messages.base_url.rsplit(":", 1)[1]))
    writer.write(partial)
    await writer.drain()
    closing = asyncio.create_task(messages.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(asyncio.shield(closing), timeout=0.5)
    finally:
        writer.close()
        await writer.wait_closed()
        if not closing.done():
            await asyncio.wait_for(closing, timeout=1)


@pytest.mark.official_client
async def test_official_client_denies_inherited_surfaces_and_round_trips_one_forge_tool(
    tmp_path, monkeypatch
) -> None:
    root = Path(__file__).parents[4]
    executable = (
        root
        / ".llm-output/claude-client-2.1.263/node_modules"
        / "@anthropic-ai/claude-code-win32-x64/claude.exe"
    )
    if not executable.is_file():
        pytest.skip("workspace-local official Claude 2.1.263 is not installed")

    scope = required_claude_live_scopes()[0]
    cwd, home = tmp_path / "repository", tmp_path / "client-home"
    cwd.mkdir()
    home.mkdir()
    (cwd / "README.md").write_text("offline conformance fixture\n", encoding="utf-8")
    project_settings = cwd / ".claude"
    project_settings.mkdir()
    skill = home / "skills" / "inherited-canary"
    skill.mkdir(parents=True)
    hook_marker, managed_hook_marker, project_hook_marker, mcp_marker, auth_marker, skill_marker = (
        tmp_path / "home-hook-ran",
        tmp_path / "managed-hook-ran",
        tmp_path / "project-hook-ran",
        tmp_path / "mcp-ran",
        tmp_path / "auth-helper-ran",
        tmp_path / "skill-ran",
    )
    canary = tmp_path / "canary.py"
    canary.write_text(
        "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('ran')\n",
        encoding="utf-8",
    )
    python = str(Path(sys.executable).resolve(strict=True))

    def command(marker: Path) -> str:
        return f'"{python}" "{canary}" "{marker}"'

    def hook(marker: Path) -> list[dict[str, object]]:
        return [{"hooks": [{"type": "command", "command": command(marker)}]}]

    ambient_policy = tmp_path / "ambient-managed-policy"
    ambient_policy.mkdir()
    ambient_policy.joinpath("managed-settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": hook(managed_hook_marker)}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CODE_MANAGED_SETTINGS_PATH", str(ambient_policy))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-api-key-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-token-must-not-be-used")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-oauth-must-not-be-used")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("CLAUDE_CODE_USE_FOUNDRY", "1")

    (home / "settings.json").write_text(
        json.dumps(
            {
                "apiKeyHelper": command(auth_marker),
                "enableAllProjectMcpServers": True,
                "enabledMcpjsonServers": ["inherited_canary"],
                "hooks": {
                    "SessionStart": hook(hook_marker),
                    "UserPromptSubmit": hook(hook_marker),
                },
            }
        ),
        encoding="utf-8",
    )
    project_settings.joinpath("settings.json").write_text(
        json.dumps({"hooks": {"UserPromptSubmit": hook(project_hook_marker)}}),
        encoding="utf-8",
    )
    cwd.joinpath(".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "inherited_canary": {
                        "type": "stdio",
                        "command": python,
                        "args": [str(canary), str(mcp_marker)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    skill.joinpath("SKILL.md").write_text(
        "---\nname: inherited-canary\ndescription: must remain unavailable\n---\n"
        + f"!`{command(skill_marker)}`\n",
        encoding="utf-8",
    )
    home.joinpath(".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "fixture-must-not-be-used"}}),
        encoding="utf-8",
    )

    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    installation = ClaudeInstallation(
        executable=str(executable.resolve(strict=True)),
        cwd=str(cwd),
        model=scope.model,
        effort=scope.effort,
        client_home=str(home),
        account="claude-test-account",
        executable_digest=digest,
        duration_seconds=30,
    )
    markers = (
        hook_marker,
        managed_hook_marker,
        project_hook_marker,
        mcp_marker,
        auth_marker,
        skill_marker,
    )

    result = await ClaudeOfficialConformanceHarness(side_effect_markers=markers).run(
        installation, scope
    )

    assert result.configuration_isolated and result.only_forge_tools_advertised
    assert set(result.forbidden_tool_calls_denied) == {
        "Bash",
        "Read",
        "Skill",
        "WebFetch",
        "mcp__inherited_canary__touch",
    }
    assert result.callback_identity_bound and result.callback_result_forwarded
    assert result.side_effects_absent and result.alternate_auth_isolated
    assert result.terminal_proof.permits_decision
    assert not result.publishable and not result.live_provider_call
    payload = json.dumps(result.sanitized_payload(), sort_keys=True)
    assert str(tmp_path) not in payload and "fixture-must-not-be-used" not in payload
