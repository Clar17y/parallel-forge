import asyncio
import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from forge.agents.antigravity_runtime import AntigravityGateway, AntigravityInstallation
from forge.agents.client_process import ClientProcessSupervisor
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInterrupted,
)
from forge.domain.tool import ToolName
from test_codex_gateway import _Broker
from test_gemini_gateway import _google_request, _Lifecycle


def request():
    value = _google_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    route = replace(
        value.task.route.effective, client="antigravity_cli", model="gemini-3.8-flash-medium"
    )
    binding = replace(value.task.route, requested=route, effective=route)
    return replace(
        value,
        task=replace(value.task, route=binding),
        envelope=replace(
            value.envelope,
            routes=tuple(
                (purpose, binding if purpose is value.task.purpose else existing)
                for purpose, existing in value.envelope.routes
            ),
        ),
    )


def gateway(tmp_path, scenario, *, broker=None, duration=5):
    executable = Path(sys.executable).resolve()
    installation = AntigravityInstallation(
        executable=str(executable),
        cwd=str(tmp_path),
        home=str(tmp_path),
        model="gemini-3.8-flash-medium",
        effort="medium",
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
        script=(str(Path(__file__).with_name("antigravity_runtime_peer.py")), scenario),
        duration_seconds=duration,
    )
    lifecycle = _Lifecycle()
    return AntigravityGateway(
        installation, broker=broker or _Broker(), lifecycle=lifecycle
    ), lifecycle


@pytest.fixture
def launch_directories(monkeypatch):
    paths = []
    original_start = ClientProcessSupervisor.start

    async def capture_start(self, spec, **kwargs):
        paths.append(Path(spec.cwd))
        return await original_start(self, spec, **kwargs)

    monkeypatch.setattr(ClientProcessSupervisor, "start", capture_start)
    return paths


@pytest.mark.parametrize("scenario", ["success", "optional_startup"])
async def test_antigravity_uses_real_mcp_and_supervised_stream_without_capability_evidence(
    tmp_path, launch_directories, scenario
):
    broker = _Broker()
    runtime, lifecycle = gateway(tmp_path, scenario, broker=broker)
    result = await runtime.execute(request())
    assert result.failure is None, [value.stderr for value in lifecycle.results]
    assert result.decision.summary == "done"
    assert len(broker.calls) == 1 and broker.revoked
    assert result.telemetry.input_tokens == 13 and result.telemetry.output_tokens == 5
    assert result.launch_proof.stop_confirmed
    assert not launch_directories[0].exists()


async def test_runtime_uses_review_mode_with_only_its_forge_server_allowed(tmp_path, monkeypatch):
    observed = []
    original_start = ClientProcessSupervisor.start

    async def capture_settings(self, spec, **kwargs):
        settings = json.loads(
            (Path(spec.cwd) / ".gemini/antigravity-cli/settings.json").read_text(encoding="utf-8")
        )
        observed.append(settings)
        return await original_start(self, spec, **kwargs)

    monkeypatch.setattr(ClientProcessSupervisor, "start", capture_settings)
    runtime, _ = gateway(tmp_path, "success")
    value = request()
    result = await runtime.execute(value)
    assert result.failure is None
    assert observed[0]["toolPermission"] == "request-review"
    assert observed[0]["permissions"] == {"allow": [f"mcp(forge_{value.attempt.attempt_id.hex}/*)"]}
    assert observed[0]["useG1Credits"] is False
    assert len(runtime.broker.calls) == 1 and runtime.broker.revoked
    assert result.launch_proof.stop_confirmed


