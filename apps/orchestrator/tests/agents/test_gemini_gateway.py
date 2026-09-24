import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from capability_support import fake_capability_evidence
from forge.agents.capability_verification import capability_scope
from forge.agents.client_process import ClientProcessSupervisor, ProcessIdentityStatus
from forge.agents.gemini_gateway import GeminiCapabilityReport, GeminiGateway, GeminiInstallation
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInterrupted,
)
from forge.domain.capability_evidence import CapabilityEvidenceScope
from forge.domain.local_cli import LocalCliTrust
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    ReasoningEffort,
    RouteSpec,
    SpecialistPurpose,
)
from forge.domain.tool import ToolName
from test_subscription_protocol import _request

_TEST_EXECUTABLE = Path(sys.executable).resolve(strict=True)
_TEST_EXECUTABLE_DIGEST = hashlib.sha256(_TEST_EXECUTABLE.read_bytes()).hexdigest()


@dataclass
class _Verifier:
    report: GeminiCapabilityReport
    bind_evidence: bool = True

    def verify(
        self, installation: GeminiInstallation, scope: CapabilityEvidenceScope
    ) -> GeminiCapabilityReport:
        if not self.bind_evidence:
            return self.report
        return replace(
            self.report,
            evidence=fake_capability_evidence(
                scope=scope,
                client_version="0.59.0",
                executable_digest=installation.executable_digest,
                client_home=installation.home,
                account=installation.account,
                verifier_id="fake-gemini-conformance",
            ),
        )


def test_capability_requires_exact_pinned_isolated_evidence():
    installation = GeminiInstallation(
        executable=str(Path(__file__).resolve()),
        cwd=str(Path.cwd()),
        home=str(Path.cwd()),
        model="gemini-3.8",
        account="test-account",
        executable_digest="c" * 64,
        effort="high",
    )
    scope = CapabilityEvidenceScope(
        route=RouteSpec(
            provider="google",
            client="gemini_cli",
            model="gemini-3.8",
            effort=ReasoningEffort.HIGH,
            auth_mode=AuthMode.SUBSCRIPTION,
            billing_mode=BillingMode.ALLOWANCE_ONLY,
        ),
        role=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
    )
    denied = GeminiCapabilityReport(
        installed_version="0.59.0",
        client_home=installation.home,
        subscription_auth=True,
        model="gemini-3.8",
        effort="high",
        tools_disabled=True,
        isolated_config=True,
        account=installation.account,
        executable_digest=installation.executable_digest,
    )
    denied = _Verifier(denied).verify(installation, scope)
    assert not denied.admits(installation, scope)
    admitted = GeminiCapabilityReport(
        installed_version="0.59.0",
        client_home=installation.home,
        subscription_auth=True,
        model="gemini-3.8",
        effort="high",
        tools_disabled=True,
        isolated_config=True,
        acp_mcp_supported=True,
        account=installation.account,
        executable_digest=installation.executable_digest,
    )
    assert not admitted.admits(installation, scope)
    admitted = _Verifier(admitted).verify(installation, scope)
    assert admitted.admits(installation, scope)


async def test_supervised_gemini_uses_real_mcp_bridge_and_retains_launch_proof(tmp_path):
    request = _google_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    broker, lifecycle = _Broker(), _Lifecycle()
    result = await _gateway(tmp_path, "tool", broker=broker, lifecycle=lifecycle).execute(request)
    assert result.failure is None, [item.stderr for item in lifecycle.results]
    assert result.decision.summary == "Fake result with preserved work"
    assert (
        len(broker.calls) == 1 and broker.calls[0].name == "repository.read_file" and broker.revoked
    )
    assert result.launch_proof is not None and result.launch_proof.permits_decision
    assert result.telemetry.input_tokens == 10 and result.telemetry.output_tokens == 4
    assert result.telemetry.tool_call_count == 1


