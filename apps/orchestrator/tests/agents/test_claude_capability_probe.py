"""Hermetic, providerless contract tests for the Claude capability probe."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from forge.agents import claude_gateway
from forge.agents.claude_capability_probe import (
    ClaudeCapabilityProbe,
    ClaudeCapabilityProbeError,
    ClaudeLiveRouteObservation,
    SupervisedClaudeAuthStatusRunner,
    SupervisedClaudeLiveRouteRunner,
    claude_subscription_environment,
    parse_claude_subscription_auth_status,
)
from forge.agents.claude_conformance import ClaudeConformanceResult
from forge.agents.claude_gateway import ClaudeInstallation
from forge.agents.claude_verification import (
    CLAUDE_VERIFIER_ID,
    CLAUDE_VERIFIER_VERSION,
    required_claude_verification_scopes,
)
from forge.agents.client_process import (
    ClientProcessError,
    ClientProcessReceipt,
    ClientProcessResult,
    ClientProcessTimeout,
)
from forge.domain.capability_evidence import capability_home_digest
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof


def _digest(value="account-123"):
    return hashlib.sha256(value.encode()).hexdigest()


def _status(**changes):
    value = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "max",
        "account": {"id": "account-123"},
    }
    value.update(changes)
    return json.dumps(value)


def _proof(certain=True):
    return SubscriptionLaunchTerminalProof(
        launch_id="proof",
        pid=123,
        process_identity="process",
        outcome="completed" if certain else "stop_uncertain",
        return_code=0 if certain else None,
        stop_confirmed=certain,
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
    )


class Stage:
    def __init__(self, result, events, name):
        self.result, self.events, self.name, self.calls = result, events, name, 0

    async def run(self, *_):
        self.calls += 1
        self.events.append(self.name)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class WrongScope:
    def evidence_scope(self):
        return object()


@pytest.fixture
def parts(tmp_path):
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"future-official-client")
    home = tmp_path / "home"
    home.mkdir()
    verification = required_claude_verification_scopes()[0]
    scope = verification.evidence_scope()
    installation = ClaudeInstallation(
        str(executable),
        str(tmp_path),
        verification.model,
        verification.effort,
        str(home),
        _digest(),
        hashlib.sha256(executable.read_bytes()).hexdigest(),
        client_version="99.7.3",
        duration_seconds=1,
    )
    offline = ClaudeConformanceResult(
        verification,
        installation.client_version,
        installation.executable_digest,
        capability_home_digest(installation.client_home),
        installation.account,
        True,
        True,
        ("Bash", "Read", "Skill", "WebFetch", "mcp__inherited_canary__touch"),
        True,
        True,
        True,
        True,
        _proof(),
    )
    live = ClaudeLiveRouteObservation(
        "99.7.3", installation.model, installation.effort, "a" * 64, "b" * 64
    )
    return installation, scope, offline, live


def make_probe(parts, *, offline=None, auth=None, live=None):
    events = []
    conformance = Stage(parts[2] if offline is None else offline, events, "offline")
    auth_runner = Stage(_status() if auth is None else auth, events, "auth")
    live_runner = Stage(parts[3] if live is None else live, events, "live")
    return (
        ClaudeCapabilityProbe(parts[0], parts[1], conformance, auth_runner, live_runner),
        conformance,
        auth_runner,
        live_runner,
        events,
    )


@pytest.mark.asyncio
async def test_exact_success_binds_newer_manifest_and_all_five_observations(parts):
    probe, _, _, _, events = make_probe(parts)
    observed = await probe.observe(authorize_provider_contact=True)
    installation, scope, _, live = parts
    assert events == ["offline", "auth", "live"]
    assert (
        observed.client_identity.client,
        observed.client_identity.client_version,
        observed.client_identity.reported_client_version,
    ) == ("claude_code", "99.7.3", "99.7.3")
    assert observed.client_identity.executable_digest == installation.executable_digest
    assert observed.client_identity.client_home_digest == capability_home_digest(
        installation.client_home
    )
    assert observed.account_authentication.account == installation.account
    assert observed.account_authentication.account_kind == "subscription"
    assert (
        observed.route_identity.model,
        observed.route_identity.effort,
        observed.route_identity.turn_observation_digest,
    ) == (installation.model, installation.effort, live.turn_digest)
    assert observed.subscription_route_binding.paid_credential_names_scrubbed is True
    assert observed.subscription_route_binding.fallback_disabled is True
    assert observed.tool_isolation.tool_surface == tuple(t.value for t in scope.tool_surface)
    assert observed.tool_isolation.advertised_tool_surface_digest == live.tool_surface_digest
    assert (observed.verifier_id, observed.verifier_version) == (
        CLAUDE_VERIFIER_ID,
        CLAUDE_VERIFIER_VERSION,
    )


@pytest.mark.asyncio
async def test_authorization_false_starts_no_stage(parts):
    probe, offline, auth, live, events = make_probe(parts)
    with pytest.raises(ClaudeCapabilityProbeError, match="^provider_contact_not_authorized$"):
        await probe.observe()
    assert events == [] and (offline.calls, auth.calls, live.calls) == (0, 0, 0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scope", WrongScope()),
        ("client_version", "99.7.2"),
        ("executable_digest", "0" * 64),
        ("client_home_digest", "0" * 64),
        ("account", "0" * 64),
        ("configuration_isolated", False),
        ("only_forge_tools_advertised", False),
        ("callback_identity_bound", False),
        ("callback_result_forwarded", False),
        ("side_effects_absent", False),
        ("alternate_auth_isolated", False),
        ("terminal_proof", _proof(False)),
    ],
)
@pytest.mark.asyncio
async def test_offline_failure_matrix_stops_before_auth_or_live(parts, field, value):
    probe, _, auth, live, events = make_probe(parts, offline=replace(parts[2], **{field: value}))
    expected = {
        "client_version": "version_mismatch",
        "executable_digest": "executable_digest_mismatch",
        "account": "account_identity_unbound",
    }.get(field, "isolation_configuration_failed")
    with pytest.raises(ClaudeCapabilityProbeError, match=f"^{expected}$"):
        await probe.observe(authorize_provider_contact=True)
    assert events == ["offline"] and auth.calls == live.calls == 0


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (_status(loggedIn=False), "subscription_signed_out"),
        (_status(authMethod="apiKey"), "subscription_authentication_failed"),
        (_status(authMethod="authToken"), "subscription_authentication_failed"),
        (_status(apiProvider="bedrock"), "subscription_authentication_failed"),
        (_status(apiProvider="vertex"), "subscription_authentication_failed"),
        (_status(subscriptionType="unknown"), "subscription_type_unrecognized"),
        (_status(account={}), "account_identity_missing"),
        (_status(account={"id": "other"}), "account_identity_unbound"),
        (_status(accountId="one", email="two"), "account_identity_missing"),
        ("not-json", "auth_status_invalid"),
        (json.dumps([]), "auth_status_invalid"),
        ("x" * (16 * 1024 + 1), "auth_status_invalid"),
    ],
)
def test_auth_parser_failure_matrix(payload, reason):
    with pytest.raises(ClaudeCapabilityProbeError, match=f"^{reason}$"):
        parse_claude_subscription_auth_status(payload, expected_account_digest=_digest())


def test_auth_parser_accepts_duplicate_same_identity_and_hashes_it():
    parsed = parse_claude_subscription_auth_status(
        _status(accountId="account-123", account={"id": "account-123"}),
        expected_account_digest=_digest(),
    )
    assert (parsed.account_digest, parsed.subscription_type) == (_digest(), "max")


@pytest.mark.asyncio
async def test_auth_failure_stops_before_live(parts):
    probe, _, _, live, events = make_probe(parts, auth=_status(loggedIn=False))
    with pytest.raises(ClaudeCapabilityProbeError, match="^subscription_signed_out$"):
        await probe.observe(authorize_provider_contact=True)
    assert events == ["offline", "auth"] and live.calls == 0


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"reported_client_version": "99.7.2"}, "version_mismatch"),
        ({"model": "claude-other"}, "model_or_effort_unavailable"),
        ({"effort": "high"}, "model_or_effort_unavailable"),
        ({"turn_digest": "x" * 64}, "probe_protocol_failed"),
        ({"turn_digest": "a" * 63}, "probe_protocol_failed"),
        ({"tool_surface_digest": "B" * 64}, "probe_protocol_failed"),
        ({"tool_surface_digest": ""}, "probe_protocol_failed"),
    ],
)
@pytest.mark.asyncio
async def test_live_identity_and_digest_failure_matrix(parts, changes, reason):
    probe, _, _, _, events = make_probe(parts, live=replace(parts[3], **changes))
    with pytest.raises(ClaudeCapabilityProbeError, match=f"^{reason}$"):
        await probe.observe(authorize_provider_contact=True)
    assert events == ["offline", "auth", "live"]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ClientProcessError("secret"), "probe_protocol_failed"),
        (ClientProcessTimeout("secret"), "probe_timeout"),
        (TimeoutError("secret"), "probe_timeout"),
        (ValueError("secret"), "probe_protocol_failed"),
    ],
)
@pytest.mark.asyncio
async def test_live_process_failures_are_sanitized(parts, error, reason):
    probe, *_ = make_probe(parts, live=error)
    with pytest.raises(ClaudeCapabilityProbeError, match=f"^{reason}$") as caught:
        await probe.observe(authorize_provider_contact=True)
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_cancellation_propagates_and_starts_no_later_work(parts):
    probe, _, _, live, events = make_probe(parts, auth=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await probe.observe(authorize_provider_contact=True)
    assert events == ["offline", "auth"] and live.calls == 0


@pytest.mark.asyncio
async def test_prelaunch_digest_drift_never_starts_auth_or_live(parts):
    probe, _, auth, live, _ = make_probe(parts)
    Path(probe.installation.executable).write_bytes(b"drift")
    with pytest.raises(ClaudeCapabilityProbeError, match="^executable_digest_mismatch$"):
        await probe.observe(authorize_provider_contact=True)
    assert auth.calls == live.calls == 0


def test_subscription_environment_is_minimal_and_offline_remains_provider_suppressed(parts):
    environment = claude_subscription_environment(parts[0])
    assert environment == {
        "CLAUDE_CONFIG_DIR": parts[0].client_home,
        "CLAUDE_CODE_ENTRYPOINT": "local-agent",
    }
    forbidden = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AZURE_API_KEY",
        "PATH",
        "HOME",
    }
    assert forbidden.isdisjoint(environment)
    assert claude_gateway.CLAUDE_SUBSCRIPTION_LAUNCH_ENVIRONMENT == {
        "CLAUDE_CODE_ENTRYPOINT": "local-agent"
    }
    assert (
        claude_gateway.CLAUDE_ISOLATION_LAUNCH_ENVIRONMENT["CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"]
        == "1"
    )


class AuthSession:
    def __init__(self, frames, certain=True):
        self.frames, self.certain, self.close_calls = list(frames), certain, 0

    async def receive(self):
        return self.frames.pop(0)

    async def close(self, *, completed):
        self.close_calls += 1
        return ClientProcessResult(
            ClientProcessReceipt("auth", 8, "process", 1.0),
            0 if self.certain else None,
            (),
            0,
            "",
            0,
            False,
            False,
            "completed" if self.certain else "stop_uncertain",
            self.certain,
        )


class AuthSupervisor:
    def __init__(self, session):
        self.session, self.specs = session, []

    async def start(self, spec):
        self.specs.append(spec)
        return self.session


@pytest.mark.asyncio
async def test_supervised_auth_exact_command_clean_environment_terminal_and_close(parts):
    session = AuthSession([json.loads(_status()), None])
    supervisor = AuthSupervisor(session)
    runner = SupervisedClaudeAuthStatusRunner(supervisor=supervisor)
    environment = claude_subscription_environment(parts[0])
    assert json.loads(await runner.run(parts[0], environment))["loggedIn"] is True
    spec = supervisor.specs[0]
    assert spec.argv == (parts[0].executable, "--setting-sources=", "auth", "status")
    assert {key: spec.environment[key] for key in environment} == environment
    assert set(spec.environment) <= {*environment, "SystemRoot"}
    assert spec.allowed_environment <= frozenset({*environment, "SystemRoot"})
    assert spec.duration_seconds == 1 and session.close_calls == 1


@pytest.mark.parametrize(
    ("frames", "certain", "reason"),
    [
        ([[], None], True, "auth_status_invalid"),
        ([{}, {}], True, "auth_status_invalid"),
        ([{}, None], False, "probe_stop_uncertain"),
    ],
)
@pytest.mark.asyncio
async def test_supervised_auth_invalid_or_uncertain_closes_once(parts, frames, certain, reason):
    session = AuthSession(frames, certain)
    runner = SupervisedClaudeAuthStatusRunner(supervisor=AuthSupervisor(session))
    with pytest.raises(ClaudeCapabilityProbeError, match=f"^{reason}$"):
        await runner.run(parts[0], claude_subscription_environment(parts[0]))
    assert session.close_calls == 1


def test_policy_digest_is_independent_of_fixture_default_version():
    before = claude_gateway.CLAUDE_ISOLATION_POLICY_DIGEST
    assert "client_version" not in claude_gateway._isolation_policy_digest_payload()
    assert claude_gateway.CLAUDE_CLIENT_VERSION != "99.7.3"
    assert claude_gateway.CLAUDE_ISOLATION_POLICY_DIGEST == before


@pytest.mark.asyncio
async def test_supervised_live_route_hashes_canonical_tool_surface_json(parts):
    class Session:
        async def close(self, *, completed):
            return ClientProcessResult(
                ClientProcessReceipt("live", 8, "process", 1.0),
                0,
                (),
                0,
                "",
                0,
                False,
                False,
                "completed",
                True,
            )

    class Supervisor:
        async def start(self, _spec):
            return Session()

    runner = SupervisedClaudeLiveRouteRunner(supervisor=Supervisor())
    expected = hashlib.sha256(
        json.dumps(
            [tool.value for tool in parts[1].tool_surface], ensure_ascii=True, separators=(",", ":")
        ).encode()
    ).hexdigest()

    # The exchange is fixed local protocol plumbing, not a provider call.
    class Gateway:
        def _command(self, _request):
            return ()

        async def _exchange(self, *_args):
            return SimpleNamespace(failure=None, decision={"ok": True})

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "forge.agents.claude_capability_probe.ClaudeGateway", lambda *_args, **_kwargs: Gateway()
    )
    try:
        assert (await runner.run(parts[0], parts[1])).tool_surface_digest == expected
    finally:
        monkeypatch.undo()