@pytest.mark.parametrize("force_hard_link", [False, True])
async def test_configured_client_state_is_reused_without_copying_global_settings(
    tmp_path, monkeypatch, force_hard_link, launch_directories
):
    login_home = tmp_path / "personal-home"
    client_state = login_home / ".gemini/antigravity-cli/jetski_state.pbtxt"
    client_state.parent.mkdir(parents=True)
    client_state.write_text("providerless login fixture", encoding="utf-8")
    settings = client_state.with_name("settings.json")
    settings.write_text('{"useG1Credits":true,"unrelated":"keep"}', encoding="utf-8")
    if force_hard_link:

        def unavailable_symlink(*args, **kwargs):
            raise OSError("fixture: symlink privilege unavailable")

        monkeypatch.setattr(Path, "symlink_to", unavailable_symlink)

    broker = _Broker()
    runtime, lifecycle = gateway(tmp_path, "login_state", broker=broker)
    runtime.installation = replace(runtime.installation, home=str(login_home))
    value = request()
    # The broker observes the live link while the fake client is running.
    original_call = broker.__class__.__call__
    observed = []

    async def check_link(self, call):
        linked_state = launch_directories[0] / ".gemini/antigravity-cli/jetski_state.pbtxt"
        observed.append(os.path.samefile(linked_state, client_state))
        return await original_call(self, call)

    monkeypatch.setattr(broker.__class__, "__call__", check_link)
    result = await runtime.execute(value)
    assert result.failure is None, [item.stderr for item in lifecycle.results]
    assert observed == [True]
    assert len(broker.calls) == 1 and broker.revoked
    assert result.launch_proof.stop_confirmed
    assert not launch_directories[0].is_relative_to(tmp_path)
    assert not launch_directories[0].exists()
    assert client_state.read_text(encoding="utf-8") == "providerless login fixture"
    assert settings.read_text(encoding="utf-8") == '{"useG1Credits":true,"unrelated":"keep"}'


@pytest.mark.parametrize(
    "workspace_kind", ["configured_workspace", "git_directory", "git_worktree"]
)
async def test_private_home_is_not_created_when_temp_directory_is_inside_a_repository(
    tmp_path, monkeypatch, workspace_kind
):
    repository = tmp_path / "repository"
    temporary_root = repository / "temp"
    temporary_root.mkdir(parents=True)
    cwd = repository
    if workspace_kind != "configured_workspace":
        marker = repository / ".git"
        if workspace_kind == "git_directory":
            marker.mkdir()
        else:
            marker.write_text("gitdir: fixture", encoding="utf-8")
        cwd = tmp_path / "configured-workspace"
        cwd.mkdir()
    monkeypatch.setattr(
        "forge.agents.antigravity_runtime.tempfile.gettempdir", lambda: str(temporary_root)
    )
    runtime, _ = gateway(cwd, "success")
    launches = []

    async def unexpected_start(*args, **kwargs):
        launches.append(True)
        raise AssertionError("a repository must not contain the private home")

    monkeypatch.setattr(runtime.supervisor, "start", unexpected_start)
    result = await runtime.execute(request())
    assert result.failure is SubscriptionFailure.PROTOCOL
    assert launches == []
    assert list(temporary_root.iterdir()) == []


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("bad_usage", SubscriptionFailure.PROTOCOL),
        ("429", SubscriptionFailure.THROTTLED),
        ("401", SubscriptionFailure.AUTHENTICATION),
        ("provider_cancel", SubscriptionFailure.INTERRUPTED),
        ("bad_tool", SubscriptionFailure.PROTOCOL),
    ],
)
async def test_antigravity_rejects_bad_data_and_never_invents_exhaustion(
    tmp_path, scenario, expected
):
    runtime, _ = gateway(tmp_path, scenario)
    result = await runtime.execute(request())
    assert result.failure is expected
    assert result.quota_exhaustion is None
    assert result.launch_proof.stop_confirmed


async def test_antigravity_cancellation_settles_the_mcp_child(tmp_path, launch_directories):
    broker = _Broker()
    runtime, _ = gateway(tmp_path, "cancel", broker=broker)
    task = asyncio.create_task(runtime.execute(request()))
    await asyncio.wait_for(broker.called.wait(), 5)
    task.cancel()
    with pytest.raises(SubscriptionInterrupted) as interrupted:
        await task
    assert broker.revoked
    assert interrupted.value.result.launch_proof.stop_confirmed
    assert not launch_directories[0].exists()