@pytest.mark.parametrize("trust", [LocalCliTrust.OPERATOR, LocalCliTrust.VERIFIED])
async def test_launch_rejects_executable_that_no_longer_matches_installation(tmp_path, trust):
    source = _gateway(tmp_path, "tool")
    installation = replace(source._installation, executable_digest="0" * 64)
    verifier = _Verifier(replace(source._verifier.report, executable_digest="0" * 64))
    broker, lifecycle = _Broker(), _Lifecycle()
    gateway = GeminiGateway(installation, verifier, broker=broker, lifecycle=lifecycle, trust=trust)

    result = await gateway.execute(
        _google_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    )

    assert result.failure is SubscriptionFailure.PROTOCOL
    assert result.decision is None and result.launch_proof is None
    assert broker.revoked and broker.calls == []
    assert lifecycle.results == []
    assert not list(tmp_path.glob(".forge-gemini-*"))


async def test_launch_materializes_configuration_before_start_and_cleans_after_stop(tmp_path):
    captured = {}

    class InspectingSupervisor(ClientProcessSupervisor):
        async def start(self, spec, **kwargs):
            captured["spec"] = spec
            settings_path = spec.environment.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH")
            captured["settings"] = (
                None if settings_path is None else json.loads(Path(settings_path).read_text())
            )
            captured["dotenv"] = {
                name: (Path(spec.cwd) / name).read_text()
                for name in (".env", ".gemini/.env")
                if (Path(spec.cwd) / name).exists()
            }
            return await super().start(spec, **kwargs)

    # Parent configuration must not become the official client's launch settings.
    (tmp_path / ".env").write_text("GEMINI_API_KEY=fake-parent-only\n")
    gateway = _gateway(tmp_path, "no_tools")
    gateway._supervisor = InspectingSupervisor()
    result = await gateway.execute(_google_request())
    assert result.failure is None
    settings = captured["settings"]
    assert settings is not None, "Forge must materialize the pinned client's settings"
    assert settings["tools"]["core"] == []
    assert settings["billing"]["overageStrategy"] == "never"
    assert settings["hooksConfig"]["enabled"] is False
    assert settings["skills"]["enabled"] is False
    assert settings["security"]["auth"] == {
        "selectedType": "oauth-personal",
        "enforcedType": "oauth-personal",
        "useExternal": False,
    }
    assert captured["dotenv"] == {".env": "", ".gemini/.env": ""}
    spec = captured["spec"]
    assert spec.environment["GEMINI_CLI_HOME"] == str(tmp_path / "account-home")
    assert "GEMINI_API_KEY" not in spec.environment
    assert "--allowed-mcp-server-names=forge" in spec.argv
    assert "--extensions=none" in spec.argv
    assert result.launch_proof.stop_confirmed
    assert not Path(spec.cwd).exists()
    assert (tmp_path / ".env").read_text() == "GEMINI_API_KEY=fake-parent-only\n"


async def test_concurrent_gateways_keep_separate_configuration_and_shared_auth(tmp_path):
    specs = []
    ready = asyncio.Event()

    class ConcurrentSupervisor(ClientProcessSupervisor):
        async def start(self, spec, **kwargs):
            specs.append(spec)
            if len(specs) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 3)
            return await super().start(spec, **kwargs)

    first, second = _gateway(tmp_path, "no_tools"), _gateway(tmp_path, "no_tools")
    first._supervisor = second._supervisor = ConcurrentSupervisor()
    results = await asyncio.gather(
        first.execute(_google_request()), second.execute(_google_request())
    )
    assert all(result.failure is None and result.launch_proof.stop_confirmed for result in results)
    assert len({spec.cwd for spec in specs}) == 2
    assert len({spec.environment["GEMINI_CLI_HOME"] for spec in specs}) == 1
    assert all(not Path(spec.cwd).exists() for spec in specs)
    assert (tmp_path / "account-home").is_dir()


