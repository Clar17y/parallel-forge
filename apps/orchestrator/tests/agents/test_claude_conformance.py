"""Bounded offline conformance for the pinned official Claude client."""

import asyncio
import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from forge.agents.claude_conformance import (
    _OFFLINE_API_KEY,
    _RESULT_SCHEMA,
    ClaudeConformanceError,
    ClaudeOfficialConformanceHarness,
    _await_initialization,
    _await_settings,
    _await_terminal,
    _HttpRequest,
    _inspect_requests,
    _LoopbackMessages,
    _validate_configuration,
    required_claude_live_scopes,
)
from forge.agents.claude_gateway import (
    ClaudeInstallation,
    claude_isolation_platform_supported,
    claude_launch_arguments,
    claude_tool_alias,
)
from forge.agents.claude_protocol import ClaudeStreamCodec
from forge.agents.client_process import (
    ClientLaunchSpec,
    ClientProcessResult,
    ClientProcessSession,
    ClientProcessSupervisor,
)
from forge.domain.subscription import SpecialistPurpose


def _official_claude_executable(root: Path) -> Path:
    explicit = os.environ.get("FORGE_CLAUDE_OFFICIAL_CLIENT")
    if explicit:
        return Path(explicit)
    linux_candidate = (
        root
        / ".llm-output/claude-client-2.1.263/node_modules"
        / "@anthropic-ai/claude-code-linux-x64/claude"
    )
    if linux_candidate.is_file():
        return linux_candidate
    if os.name == "nt":
        windows_candidate = (
            root
            / ".llm-output/claude-client-2.1.263/node_modules"
            / "@anthropic-ai/claude-code-win32-x64/claude.exe"
        )
        if windows_candidate.is_file():
            return windows_candidate
    return linux_candidate


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


async def test_claude_conformance_launch_omits_managed_settings_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "forge.agents.claude_gateway.claude_isolation_platform_supported", lambda: True
    )
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"official-client-fixture")
    (tmp_path / "README.md").write_text("sentinel", encoding="utf-8")
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

    launches = []

    class LaunchObserved:
        async def start(self, spec, **_kwargs):
            launches.append(spec)
            raise AssertionError("launch observed")

    with pytest.raises(AssertionError, match="launch observed"):
        await ClaudeOfficialConformanceHarness(supervisor=LaunchObserved()).run(installation, scope)
    assert "CLAUDE_CODE_MANAGED_SETTINGS_PATH" not in launches[0].environment
    assert launches[0].environment["CLAUDE_CODE_ENTRYPOINT"] == "local-agent"
    assert launches[0].environment["CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"] == "1"
    assert "--strict-mcp-config" in launches[0].argv


async def test_unsupported_platform_refuses_conformance_before_launch(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "forge.agents.claude_gateway.claude_isolation_platform_supported", lambda: False
    )
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

    class NoLaunch:
        async def start(self, *_args, **_kwargs):
            raise AssertionError("unsupported platform must not launch")

    with pytest.raises(ClaudeConformanceError, match="platform is unsupported"):
        await ClaudeOfficialConformanceHarness(supervisor=NoLaunch()).run(installation, scope)


async def test_conformance_harness_offloads_platform_probe_from_event_loop(
    tmp_path, monkeypatch
) -> None:
    import threading

    probe_threads: list[threading.Thread] = []

    def _probe() -> bool:
        probe_threads.append(threading.current_thread())
        return False

    monkeypatch.setattr("forge.agents.claude_gateway.claude_isolation_platform_supported", _probe)
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

    with pytest.raises(ClaudeConformanceError, match="platform is unsupported"):
        await ClaudeOfficialConformanceHarness().run(installation, scope)

    assert len(probe_threads) == 1
    assert probe_threads[0] is not threading.main_thread()


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