async def test_antigravity_deadline_returns_stopped_tree(tmp_path, launch_directories):
    runtime, _ = gateway(tmp_path, "cancel", duration=0.5)
    result = await runtime.execute(request())
    assert result.failure is SubscriptionFailure.DEADLINE
    assert result.launch_proof.stop_confirmed
    assert not launch_directories[0].exists()


async def test_missing_usage_stays_unknown_and_does_not_block_personal_use(tmp_path):
    runtime, _ = gateway(tmp_path, "missing_usage")
    result = await runtime.execute(request())
    assert result.failure is None
    assert result.telemetry.input_tokens is None
    assert result.telemetry.output_tokens is None


async def test_token_budget_failure_retains_actual_usage(tmp_path):
    runtime, _ = gateway(tmp_path, "success")
    value = request()
    value = replace(
        value, task=replace(value.task, budget=replace(value.task.budget, max_input_tokens=10))
    )
    result = await runtime.execute(value)
    assert result.failure is SubscriptionFailure.BUDGET
    assert result.telemetry.input_tokens == 13
    assert result.telemetry.output_tokens == 5
    assert result.launch_proof.stop_confirmed


@pytest.mark.parametrize(
    "scenario,detail",
    [
        ("missing_output", "Antigravity returned invalid structured output"),
        ("invalid_decision", "Antigravity returned an invalid Forge decision"),
    ],
)
async def test_invalid_output_retains_usage_and_explains_the_failure(tmp_path, scenario, detail):
    runtime, _ = gateway(tmp_path, scenario)
    result = await runtime.execute(request())
    assert result.failure is SubscriptionFailure.PROTOCOL
    assert result.failure_detail == detail
    assert result.telemetry.input_tokens == 13 and result.telemetry.output_tokens == 5
    assert result.launch_proof.stop_confirmed


@pytest.mark.parametrize("scenario", ["permission_denied", "permission_denied_with_output"])
async def test_headless_permission_denial_is_not_a_successful_forge_decision(tmp_path, scenario):
    broker = _Broker()
    runtime, _ = gateway(tmp_path, scenario, broker=broker)
    result = await runtime.execute(request())
    assert result.failure is SubscriptionFailure.POLICY_DENIED
    assert result.failure_detail == "Antigravity denied a tool permission in headless mode"
    assert result.decision is None
    assert result.telemetry.input_tokens == 13 and result.telemetry.output_tokens == 5
    assert not broker.calls and broker.revoked
    assert result.launch_proof.stop_confirmed


async def test_malformed_denied_actions_cannot_be_accepted_as_success(tmp_path):
    runtime, _ = gateway(tmp_path, "invalid_denied_actions")
    result = await runtime.execute(request())
    assert result.failure is SubscriptionFailure.PROTOCOL
    assert result.decision is None
    assert result.telemetry.input_tokens == 13 and result.telemetry.output_tokens == 5
    assert result.launch_proof.stop_confirmed


async def test_antigravity_failed_revoke_still_closes_transport_and_stops_child(
    tmp_path, monkeypatch
):
    from forge.agents.local_cli_mcp import LocalCliMcp

    closed = []
    original_close = LocalCliMcp.close

    async def close(mcp):
        await original_close(mcp)
        closed.append(True)

    monkeypatch.setattr(LocalCliMcp, "close", close)

    class FailedRevoke(_Broker):
        async def revoke(self):
            raise RuntimeError("fixture revoke error")

    runtime, _ = gateway(tmp_path, "success", broker=FailedRevoke())
    result = await runtime.execute(request())
    assert result.failure is SubscriptionFailure.UNCERTAIN
    assert result.launch_proof is not None
    assert result.launch_proof.outcome == "stop_uncertain"
    assert closed == [True]