def _google_request(*, tools=frozenset()):
    request = _request(tools=tools)
    route = replace(
        request.task.route.effective, provider="google", client="gemini_cli", model="gemini-test"
    )
    binding = replace(request.task.route, requested=route, effective=route)
    return replace(
        request,
        task=replace(request.task, route=binding),
        envelope=replace(
            request.envelope,
            routes=tuple(
                (purpose, binding if purpose is request.task.purpose else current)
                for purpose, current in request.envelope.routes
            ),
        ),
    )


class _Lifecycle:
    def __init__(self):
        self.results = []
        self.intents = []

    async def launch_intent(self, launch_id):
        self.intents.append(launch_id)

    async def started(self, receipt):
        pass

    async def finished(self, receipt, result):
        self.results.append(result)


class _Broker:
    def __init__(self, *, block=False):
        self.calls = []
        self.revoked = False
        self.block = block
        self.called = asyncio.Event()
        self.reconciling = asyncio.Event()
        self.allow_reconciliation = asyncio.Event()
        self.reconciled = False

    async def __call__(self, call):
        assert not self.revoked
        self.calls.append(call)
        self.called.set()
        if self.block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.reconciling.set()
                await self.allow_reconciliation.wait()
                self.reconciled = True
                raise
        return {"status": "succeeded", "operation_id": "fake-controlled-receipt"}

    async def revoke(self):
        self.revoked = True


def _gateway(
    tmp_path,
    scenario,
    *,
    broker=None,
    lifecycle=None,
    report=None,
    duration=10,
    bind_evidence=True,
):
    home = tmp_path / "account-home"
    home.mkdir(exist_ok=True)
    report = report or GeminiCapabilityReport(
        installed_version="0.59.0",
        client_home=str(home.resolve()),
        subscription_auth=True,
        model="gemini-test",
        effort="medium",
        tools_disabled=True,
        isolated_config=True,
        acp_mcp_supported=True,
        account="test-account",
        executable_digest=_TEST_EXECUTABLE_DIGEST,
    )
    return GeminiGateway(
        GeminiInstallation(
            executable=str(_TEST_EXECUTABLE),
            cwd=str(tmp_path),
            home=str(home),
            model="gemini-test",
            account="test-account",
            executable_digest=_TEST_EXECUTABLE_DIGEST,
            effort="medium",
            script=(str(Path(__file__).with_name("gemini_acp_peer.py")), scenario, "--acp"),
            duration_seconds=duration,
        ),
        _Verifier(report, bind_evidence),
        broker=broker,
        lifecycle=lifecycle,
    )


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("rpc_429", SubscriptionFailure.THROTTLED),
        ("rpc_401", SubscriptionFailure.AUTHENTICATION),
        ("rpc_403", SubscriptionFailure.AUTHENTICATION),
        ("rpc_-32000", SubscriptionFailure.AUTHENTICATION),
        ("rpc_503", SubscriptionFailure.OUTAGE),
        ("rpc_-32601", SubscriptionFailure.UNSUPPORTED),
        ("rpc_404", SubscriptionFailure.UNSUPPORTED),
        ("rpc_-32603", SubscriptionFailure.PROTOCOL),
        ("early_eof", SubscriptionFailure.PROTOCOL),
        ("wrong_version", SubscriptionFailure.UNSUPPORTED),
        ("wrong_model", SubscriptionFailure.UNSUPPORTED),
        ("wrong_session", SubscriptionFailure.PROTOCOL),
        ("native_permission", SubscriptionFailure.POLICY_DENIED),
        ("forbidden_tool", SubscriptionFailure.PROTOCOL),
    ],
)
async def test_supervised_failure_revokes_and_stops_without_inventing_quota(
    tmp_path, scenario, expected
):
    broker, lifecycle = _Broker(), _Lifecycle()
    result = await _gateway(tmp_path, scenario, broker=broker, lifecycle=lifecycle).execute(
        _google_request()
    )
    assert result.failure is expected and result.decision is None
    assert result.quota_exhaustion is None and "fake-sensitive-text" not in repr(result)
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    assert broker.revoked and broker.calls == []
    assert len(lifecycle.results) == 1
    assert (
        ClientProcessSupervisor.identity_status(lifecycle.results[0].receipt)
        is ProcessIdentityStatus.GONE
    )
    assert not list(tmp_path.glob(".forge-gemini-*"))