def test_operational_tool_errors_do_not_prove_forbidden_tools_are_unavailable() -> None:
    scope = required_claude_live_scopes()[0]
    tools = [
        {"type": "custom", "name": name}
        for name in sorted(
            {claude_tool_alias(tool) for tool in scope.tool_surface} | {"StructuredOutput"}
        )
    ]
    base = {"model": scope.model, "tools": tools}

    def request(body):
        return _HttpRequest(
            path="/v1/messages",
            headers={"x-api-key": "forge-offline-conformance"},
            body=body,
        )

    denied = {
        **base,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"forbidden-{index}",
                        "is_error": True,
                        "content": "Error: file not found",
                    }
                    for index in range(1, 6)
                ],
            }
        ],
    }
    forwarded = {
        **base,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "forge-call-1",
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({"path": "README.md", "status": "succeeded"}),
                            }
                        ],
                    }
                ],
            }
        ],
    }

    with pytest.raises(ClaudeConformanceError, match="admitted a forbidden"):
        _inspect_requests((request(base), request(denied), request(forwarded)), scope)


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
    if not claude_isolation_platform_supported():
        pytest.skip("native platform does not support Claude managed-policy isolation")
    root = Path(__file__).parents[4]
    executable = _official_claude_executable(root)
    if not executable.is_file():
        pytest.skip("official Claude 2.1.263 is not installed")

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
    (home / "managed-settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": hook(managed_hook_marker)}}), encoding="utf-8"
    )
    managed_directory = home / "managed-settings.d"
    managed_directory.mkdir()
    (managed_directory / "canary.json").write_text(
        json.dumps({"hooks": {"SessionStart": hook(managed_hook_marker)}}), encoding="utf-8"
    )
    (home / "managed-mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "managed_canary": {
                        "type": "stdio",
                        "command": python,
                        "args": [str(canary), str(mcp_marker)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (home / "remote-settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": hook(managed_hook_marker)}}),
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
        json.dumps(
            {
                "claudeAiOauth": {"accessToken": "fixture-must-not-be-used"},
                "enterpriseGateway": {
                    "url": "https://127.0.0.1:1",
                    "expiresAt": 4102444800000,
                },
                "gatewayTrust": {
                    "127.0.0.1": "fixture-pinned-fingerprint",
                },
            }
        ),
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

    # Bounded negative control: stripping CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST restores
    # planted gateway credentials, causing the client to attempt remote-settings gateway
    # connection and fail before Forge admission, demonstrating the bypass surface is observable.
    negative_spec = ClientLaunchSpec(
        argv=(
            installation.executable,
            *claude_launch_arguments(
                installation,
                session_id=str(uuid4()),
                system_prompt="Use only the supplied Forge MCP tools.",
                permitted_tools=frozenset(scope.tool_surface),
                schema=_RESULT_SCHEMA,
            ),
        ),
        cwd=installation.cwd,
        environment={
            "ANTHROPIC_API_KEY": _OFFLINE_API_KEY,
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:1",
            "CLAUDE_CONFIG_DIR": installation.client_home,
            "CLAUDE_CODE_ENTRYPOINT": "local-agent",
        },
        allowed_environment=frozenset(
            {
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_BASE_URL",
                "CLAUDE_CONFIG_DIR",
                "CLAUDE_CODE_ENTRYPOINT",
            }
        ),
        executable_digest=installation.executable_digest,
        duration_seconds=5,
    )
    negative_session = await ClientProcessSupervisor().start(negative_spec)
    try:
        # Let the providerless client exercise its planted enterprise-gateway
        # credential and terminate naturally; do not kill it before the bypass is
        # observable.
        negative_result = await asyncio.wait_for(
            negative_session.wait_closed(), timeout=negative_spec.duration_seconds + 3.0
        )
        assert negative_result.return_code is not None
        assert "Couldn't load settings from Cloud gateway" in negative_result.stderr
        assert not managed_hook_marker.exists()
    finally:
        await negative_session.close()

    launches = []
    sessions: list[ClientProcessSession] = []

    class Capture:
        async def start(self, spec, **kwargs):
            launches.append(spec)
            session = await ClientProcessSupervisor().start(spec, **kwargs)
            sessions.append(session)
            return session

    result = await ClaudeOfficialConformanceHarness(
        side_effect_markers=markers, supervisor=Capture()
    ).run(installation, scope)

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
    assert launches[0].environment["CLAUDE_CODE_ENTRYPOINT"] == "local-agent"
    assert launches[0].environment["CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"] == "1"
    assert "CLAUDE_CODE_MANAGED_SETTINGS_PATH" not in launches[0].environment
    assert set(launches[0].environment) == {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
    } | ({"SystemRoot"} if os.name == "nt" else set())
    forbidden_credential_keys = {
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CCR_OAUTH_TOKEN_FILE",
        "CLAUDE_CODE_USE_GATEWAY",
        "CLAUDE_CODE_MANAGED_SETTINGS_PATH",
    }
    assert not (set(launches[0].environment) & forbidden_credential_keys)
    assert "--strict-mcp-config" in launches[0].argv
    assert not result.publishable and not result.live_provider_call
    assert "Cloud gateway" not in (await sessions[0].wait_closed()).stderr
    sanitized = result.sanitized_payload()
    publication = sanitized["publication"]
    assert sanitized["schema_version"] == 1
    assert publication["billing_enforcement_proven"] is False
    assert "subscription_route_binding_proven" not in publication
    payload = json.dumps(sanitized, sort_keys=True)
    assert (
        str(tmp_path) not in payload
        and "fixture-must-not-be-used" not in payload
        and "fixture-pinned-fingerprint" not in payload
    )


