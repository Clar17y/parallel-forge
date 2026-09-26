from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from forge.agents import codex_gateway
from forge.agents.client_process import (
    ClientProcessError,
    ClientProcessReceipt,
    ClientProcessResult,
    ClientProcessTimeout,
)
from forge.agents.codex_capability_probe import CodexCapabilityProbe, CodexCapabilityProbeError
from forge.agents.codex_conformance import CodexConformanceResult
from forge.agents.codex_gateway import (
    CodexInstallation,
    codex_account_identity,
    codex_isolation_configuration,
)
from forge.agents.codex_verification import (
    CODEX_VERIFIER_ID,
    CODEX_VERIFIER_VERSION,
    required_codex_verification_scopes,
)
from forge.domain.capability_evidence import capability_home_digest
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof


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


class Conformance:
    def __init__(self, result):
        self.result, self.calls = result, 0

    async def run(self, *_):
        self.calls += 1
        return self.result


class WrongScope:
    def evidence_scope(self):
        return object()


def _nested_config(flat):
    result = {}
    for key, value in flat.items():
        target = result
        parts = key.split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    result["mcp_servers"] = {}
    return result


class Session:
    def __init__(self, installation, scope, scenario="success"):
        self.i, self.scope, self.scenario = installation, scope, scenario
        self.sent, self.frames, self.close_calls = [], [], 0
        self.config = codex_isolation_configuration(installation) | {
            "model_catalog_json": "catalog.json"
        }

    def pinned_path(self, placeholder):
        assert placeholder == codex_gateway.CODEX_MODEL_CATALOG_ARGUMENT
        return "catalog.json"

    async def send(self, value):
        self.sent.append(value)
        method, ident = value.get("method"), value.get("id")
        if ident is None:
            assert method == "initialized"
            return
        if method == "initialize":
            version = "0.153.4" if self.scenario == "wrong_version" else "0.154.0"
            result = {} if self.scenario == "missing_version" else {"userAgent": f"codex/{version}"}
        elif method == "account/read":
            account = {"type": "chatgpt", "email": "codex@example.invalid"}
            if self.scenario == "signed_out":
                account = None
            elif self.scenario == "api_key":
                account = {"type": "apiKey"}
            elif self.scenario == "unknown_account":
                account = {"type": "unknown"}
            elif self.scenario == "account_mismatch":
                account["email"] = "other@example.invalid"
            result = {"account": account}
        elif method == "model/list":
            efforts = (
                [] if self.scenario == "missing_effort" else [{"reasoningEffort": self.i.effort}]
            )
            data = (
                []
                if self.scenario == "missing_model"
                else [{"id": self.i.model, "supportedReasoningEfforts": efforts}]
            )
            result = {"data": data}
        elif method == "config/read":
            config = _nested_config(self.config)
            if self.scenario == "config_drift":
                config["sandbox_mode"] = "danger-full-access"
            result = {"config": config}
        elif method == "thread/start":
            p = value["params"]
            assert p["model"] == self.i.model and p["allowProviderModelFallback"] is False
            assert p["dynamicTools"] == codex_gateway.codex_dynamic_tools(self.scope.tool_surface)
            result = {
                "thread": {"id": "thread-1"},
                "model": "wrong" if self.scenario == "wrong_thread_model" else self.i.model,
            }
        elif method == "turn/start":
            p = value["params"]
            assert (p["model"], p["effort"]) == (self.i.model, self.i.effort)
            assert p["outputSchema"] == {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            }
            result = {"turn": {"id": "turn-1"}}
            self.frames.append({"id": ident, "result": result})
            self.frames += self.completion(p["threadId"])
            return
        else:
            raise AssertionError(f"unexpected method {method}")
        self.frames.append({"id": ident, "result": result})

    def completion(self, thread):
        marker = {
            "missing_marker": None,
            "malformed_marker": "{",
            "wrong_marker": '{"ok":false}',
        }.get(self.scenario, '{"ok":true}')
        frames = []
        if self.scenario == "unexpected_tool":
            frames.append(
                {"method": "item/tool/call", "params": {"threadId": thread, "turnId": "turn-1"}}
            )
        elif marker is not None:
            frames.append(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "foreign" if self.scenario == "foreign_thread" else thread,
                        "turnId": "foreign" if self.scenario == "foreign_turn" else "turn-1",
                        "item": {"type": "agentMessage", "text": marker},
                    },
                }
            )
        frames.append(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread,
                    "turn": {
                        "id": "turn-1",
                        "status": "failed" if self.scenario == "failed_terminal" else "completed",
                        "error": {"message": "secret provider detail"}
                        if self.scenario == "failed_terminal"
                        else None,
                    },
                },
            }
        )
        return frames

    async def receive(self):
        if self.scenario == "timeout" and len(self.sent) == 1:
            raise ClientProcessTimeout("raw")
        if self.scenario == "cancel" and len(self.sent) == 1:
            raise asyncio.CancelledError
        if self.scenario == "crash" and len(self.sent) == 1:
            raise ClientProcessError("raw secret")
        if not self.frames:
            raise ClientProcessError("missing")
        return self.frames.pop(0)

    async def close(self, *, completed):
        self.close_calls += 1
        if self.scenario == "post_drift":
            Path(self.i.executable).write_bytes(b"drifted")
        certain = self.scenario != "uncertain_stop"
        return ClientProcessResult(
            ClientProcessReceipt("live", 124, "live-process", 1.0),
            0 if certain else None,
            (),
            0,
            "",
            0,
            False,
            False,
            "completed" if certain else "stop_uncertain",
            certain,
        )