@pytest.mark.parametrize("scenario", ["unknown_usage", "empty_usage"])
async def test_absent_model_measurement_is_unknown_and_never_zero(tmp_path, scenario):
    result = await _gateway(tmp_path, scenario).execute(_google_request())
    assert result.failure is None
    assert result.telemetry.input_tokens is result.telemetry.output_tokens is None
    assert result.telemetry.subscription_allowance_charge is None
    assert result.telemetry.unknown_telemetry_reasons
    assert result.launch_proof.permits_decision


async def test_zero_tool_budget_advertises_no_tools(tmp_path):
    request = _google_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    request = replace(
        request, task=replace(request.task, budget=replace(request.budget, max_tool_calls=0))
    )
    broker = _Broker()
    result = await _gateway(tmp_path, "no_tools", broker=broker).execute(request)
    assert result.failure is None and broker.calls == [] and result.telemetry.tool_call_count == 0


async def test_second_distinct_operation_cannot_exceed_tool_budget(tmp_path):
    request = _google_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    request = replace(
        request, task=replace(request.task, budget=replace(request.budget, max_tool_calls=1))
    )
    broker = _Broker()
    result = await _gateway(tmp_path, "two_tools", broker=broker).execute(request)
    assert result.failure is SubscriptionFailure.PROTOCOL
    assert len(broker.calls) == result.telemetry.tool_call_count == 1
    assert result.launch_proof.stop_confirmed and broker.revoked


async def test_repeated_cancellation_waits_for_admitted_effect_reconciliation(tmp_path):
    broker, lifecycle = _Broker(block=True), _Lifecycle()
    request = _google_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    task = asyncio.create_task(
        _gateway(tmp_path, "tool", broker=broker, lifecycle=lifecycle).execute(request)
    )
    try:
        await asyncio.wait_for(broker.called.wait(), 5)
        task.cancel()
        await asyncio.wait_for(broker.reconciling.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not broker.reconciled and broker.revoked
    finally:
        broker.allow_reconciliation.set()
        task.cancel()
        with pytest.raises(SubscriptionInterrupted) as raised:
            await asyncio.wait_for(task, 5)
    result = raised.value.result
    assert broker.reconciled and result.failure is SubscriptionFailure.INTERRUPTED
    assert result.telemetry.tool_call_count == 1
    assert result.launch_proof.stop_confirmed and lifecycle.results[0].stop_confirmed
    assert not list(tmp_path.glob(".forge-gemini-*"))


async def test_deadline_settles_inflight_callback_and_process_tree(tmp_path):
    broker, lifecycle = _Broker(block=True), _Lifecycle()
    broker.allow_reconciliation.set()
    result = await _gateway(
        tmp_path, "tool", broker=broker, lifecycle=lifecycle, duration=1.5
    ).execute(
        _google_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})),
    )
    assert broker.reconciled and broker.revoked
    assert result.failure is SubscriptionFailure.DEADLINE and result.telemetry.tool_call_count == 1
    assert result.launch_proof.stop_confirmed and lifecycle.results[0].stop_confirmed


async def test_failed_durable_receipt_overrides_success_and_preserves_evidence(tmp_path):
    class UncertainLifecycle(_Lifecycle):
        async def finished(self, receipt, result):
            await super().finished(receipt, result)
            raise RuntimeError("fake database unavailable")

    lifecycle = UncertainLifecycle()
    result = await _gateway(tmp_path, "no_tools", lifecycle=lifecycle).execute(_google_request())
    assert result.failure is SubscriptionFailure.UNCERTAIN and result.decision is None
    assert result.launch_proof.stop_confirmed
    assert (result.telemetry.input_tokens, result.telemetry.output_tokens) == (10, 4)
    assert list(tmp_path.glob(".forge-gemini-*/system.md"))
    assert list(tmp_path.glob(".forge-gemini-*/settings.json"))