class _AuthStatusSupervisor(ClientProcessSupervisor):
    """Run an official client command whose JSON output may be formatted across multiple lines."""

    async def execute_status(
        self, spec: ClientLaunchSpec, *, timeout: float | None = None
    ) -> tuple[dict[str, Any], ClientProcessResult]:
        buffer = bytearray()
        orig_admit = ClientProcessSession._admit_frame

        def _admit(session: ClientProcessSession, line: bytes | bytearray) -> None:
            buffer.extend(line)
            buffer.extend(b"\n")
            try:
                decoded = json.loads(buffer.decode("utf-8"))
            except ValueError, UnicodeError:
                return
            if not isinstance(decoded, dict):
                return
            session._frames.append(decoded)
            session._queue.put_nowait(copy.deepcopy(decoded))
            buffer.clear()

        wait_timeout = timeout if timeout is not None else spec.duration_seconds + 3.0
        session: ClientProcessSession | None = None
        ClientProcessSession._admit_frame = _admit
        try:
            session = await self.start(spec)
            result = await asyncio.wait_for(session.wait_closed(), timeout=wait_timeout)
            if not result.frames:
                raise RuntimeError(
                    f"Official client emitted no valid status frame: {result.stderr}"
                )
            return dict(result.frames[0]), result
        finally:
            ClientProcessSession._admit_frame = orig_admit
            if session is not None:
                await session.close()


async def test_auth_status_supervisor_closes_session_on_timeout_and_error() -> None:
    closed = False

    class FakeSession:
        def __init__(self) -> None:
            self.deadline = 100.0
            self._frames: list[dict[str, Any]] = []

        async def wait_closed(self) -> ClientProcessResult:
            raise TimeoutError()

        async def close(self, *, completed: bool = False) -> None:
            nonlocal closed
            closed = True

    class FakeSupervisor(_AuthStatusSupervisor):
        async def start(self, spec: ClientLaunchSpec, **kwargs: Any) -> Any:
            return FakeSession()

    supervisor = FakeSupervisor()
    spec = ClientLaunchSpec(
        argv=(str(Path(__file__).resolve()),),
        cwd=".",
        environment={},
        allowed_environment=frozenset(),
        executable_digest="a" * 64,
        duration_seconds=2.0,
    )
    with pytest.raises(asyncio.TimeoutError):
        await supervisor.execute_status(spec, timeout=0.1)

    assert closed is True