class Supervisor:
    def __init__(self, installation, scope, scenario="success"):
        self.i, self.scope, self.scenario, self.specs, self.session = (
            installation,
            scope,
            scenario,
            [],
            None,
        )

    async def start(self, spec):
        self.specs.append(spec)
        self.session = Session(self.i, self.scope, self.scenario)
        return self.session


@pytest.fixture
def parts(tmp_path):
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fake-0.154.0")
    home = tmp_path / "home"
    home.mkdir()
    scope = required_codex_verification_scopes()[0]
    i = CodexInstallation(
        str(executable),
        str(tmp_path),
        scope.model,
        scope.effort,
        str(home),
        codex_account_identity("codex@example.invalid"),
        hashlib.sha256(executable.read_bytes()).hexdigest(),
        client_version="0.154.0",
        duration_seconds=1,
    )
    result = CodexConformanceResult(
        scope,
        i.client_version,
        i.executable_digest,
        capability_home_digest(i.client_home),
        i.account,
        True,
        True,
        True,
        ("all",),
        True,
        True,
        True,
        False,
        _proof(),
    )
    return i, scope, result


def make_probe(parts, scenario="success", offline=None):
    i, scope, valid = parts
    supervisor = Supervisor(i, scope.evidence_scope(), scenario)
    conformance = Conformance(valid if offline is None else offline)
    return (
        CodexCapabilityProbe(i, scope.evidence_scope(), conformance, supervisor),
        supervisor,
        conformance,
    )


@pytest.mark.asyncio
async def test_success_binds_exact_observations_protocol_and_isolated_environment(parts):
    probe, supervisor, _ = make_probe(parts)
    o = await probe.observe(authorize_provider_contact=True)
    i = parts[0]
    assert (o.client_identity.client_version, o.client_identity.reported_client_version) == (
        "0.154.0",
        "0.154.0",
    )
    assert (o.client_identity.executable_digest, o.client_identity.client_home_digest) == (
        i.executable_digest,
        capability_home_digest(i.client_home),
    )
    assert (o.account_authentication.account, o.account_authentication.account_kind) == (
        i.account,
        "chatgpt",
    )
    assert (o.route_identity.model, o.route_identity.effort, o.route_identity.turn_completed) == (
        i.model,
        i.effort,
        True,
    )
    assert len(o.route_identity.turn_observation_digest) == 64
    assert (
        o.subscription_route_binding.fallback_disabled
        and o.subscription_route_binding.paid_credential_names_scrubbed
    )
    assert o.tool_isolation.tool_surface == tuple(t.value for t in probe.scope.tool_surface)
    assert (
        o.tool_isolation.forbidden_tool_calls == 0 and o.tool_isolation.side_effect_canaries_clear
    )
    assert (o.verifier_id, o.verifier_version) == (CODEX_VERIFIER_ID, CODEX_VERIFIER_VERSION)
    spec = supervisor.specs[0]
    assert set(spec.environment) <= {"CODEX_HOME", "SystemRoot"}
    assert spec.allowed_environment <= {"CODEX_HOME", "SystemRoot"}
    assert spec.environment["CODEX_HOME"] == i.client_home
    assert not {"OPENAI_API_KEY", "OPENAI_AUTH_TOKEN", "PATH"} & set(spec.environment)
    assert supervisor.session.close_calls == 1