def test_gemini_admission_requires_subscription_auth_but_not_removed_billing_field(tmp_path):
    gateway = _gateway(tmp_path, "no_tools")
    scope = capability_scope(_google_request())
    report = gateway._verifier.verify(gateway._installation, scope)
    assert report.admits(gateway._installation, scope)
    assert not replace(report, subscription_auth=False).admits(gateway._installation, scope)


@pytest.mark.parametrize(
    "field",
    [
        "subscription_auth",
        "tools_disabled",
        "isolated_config",
        "acp_mcp_supported",
    ],
)
@pytest.mark.parametrize("value", [False, 1, "true", None])
async def test_unproved_capability_is_rejected_before_launch(tmp_path, field, value):
    lifecycle, broker = _Lifecycle(), _Broker()
    gateway = _gateway(tmp_path, "no_tools", broker=broker, lifecycle=lifecycle)
    report = replace(
        gateway._verifier.verify(gateway._installation, capability_scope(_google_request())),
        **{field: value},
    )
    result = await _gateway(
        tmp_path, "no_tools", broker=broker, lifecycle=lifecycle, report=report
    ).execute(_google_request())
    assert result.failure is SubscriptionFailure.UNAVAILABLE and result.launch_proof is None
    assert broker.revoked and broker.calls == [] and lifecycle.intents == []
    assert list(tmp_path.iterdir()) == [tmp_path / "account-home"]
    assert not list((tmp_path / "account-home").iterdir())


async def test_unknown_telemetry_policy_failure_retains_launch_proof(tmp_path):
    request = _google_request()
    policy = replace(request.budget.unknown_telemetry_policy, allow_unknown_tokens=False)
    request = replace(
        request,
        task=replace(request.task, budget=replace(request.budget, unknown_telemetry_policy=policy)),
    )
    result = await _gateway(tmp_path, "unknown_usage").execute(request)
    assert result.failure is SubscriptionFailure.POLICY_DENIED
    assert result.launch_proof.stop_confirmed and result.telemetry.input_tokens is None


async def test_existing_attempt_directory_is_preserved_and_never_launched(tmp_path):
    request, lifecycle = _google_request(), _Lifecycle()
    retained = tmp_path / f".forge-gemini-{request.attempt.attempt_id}"
    retained.mkdir()
    prompt = retained / "system.md"
    prompt.write_text("Existing attempt evidence", encoding="utf-8")
    result = await _gateway(tmp_path, "no_tools", lifecycle=lifecycle).execute(request)
    assert result.failure is SubscriptionFailure.PROTOCOL and lifecycle.intents == []
    assert prompt.read_text(encoding="utf-8") == "Existing attempt evidence"


async def test_uncertain_start_settlement_keeps_terminal_proof_and_attempt_file(tmp_path):
    class UncertainStart(_Lifecycle):
        async def started(self, receipt):
            raise RuntimeError("fake start persistence failure")

        async def finished(self, receipt, result):
            await super().finished(receipt, result)
            raise RuntimeError("fake receipt persistence failure")

    lifecycle = UncertainStart()
    result = await _gateway(tmp_path, "no_tools", lifecycle=lifecycle).execute(_google_request())
    assert result.failure is SubscriptionFailure.UNCERTAIN
    assert result.launch_proof.stop_confirmed and not result.launch_proof.permits_decision
    assert lifecycle.results[0].stop_confirmed
    assert list(tmp_path.glob(".forge-gemini-*/system.md"))
    assert list(tmp_path.glob(".forge-gemini-*/settings.json"))