async def test_auth_status_supervisor_restores_decoder_when_start_fails() -> None:
    original_admit = ClientProcessSession._admit_frame

    class StartFails(_AuthStatusSupervisor):
        async def start(self, spec: ClientLaunchSpec, **kwargs: Any) -> Any:
            raise RuntimeError("start failed")

    spec = ClientLaunchSpec(
        argv=(str(Path(__file__).resolve()),),
        cwd=".",
        environment={},
        allowed_environment=frozenset(),
        executable_digest="a" * 64,
        duration_seconds=2.0,
    )
    try:
        with pytest.raises(RuntimeError, match="start failed"):
            await StartFails().execute_status(spec)
        assert ClientProcessSession._admit_frame is original_admit
    finally:
        ClientProcessSession._admit_frame = original_admit


@pytest.mark.official_client
async def test_official_client_auth_status_characterization_proves_host_managed_suppression(
    tmp_path: Path,
) -> None:
    if os.environ.get("FORGE_CLAUDE_OFFICIAL_NETWORK_ISOLATED") != "1":
        pytest.skip("auth-status characterization requires explicit network isolation")
    root = Path(__file__).parents[4]
    executable = _official_claude_executable(root)
    if not executable.is_file():
        pytest.skip("official Claude 2.1.263 is not installed")

    home = tmp_path / "client-home"
    home.mkdir()
    dummy_token = "dummy-oauth-token-for-test-characterization"
    home.joinpath(".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": dummy_token,
                    "scopes": ["user:inference"],
                }
            }
        ),
        encoding="utf-8",
    )

    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    supervisor = _AuthStatusSupervisor()

    forbidden_credential_keys = {
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CCR_OAUTH_TOKEN_FILE",
        "CLAUDE_CODE_USE_GATEWAY",
        "CLAUDE_CODE_MANAGED_SETTINGS_PATH",
    }

    # Case 1: local-agent only recognized as stored first-party subscription auth
    spec_local_agent = ClientLaunchSpec(
        argv=(str(executable.resolve(strict=True)), "auth", "status", "--json"),
        cwd=str(tmp_path),
        environment={
            "CLAUDE_CONFIG_DIR": str(home),
            "CLAUDE_CODE_ENTRYPOINT": "local-agent",
        },
        allowed_environment=frozenset({"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENTRYPOINT"}),
        executable_digest=digest,
        duration_seconds=5,
    )
    status_local_agent, result_local_agent = await supervisor.execute_status(spec_local_agent)
    assert result_local_agent.return_code == 0
    assert result_local_agent.stop_confirmed and result_local_agent.outcome == "exited"
    assert status_local_agent["loggedIn"] is True
    assert status_local_agent["authMethod"] == "claude.ai"
    assert status_local_agent["apiProvider"] == "firstParty"

    # Case 2: adding CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST suppresses stored auth
    spec_host_managed = ClientLaunchSpec(
        argv=(str(executable.resolve(strict=True)), "auth", "status", "--json"),
        cwd=str(tmp_path),
        environment={
            "CLAUDE_CONFIG_DIR": str(home),
            "CLAUDE_CODE_ENTRYPOINT": "local-agent",
            "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1",
        },
        allowed_environment=frozenset(
            {
                "CLAUDE_CONFIG_DIR",
                "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
            }
        ),
        executable_digest=digest,
        duration_seconds=5,
    )
    status_host_managed, result_host_managed = await supervisor.execute_status(spec_host_managed)
    assert result_host_managed.return_code == 1
    assert result_host_managed.stop_confirmed and result_host_managed.outcome == "exited"
    assert status_host_managed["loggedIn"] is False
    assert status_host_managed["authMethod"] == "none"
    assert status_host_managed["apiProvider"] == "firstParty"

    # Sanitize and assert no dummy credential value appears in retained payload or output
    payload_local = json.dumps(status_local_agent)
    payload_host = json.dumps(status_host_managed)
    assert dummy_token not in payload_local
    assert dummy_token not in result_local_agent.stderr
    assert dummy_token not in payload_host
    assert dummy_token not in result_host_managed.stderr

    # Both production and conformance closed environments contain no credential injection keys
    assert not (set(spec_local_agent.environment) & forbidden_credential_keys)
    assert not (set(spec_host_managed.environment) & forbidden_credential_keys)