@pytest.mark.asyncio
async def test_no_authorization_fails_before_conformance_or_launch(parts):
    probe, supervisor, conformance = make_probe(parts)
    with pytest.raises(CodexCapabilityProbeError, match="^provider_contact_not_authorized$"):
        await probe.observe()
    assert conformance.calls == 0 and not supervisor.specs


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scope", WrongScope()),
        ("client_version", "0.153.4"),
        ("executable_digest", "0" * 64),
        ("client_home_digest", "0" * 64),
        ("account", "0" * 64),
        ("environmentless", False),
        ("configuration_isolated", False),
        ("only_forge_tools_advertised", False),
        ("callback_identity_bound", False),
        ("callback_result_forwarded", False),
        ("side_effects_absent", False),
        ("credentials_sent", True),
        ("terminal_proof", _proof(False)),
    ],
)
@pytest.mark.asyncio
async def test_offline_failure_matrix_never_launches(parts, field, value):
    invalid = replace(parts[2], **{field: value})
    probe, supervisor, _ = make_probe(parts, offline=invalid)
    expected = {
        "client_version": "version_mismatch",
        "executable_digest": "executable_digest_mismatch",
        "account": "account_identity_unbound",
    }.get(field, "isolation_configuration_failed")
    with pytest.raises(CodexCapabilityProbeError, match=f"^{expected}$"):
        await probe.observe(authorize_provider_contact=True)
    assert not supervisor.specs


@pytest.mark.parametrize(
    ("scenario", "error"),
    [
        ("missing_version", "version_mismatch"),
        ("wrong_version", "version_mismatch"),
        ("signed_out", "subscription_authentication_failed"),
        ("api_key", "subscription_authentication_failed"),
        ("unknown_account", "subscription_authentication_failed"),
        ("account_mismatch", "subscription_authentication_failed"),
        ("missing_model", "model_or_effort_unavailable"),
        ("missing_effort", "model_or_effort_unavailable"),
        ("config_drift", "isolation_configuration_failed"),
        ("wrong_thread_model", "model_or_effort_unavailable"),
        ("missing_marker", "offline_conformance_failed"),
        ("malformed_marker", "offline_conformance_failed"),
        ("wrong_marker", "offline_conformance_failed"),
        ("foreign_thread", "probe_protocol_failed"),
        ("foreign_turn", "probe_protocol_failed"),
        ("unexpected_tool", "unexpected_tool_callback"),
        ("failed_terminal", "offline_conformance_failed"),
        ("crash", "probe_protocol_failed"),
        ("timeout", "probe_timeout"),
        ("uncertain_stop", "probe_stop_uncertain"),
        ("post_drift", "executable_digest_mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_live_failures_are_sanitized_and_close_once(parts, scenario, error):
    probe, supervisor, _ = make_probe(parts, scenario)
    with pytest.raises(CodexCapabilityProbeError, match=f"^{error}$") as caught:
        await probe.observe(authorize_provider_contact=True)
    assert "secret" not in str(caught.value)
    assert supervisor.session.close_calls == 1


@pytest.mark.asyncio
async def test_cancellation_propagates_and_closes_once(parts):
    probe, supervisor, _ = make_probe(parts, "cancel")
    with pytest.raises(asyncio.CancelledError):
        await probe.observe(authorize_provider_contact=True)
    assert supervisor.session.close_calls == 1


@pytest.mark.asyncio
async def test_prelaunch_digest_drift_never_launches(parts):
    probe, supervisor, _ = make_probe(parts)
    Path(probe.installation.executable).write_bytes(b"drift")
    with pytest.raises(CodexCapabilityProbeError, match="^executable_digest_mismatch$"):
        await probe.observe(authorize_provider_contact=True)
    assert not supervisor.specs


def test_isolation_policy_digest_is_version_independent():
    before = codex_gateway.CODEX_ISOLATION_POLICY_DIGEST
    assert "client_version" not in dict(codex_gateway._FIXED_ISOLATION_CONFIGURATION)
    assert codex_gateway.CODEX_CLIENT_VERSION != "0.154.0"
    assert codex_gateway.CODEX_ISOLATION_POLICY_DIGEST == before
