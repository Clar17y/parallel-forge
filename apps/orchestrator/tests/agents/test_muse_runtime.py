import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.agents.muse_runtime import (
    MuseAttemptHome,
    MuseAuthoringHome,
    MuseGateway,
    MuseInstallation,
    MuseRuntimeAdapter,
    resolve_muse_executable,
)
from forge.agents.subscription_protocol import ProtocolError
from forge.application.ports.subscription_gateway import (
    SubscriptionInvocationRequest,
)
from forge.domain.local_cli import LocalCliTrust
from forge.domain.provider_quota import (
    classify_muse_error,
)
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    ReasoningEffort,
    RouteBinding,
    RouteSpec,
)
from forge.domain.subscription_installations import (
    InstallationQuota,
    MuseInstallationSpec,
    quota_route_for,
)
from forge.domain.subscription_readiness import ReadinessReason, ReadinessWarning
from forge.domain.tool import ToolName
from forge.settings import Settings
from forge.worker.subscription_installations import (
    SubscriptionVerifierDependencies,
    load_subscription_installations_diagnostic,
)
from forge.worker.subscription_model_catalog import configured_catalogs
from test_codex_gateway import _Broker
from test_gemini_gateway import _google_request, _Lifecycle

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
ROOT_ID = "9fbd91ca-c205-466a-a420-9df2aa643caa"
CHILD_ID = "01a13165-51c9-7422-a1d8-2b169dad8f73"
SESSION_ID = "01a13165-4e43-7072-8344-a1f2e4584d3b"


def _record(event: str, payload: dict, sequence: int = 1) -> dict:
    return {
        "schema_version": 1,
        "stream": {"kind": "session", "id": SESSION_ID},
        "sequence": sequence,
        "record_type": "event",
        "payload_type": event,
        "payload_schema_version": 1,
        "payload": payload,
    }


def _linked(command_id: str = ROOT_ID) -> dict:
    return _record(
        "session.run.linked",
        {
            "kind": "session_run_linked",
            "command_id": command_id,
            "run_stream": {"kind": "run", "id": command_id},
        },
    )


def _terminal(
    state: str = "completed",
    text: str = json.dumps({
        "decision": {
            "kind": "handoff",
            "status": "blocked",
            "summary": "done",
            "candidate_commit": None,
            "candidate_tree_digest": "",
            "changed_paths": [],
            "check_results": [],
            "evidence_receipt_ids": [],
            "residual_concerns": [],
            "scope_request_paths": [],
        }
    }),
    reason: str | None = None,
    run_id: str = ROOT_ID,
    session_id: str = SESSION_ID,
) -> dict:
    rec = _record(
        "run.terminal." + state,
        {
            "kind": "run_terminal",
            "command_id": run_id,
            "run_stream": {"kind": "run", "id": run_id},
            "terminal": state,
            "text": text,
            "reason": reason,
        },
        2,
    )
    rec["stream"]["id"] = session_id
    return rec


def _muse_request(
    model: str = "muse-wonder-1.0",
    effort: ReasoningEffort = ReasoningEffort.MEDIUM,
    tools: frozenset = frozenset(),
) -> SubscriptionInvocationRequest:
    base = _google_request(tools=tools)
    route = RouteSpec(
        provider="meta",
        client="muse_code",
        model=model,
        effort=effort,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    binding = RouteBinding(requested=route, effective=route)
    from dataclasses import replace

    return replace(
        base,
        task=replace(base.task, route=binding),
        envelope=replace(
            base.envelope,
            routes=tuple(
                (purpose, binding if purpose is base.task.purpose else existing)
                for purpose, existing in base.envelope.routes
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Invariant 1: Installation admission & catalog bindings
# ---------------------------------------------------------------------------


def test_muse_installation_spec_and_quota_route() -> None:
    digest = "a" * 64
    account = "b" * 64
    spec = MuseInstallationSpec(
        client="muse_code",
        executable="C:/Programs/muse/muse.cmd",
        cwd="C:/work/repo",
        home="C:/Users/test",
        model="muse-wonder-1.0",
        effort="medium",
        account=account,
        executable_digest=digest,
        client_version="1.4.7",
        quota=InstallationQuota(account="personal", pool="allowance"),
    )
    assert spec.client == "muse_code"
    assert spec.max_model_steps == 100
    configured = spec.model_copy(update={"max_model_steps": 250})
    assert MuseInstallationSpec.model_validate(configured.model_dump()).max_model_steps == 250
    for invalid in (0, -1, True, "250"):
        with pytest.raises(ValueError):
            MuseInstallationSpec.model_validate({**spec.model_dump(), "max_model_steps": invalid})
    quota_route = quota_route_for(spec)
    assert quota_route.provider == "meta"
    assert quota_route.client == "muse_code"
    assert quota_route.account == "personal"
    assert quota_route.pool == "allowance"
    assert quota_route.model == "muse-wonder-1.0"


def test_muse_route_effort_is_exact_and_model_is_bound(tmp_path: Path) -> None:
    from forge.agents.muse_runtime import muse_launch_spec
    fake = tmp_path / "client.exe"
    fake.write_bytes(b"dummy")
    common = {
        "executable": str(fake.resolve()), "cwd": str(tmp_path.resolve()),
        "home": str(tmp_path.resolve()), "model": "official-custom-model",
        "executable_digest": hashlib.sha256(fake.read_bytes()).hexdigest(),
    }
    installation = MuseInstallation(**common, effort="maximum")
    assert MuseRuntimeAdapter(installation).route.effort is ReasoningEffort.MAXIMUM
    home = MuseAuthoringHome(installation, uuid4())
    home.prepare("prompt", {"type": "object"})
    try:
        spec = muse_launch_spec(
            installation, home, mcp_name="forge",
            mcp_server={"transport": "stdio", "command": "test", "args": [], "env": {}},
            tools=("read_file",), duration=1,
        )
        assert spec.argv[spec.argv.index("--reasoning-effort") + 1] == "max"
        assert spec.argv[spec.argv.index("--model") + 1] == "official-custom-model"
        assert spec.argv[spec.argv.index("--workspace") + 1] == str(home.path) == str(spec.cwd)
        assert "--trust-workspace" in spec.argv
        assert installation.cwd != str(spec.cwd)
        assert "mcp__forge__read_file" in " ".join(spec.argv)
        assert spec.argv[spec.argv.index("--approval-mode") + 1] == "on-request"
        assert spec.argv[spec.argv.index("--approval-judge") + 1] == "off"
        hooks = json.loads(next(value.removeprefix("hooks=") for value in spec.argv if value.startswith("hooks=")))
        assert hooks["PermissionRequest"][0]["matcher"] == "^(?:mcp__forge__read_file)$"
        policy = json.loads((home.path / "approval-policy.json").read_text(encoding="utf-8"))
        assert policy["tools"] == ["mcp__forge__read_file"]
        assert policy["cwd"] == str(spec.cwd) == str(home.path)
        assert policy["cwd"] != installation.cwd
    finally:
        home.cleanup()
    empty_home = MuseAuthoringHome(installation, uuid4())
    empty_home.prepare("prompt", {"type": "object"})
    try:
        empty_spec = muse_launch_spec(
            installation, empty_home, mcp_name="forge",
            mcp_server={"transport": "stdio", "command": "test", "args": [], "env": {}},
            tools=(), duration=1,
        )
        assert "hooks={}" in empty_spec.argv
        assert not (empty_home.path / "approval-policy.json").exists()
        assert empty_spec.argv[empty_spec.argv.index("--workspace") + 1] == str(empty_home.path)
        assert "--trust-workspace" in empty_spec.argv
    finally:
        empty_home.cleanup()
    with pytest.raises(ValueError, match="invalid Muse installation"):
        MuseInstallation(**{**common, "model": "cli-default"}, effort="medium")
    with pytest.raises(ValueError, match="invalid Muse installation"):
        MuseInstallation(**common, effort="minimal")


def test_muse_toolset_uses_the_names_muse_exposes_for_dotted_mcp_tools(tmp_path: Path) -> None:
    """Muse 1.4.7 maps every character outside [A-Za-z0-9_] in an MCP tool name to `_`."""
    from forge.agents.muse_runtime import muse_launch_spec

    fake = tmp_path / "client.exe"
    fake.write_bytes(b"dummy")
    installation = MuseInstallation(
        executable=str(fake.resolve()), cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()), model="muse-wonder-1.0", effort="medium",
        executable_digest=hashlib.sha256(fake.read_bytes()).hexdigest(),
    )
    home = MuseAuthoringHome(installation, uuid4())
    home.prepare("prompt", {"type": "object"})
    try:
        spec = muse_launch_spec(
            installation, home, mcp_name="forge",
            mcp_server={"transport": "stdio", "command": "test", "args": [], "env": {}},
            tools=("repository.list_files", "repository.read_file", "validation-results.read"),
            duration=1,
        )
        toolset = json.loads(
            next(v.removeprefix("run.toolset=") for v in spec.argv if v.startswith("run.toolset="))
        )
        assert toolset == [
            "mcp__forge__repository_list_files",
            "mcp__forge__repository_read_file",
            "mcp__forge__validation_results_read",
        ]
        policy = json.loads((home.path / "approval-policy.json").read_text(encoding="utf-8"))
        assert policy["tools"] == toolset
    finally:
        home.cleanup()


@pytest.mark.parametrize("tools", [(), ("read_file",)])
def test_muse_launch_rejects_unprepared_or_replaced_private_home(
    tmp_path: Path, tools: tuple[str, ...],
) -> None:
    from forge.agents.muse_runtime import muse_launch_spec

    fake = tmp_path / "client.exe"
    fake.write_bytes(b"dummy")
    installation = MuseInstallation(
        executable=str(fake.resolve()), cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()), model="muse-wonder-1.0", effort="medium",
        executable_digest=hashlib.sha256(fake.read_bytes()).hexdigest(),
    )
    server = {"transport": "stdio", "command": "test", "args": [], "env": {}}
    unprepared = MuseAuthoringHome(installation, uuid4())
    with pytest.raises(OSError):
        muse_launch_spec(
            installation, unprepared, mcp_name="forge", mcp_server=server,
            tools=tools, duration=1,
        )
    home = MuseAuthoringHome(installation, uuid4())
    home.prepare("prompt", {"type": "object"})
    saved = tmp_path / "saved-private-home"
    home.path.rename(saved)
    home.path.mkdir()
    try:
        with pytest.raises(OSError, match="private home changed"):
            muse_launch_spec(
                installation, home, mcp_name="forge", mcp_server=server,
                tools=tools, duration=1,
            )
        assert not (home.path / "approval-policy.json").exists()
    finally:
        home.path.rmdir()
        saved.rename(home.path)
        home.cleanup()


def test_manifest_loads_muse_under_operator_trust_and_unsupported_in_verified(tmp_path: Path) -> None:
    fake_exe = tmp_path / "muse.cmd"
    fake_exe.write_text("@echo off\n", encoding="utf-8")
    if os.name == "nt":
        (tmp_path / ".muse-version").write_text("1.4.7-R6101.1", encoding="ascii")
        native = tmp_path / "muse-bin-1.4.7-R6101.1.exe"
        native.write_bytes(b"test native Muse")
    else:
        native = fake_exe
    actual_digest = hashlib.sha256(native.read_bytes()).hexdigest()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    manifest_file = tmp_path / "manifest.json"
    manifest_data = {
        "version": 2,
        "installations": [
            {
                "client": "muse_code",
                "executable": str(fake_exe.resolve()),
                "cwd": str(cwd.resolve()),
                "home": str(home.resolve()),
                "model": "muse-wonder-1.0",
                "effort": "medium",
                "account": "a" * 64,
                "executable_digest": actual_digest,
                "client_version": "1.4.7",
                "quota": {"account": "meta-sub", "pool": "allowance"},
            }
        ],
    }
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    # In operator mode: admitted with operator_trusted
    operator_settings = Settings(
        subscription_installations_path=manifest_file,
        subscription_client_trust=LocalCliTrust.OPERATOR,
        _env_file=None,
    )
    verifiers = SubscriptionVerifierDependencies()
    diag = load_subscription_installations_diagnostic(operator_settings, verifiers)
    assert len(diag.adapters) == 1
    assert isinstance(diag.adapters[0], MuseRuntimeAdapter)
    assert diag.adapters[0].route.provider == "meta"
    assert diag.adapters[0].route.client == "muse_code"
    assert diag.adapters[0].installation.max_model_steps == 100
    manifest_data["installations"][0]["max_model_steps"] = 250
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")
    overridden = load_subscription_installations_diagnostic(operator_settings, verifiers)
    assert overridden.adapters[0].installation.max_model_steps == 250
    assert diag.readiness[0].admitted is True
    assert diag.readiness[0].reason is ReadinessReason.OPERATOR_TRUSTED
    assert ReadinessWarning.OPERATOR_TRUSTED in diag.readiness[0].warnings

    # In verified mode: unsupported (no verifier)
    verified_settings = Settings(
        subscription_installations_path=manifest_file,
        subscription_client_trust=LocalCliTrust.VERIFIED,
        _env_file=None,
    )
    diag_v = load_subscription_installations_diagnostic(verified_settings, verifiers)
    assert len(diag_v.adapters) == 0
    assert diag_v.readiness[0].admitted is False
    assert diag_v.readiness[0].reason is ReadinessReason.PROVIDER_UNSUPPORTED


def test_configured_catalogs_includes_meta_muse_code() -> None:
    digest = "a" * 64
    account = "b" * 64
    specs = [
        MuseInstallationSpec(
            client="muse_code",
            executable="C:/Programs/muse/muse.cmd",
            cwd="C:/work/repo",
            home="C:/Users/test",
            model="muse-wonder-1.0",
            effort="medium",
            account=account,
            executable_digest=digest,
            client_version="1.4.7",
            quota=InstallationQuota(account="personal", pool="allowance"),
        ),
        MuseInstallationSpec(
            client="muse_code",
            executable="C:/Programs/muse/muse.cmd",
            cwd="C:/work/repo",
            home="C:/Users/test",
            model="muse-wonder-full-1.0",
            effort="high",
            account=account,
            executable_digest=digest,
            client_version="1.4.7",
            quota=InstallationQuota(account="personal", pool="allowance"),
        ),
    ]
    catalogs = configured_catalogs(specs)
    assert len(catalogs) == 1
    cat = catalogs[0]
    assert cat["provider"] == "meta"
    assert cat["client"] == "muse_code"
    assert cat["source"] == "configured"
    models = {m["id"]: m["efforts"] for m in cat["models"]}
    assert "muse-wonder-1.0" in models
    assert "medium" in models["muse-wonder-1.0"]
    assert "muse-wonder-full-1.0" in models
    assert "high" in models["muse-wonder-full-1.0"]


# ---------------------------------------------------------------------------
# Invariant 2: Quota classification
# ---------------------------------------------------------------------------


def test_classify_muse_error_quota_and_reset() -> None:
    reason_iso = "API error 403: Weekly usage limit reached. Resets at 2026-10-14T00:00:00Z. (quota_exhausted)"
    failure, evidence = classify_muse_error(reason_iso, now=NOW)
    assert failure == "quota"
    assert evidence is not None
    assert evidence.reason == "muse_account_usage_exhausted"
    assert evidence.reset_at == datetime(2026, 10, 14, 0, 0, tzinfo=UTC)

    reason_rel = "API error 403: Weekly usage limit reached. Resets in 2h. (quota_exhausted)"
    failure2, evidence2 = classify_muse_error(reason_rel, now=NOW)
    assert failure2 == "quota"
    assert evidence2 is not None
    assert evidence2.reset_at == NOW + timedelta(hours=2)

    # 429 throttled
    assert classify_muse_error("API error 429: rate limit per minute", now=NOW)[0] == "throttled"
    # 401 auth
    assert classify_muse_error("API error 401: authentication required", now=NOW)[0] == "authentication"
    # 403 other
    assert classify_muse_error("API error 403: permission denied", now=NOW)[0] == "policy_denied"


@pytest.mark.parametrize("reason", [
    "API error 429: rate limit per minute (quota_exhausted)",
    "API error 429: Weekly usage limit reached. Resets in 1h. (quota_exhausted)",
    "API error 403: permission denied (quota_exhausted)",
    "API error 403: Weekly usage limit reached. Resets in 1h. (invalid_request_error)",
    "API error 400: invalid model (invalid_request_error)",
    "API error 401: authentication required (auth_error)",
    "quoted: API error 403: Weekly usage limit reached. Resets in 1h. (quota_exhausted)",
    "API error 403: Usage limit reached. (quota_exhausted)",
    "",
])
def test_muse_quota_classifier_rejects_noncanonical_and_quoted_errors(reason: str) -> None:
    _, evidence = classify_muse_error(reason, now=NOW)
    assert evidence is None


# ---------------------------------------------------------------------------
# Invariant 3: Root terminal matching & rejection of child/foreign terminals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_muse_stream_rejects_child_or_foreign_terminal(tmp_path: Path) -> None:
    fake_exe = tmp_path / "fake.exe"
    fake_exe.write_bytes(b"dummy")
    digest = hashlib.sha256(fake_exe.read_bytes()).hexdigest()
    installation = MuseInstallation(
        executable=str(fake_exe.resolve()),
        cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()),
        model="muse-wonder-1.0",
        effort="medium",
        executable_digest=digest,
    )
    gateway = MuseGateway(installation, broker=_Broker(), lifecycle=_Lifecycle())
    req = _muse_request()

    class FakeSession:
        def __init__(self, frames: list[dict]):
            self._frames = list(frames)

        async def receive(self):
            return self._frames.pop(0) if self._frames else None

        async def send(self, value):
            pass

        async def close(self, **kwargs):
            return SimpleNamespace(
                stop_confirmed=True,
                permits_decision=True,
                receipt=SimpleNamespace(launch_id="l1", pid=1, process_start_token="t1"),
                outcome="completed",
                return_code=0,
                stdout_byte_count=100,
                stderr="",
                stderr_byte_count=0,
                stdout_truncated=False,
                stderr_truncated=False,
            )

    # Foreign command ID on terminal
    child_term_session = FakeSession([_linked(ROOT_ID), _terminal(run_id=CHILD_ID)])
    with pytest.raises(ProtocolError):
        await gateway._exchange(child_term_session, req, SimpleNamespace(receive=lambda s: s.receive()))

    # Foreign session ID on terminal
    diff_session = FakeSession([_linked(ROOT_ID), _terminal(run_id=ROOT_ID, session_id="foreign-session")])
    with pytest.raises(ProtocolError):
        await gateway._exchange(diff_session, req, SimpleNamespace(receive=lambda s: s.receive()))

    # Valid root terminal
    valid_session = FakeSession([_linked(ROOT_ID), _terminal(run_id=ROOT_ID)])
    res, _telem = await gateway._exchange(valid_session, req, SimpleNamespace(receive=lambda s: s.receive()))
    assert res.failure is None
    assert res.decision.summary == "done"


@pytest.mark.asyncio
async def test_muse_failed_terminal_preserves_quota_classification(tmp_path: Path) -> None:
    fake_exe = tmp_path / "fake.exe"
    fake_exe.write_bytes(b"dummy")
    installation = MuseInstallation(
        executable=str(fake_exe.resolve()), cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()), model="muse-wonder-1.0", effort="medium",
        executable_digest=hashlib.sha256(fake_exe.read_bytes()).hexdigest(),
    )
    gateway = MuseGateway(installation, broker=_Broker(), lifecycle=_Lifecycle())

    class FakeSession:
        def __init__(self):
            self.frames = [_linked(), _terminal(state="failed", reason="API error 403: Weekly usage limit reached. (quota_exhausted)")]

        async def receive(self):
            return self.frames.pop(0) if self.frames else None

    result, _ = await gateway._exchange(
        FakeSession(), _muse_request(), SimpleNamespace(receive=lambda s: s.receive())
    )
    from forge.application.ports.subscription_gateway import SubscriptionFailure

    assert result.failure is SubscriptionFailure.QUOTA


@pytest.mark.asyncio
async def test_muse_supervised_process_calls_real_mcp_and_settles(tmp_path: Path) -> None:
    import sys

    peer = tmp_path / "peer.py"
    peer.write_text(
        '''import json, os, socket, sys
options = sys.argv
assert "--output-schema" in options
assert json.load(open(options[options.index("--output-schema") + 1], encoding="utf-8"))
assert "--disable-approval" not in options and "--trust-workspace" in options
assert options[options.index("--workspace") + 1] == os.getcwd()
config = json.loads(next(x.split("=", 1)[1] for x in options if x.startswith("mcp_servers.")))
assert config["transport"] == "stdio"
assert config["env"]["FORGE_GEMINI_BRIDGE_SECRET"] == "${FORGE_GEMINI_BRIDGE_SECRET}"
assert os.environ["HOME"] == os.environ["USERPROFILE"]
with socket.create_connection(("127.0.0.1", int(os.environ["FORGE_GEMINI_BRIDGE_PORT"])), timeout=5) as s:
    s.sendall((os.environ["FORGE_GEMINI_BRIDGE_SECRET"] + "\\n").encode())
    f = s.makefile("rb")
    def rpc(i, method, params):
        s.sendall((json.dumps({"jsonrpc":"2.0","id":i,"method":method,"params":params})+"\\n").encode())
        return json.loads(f.readline())
    assert "result" in rpc(1,"initialize",{"protocolVersion":"2025-03-26"})
    listed = rpc(2,"tools/list",{})["result"]["tools"]
    assert [x["name"] for x in listed] == ["repository.read_file"]
    assert "result" in rpc(3,"tools/call",{"name":"repository.read_file","arguments":{"path":"README.md"}})
for frame in ''' + repr([_linked(), _terminal()]) + ''':
    print(json.dumps(frame), flush=True)
''', encoding="utf-8"
    )
    executable = Path(sys.executable)
    installation = MuseInstallation(
        executable=str(executable), script=(str(peer),),
        cwd=str(tmp_path.resolve()), home=str(tmp_path.resolve()),
        model="muse-wonder-1.0", effort="medium",
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    broker, lifecycle = _Broker(), _Lifecycle()
    request = _muse_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE}))
    result = await MuseGateway(installation, broker=broker, lifecycle=lifecycle).execute(request)
    assert result.failure is None
    assert result.decision.summary == "done"
    assert result.launch_proof is not None and result.launch_proof.permits_decision
    assert result.telemetry.tool_call_count == 1
    assert len(broker.calls) == 1 and broker.revoked
    assert len(lifecycle.results) == 1 and lifecycle.results[0].stop_confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize("frames", [
    [_linked(), _linked(CHILD_ID), _terminal()],
    [_linked(), _terminal(), {**_terminal(), "sequence": 3}],
    [{**_linked(), "schema_version": 2}, _terminal()],
    [_linked(), {**_terminal(), "payload": {**_terminal()["payload"], "run_stream": {"kind": "run", "id": CHILD_ID}}}],
])
async def test_muse_rejects_incoherent_root_stream(frames: list[dict]) -> None:
    from forge.agents.muse_runtime import muse_root_terminal

    class Reader:
        def __init__(self):
            self.frames = list(frames)

        async def receive(self):
            return self.frames.pop(0) if self.frames else None

    reader = Reader()
    with pytest.raises(ProtocolError):
        await muse_root_terminal(reader.receive)


@pytest.mark.asyncio
async def test_muse_cancel_revokes_callbacks_before_settlement(tmp_path: Path) -> None:
    import asyncio
    import sys

    from forge.application.ports.subscription_gateway import (
        SubscriptionFailure,
        SubscriptionInterrupted,
    )

    peer = tmp_path / "slow.py"
    peer.write_text(
        "import json, time\nprint(json.dumps(" + repr(_linked()) + "), flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    executable = Path(sys.executable)
    installation = MuseInstallation(
        executable=str(executable), script=(str(peer),), cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()), model="muse-wonder-1.0", effort="medium",
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    broker, lifecycle = _Broker(), _Lifecycle()
    work = asyncio.create_task(
        MuseGateway(installation, broker=broker, lifecycle=lifecycle).execute(_muse_request())
    )
    for _ in range(100):
        if lifecycle.intents:
            break
        await asyncio.sleep(0.01)
    assert lifecycle.intents
    work.cancel()
    with pytest.raises(SubscriptionInterrupted) as error:
        await work
    assert error.value.result.failure is SubscriptionFailure.INTERRUPTED
    assert broker.revoked
    assert lifecycle.results and lifecycle.results[0].stop_confirmed


@pytest.mark.asyncio
async def test_muse_authoring_real_process_receives_job_and_read_only_mcp(tmp_path: Path) -> None:
    import sys

    from forge.agents.epic_brainstorm_gateway import EpicBrainstormGateway
    from forge.tools.epic_brainstorm import BrainstormReadOnlyTools
    from forge.tools.repository import RepositoryReader

    from apps.orchestrator.tests.epic_brainstorm.test_gateway import MockLifecycle, _make_snapshot

    job = _make_snapshot()
    proposal = {"proposal": {
        "schema_version": 1, "turn_id": str(job.prompt_turn_id),
        "problem": "Clear problem statement", "outcomes": ["Outcome 1"],
        "scope": ["Scope 1"], "exclusions": [], "requirements": ["Requirement 1"],
        "requirement_criteria": [{"requirement": "Requirement 1", "criteria": ["Criterion 1"]}],
        "decisions": [], "assumptions": [], "open_questions": [],
        "resolved_turn_ids": [], "evidence": [],
    }}
    peer = tmp_path / "author.py"
    peer.write_text(
        '''import json, os, socket, sys
options = sys.argv
prompt = open(options[options.index("--prompt-file") + 1], encoding="utf-8").read()
assert "--trust-workspace" in options
assert options[options.index("--workspace") + 1] == os.getcwd()
assert options[options.index("--max-model-steps") + 1] == "250"
assert "Test problem" in prompt and "prompt_turn_id" in prompt
assert json.load(open(options[options.index("--output-schema") + 1], encoding="utf-8"))
with socket.create_connection(("127.0.0.1", int(os.environ["FORGE_GEMINI_BRIDGE_PORT"])), timeout=5) as s:
    s.sendall((os.environ["FORGE_GEMINI_BRIDGE_SECRET"] + "\\n").encode())
    f = s.makefile("rb")
    def rpc(i, method, params):
        s.sendall((json.dumps({"jsonrpc":"2.0","id":i,"method":method,"params":params})+"\\n").encode())
        return json.loads(f.readline())
    assert "result" in rpc(1,"initialize",{"protocolVersion":"2025-03-26"})
    names = [x["name"] for x in rpc(2,"tools/list",{})["result"]["tools"]]
    assert set(names) == {"list_files", "read_file", "read_instructions", "search"}
    assert "result" in rpc(3,"tools/call",{"name":"read_file","arguments":{"path":"README.md"}})
for frame in ''' + repr([_linked(), _terminal(text=json.dumps(proposal))]) + ''':
    print(json.dumps(frame), flush=True)
''', encoding="utf-8"
    )
    executable = Path(sys.executable)
    spec = MuseInstallationSpec(
        client="muse_code", executable=str(executable), cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()), model="muse-wonder-1.0", effort="medium",
        account="dev-account", executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
        client_version="1.4.7", quota=InstallationQuota(account="dev-account", pool="allowance"),
        max_model_steps=250,
    )
    # The installation script is an internal test peer; the production manifest still binds the executable.
    from unittest.mock import patch

    from forge.agents.muse_runtime import MuseInstallation

    original = MuseInstallation
    def test_installation(**kwargs):
        assert kwargs["max_model_steps"] == 250
        return original(**kwargs, script=(str(peer),))

    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))
    async def active() -> bool:
        return False
    lifecycle = MockLifecycle()
    with patch("forge.agents.epic_brainstorm_gateway.MuseInstallation", side_effect=test_installation):
        result = await EpicBrainstormGateway(installation=spec).execute(
            job, (), reader, cancelled=active, lifecycle=lifecycle
        )
    assert result.failure is None, lifecycle.results[0][1].stderr if lifecycle.results else "no receipt"
    assert result.proposal is not None and result.proposal.problem == "Clear problem statement"
    assert result.telemetry is not None and result.telemetry.tool_call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code,expected", [(0, "invalid_output"), (1, "quota_exhausted")])
async def test_muse_authoring_failed_terminal_correlates_process_exit(
    tmp_path: Path, exit_code: int, expected: str,
) -> None:
    import sys
    from unittest.mock import patch

    from forge.agents.epic_brainstorm_gateway import EpicBrainstormGateway
    from forge.tools.epic_brainstorm import BrainstormReadOnlyTools
    from forge.tools.repository import RepositoryReader

    from apps.orchestrator.tests.epic_brainstorm.test_gateway import MockLifecycle, _make_snapshot

    peer = tmp_path / "failed_author.py"
    peer.write_text(
        "import json,sys\nfor frame in "
        + repr([_linked(), _terminal(state="failed", reason="API error 403: Weekly usage limit reached. Resets in 1h. (quota_exhausted)")])
        + ":\n print(json.dumps(frame),flush=True)\nsys.exit(" + str(exit_code) + ")\n",
        encoding="utf-8",
    )
    executable = Path(sys.executable)
    spec = MuseInstallationSpec(
        client="muse_code", executable=str(executable), cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()), model="muse-wonder-1.0", effort="medium",
        account="dev-account", executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
        client_version="1.4.7", quota=InstallationQuota(account="dev-account", pool="allowance"),
        max_model_steps=100,
    )
    original = MuseInstallation
    def test_installation(**kwargs):
        assert kwargs["max_model_steps"] == 100
        return original(**kwargs, script=(str(peer),))
    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))
    async def active() -> bool:
        return False
    with patch("forge.agents.epic_brainstorm_gateway.MuseInstallation", side_effect=test_installation):
        result = await EpicBrainstormGateway(installation=spec).execute(
            _make_snapshot(), (), reader, cancelled=active, lifecycle=MockLifecycle()
        )
    assert result.failure == expected
    assert result.proposal is None
    assert (result.quota_reset_at is not None) is (exit_code == 1)
    assert result.quota_exhausted is (exit_code == 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,exit_code,expected", [
    ("API error 403: Weekly usage limit reached. (quota_exhausted)", 1, "quota"),
    ("API error 401: authentication required", 1, "authentication"),
    ("API error 429: rate limit per minute", 1, "throttled"),
    ("API error 403: Weekly usage limit reached. (quota_exhausted)", 0, "protocol"),
])
async def test_muse_supervised_failure_preserves_only_confirmed_quota(
    tmp_path: Path, reason: str, exit_code: int, expected: str,
) -> None:
    import sys

    from forge.application.ports.subscription_gateway import SubscriptionFailure

    peer = tmp_path / "failed.py"
    peer.write_text(
        "import json, sys\nfor frame in "
        + repr([_linked(), _terminal(state="failed", reason=reason)])
        + ":\n print(json.dumps(frame), flush=True)\nsys.exit(" + str(exit_code) + ")\n",
        encoding="utf-8",
    )
    executable = Path(sys.executable)
    installation = MuseInstallation(
        executable=str(executable), script=(str(peer),), cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()), model="muse-wonder-1.0", effort="medium",
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    result = await MuseGateway(installation, broker=_Broker(), lifecycle=_Lifecycle()).execute(_muse_request())
    assert result.failure is SubscriptionFailure(expected)
    assert (result.quota_exhaustion is not None) is (expected == "quota")
    assert reason not in (result.failure_detail or "")
    assert result.telemetry.input_tokens is None and result.telemetry.output_tokens is None


@pytest.mark.asyncio
async def test_muse_completed_root_with_nonzero_exit_cannot_decide(tmp_path: Path) -> None:
    import sys

    from forge.application.ports.subscription_gateway import SubscriptionFailure

    peer = tmp_path / "bad_exit.py"
    peer.write_text(
        "import json, sys\nfor frame in " + repr([_linked(), _terminal()])
        + ":\n print(json.dumps(frame), flush=True)\nsys.exit(1)\n",
        encoding="utf-8",
    )
    executable = Path(sys.executable)
    installation = MuseInstallation(
        executable=str(executable), script=(str(peer),), cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()), model="muse-wonder-1.0", effort="medium",
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    result = await MuseGateway(installation, broker=_Broker(), lifecycle=_Lifecycle()).execute(_muse_request())
    assert result.failure is SubscriptionFailure.PROTOCOL
    assert result.decision is None
    assert result.launch_proof is not None and result.launch_proof.return_code == 1


@pytest.mark.asyncio
async def test_muse_deadline_settles_without_decision(tmp_path: Path) -> None:
    import sys

    from forge.application.ports.subscription_gateway import SubscriptionFailure

    peer = tmp_path / "slow.py"
    peer.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    executable = Path(sys.executable)
    installation = MuseInstallation(
        executable=str(executable), script=(str(peer),), cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()), model="muse-wonder-1.0", effort="medium",
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
        duration_seconds=0.2,
    )
    broker, lifecycle = _Broker(), _Lifecycle()
    result = await MuseGateway(installation, broker=broker, lifecycle=lifecycle).execute(_muse_request())
    assert result.failure is SubscriptionFailure.DEADLINE
    assert result.decision is None and broker.revoked
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed


@pytest.mark.asyncio
async def test_muse_mcp_rejects_unauthorized_and_late_callbacks() -> None:
    from forge.agents.local_cli_mcp import LocalCliMcp

    broker = _Broker()
    mcp = LocalCliMcp(
        _muse_request(tools=frozenset({ToolName.REPOSITORY_READ_FILE})), broker
    )
    await mcp.start()
    try:
        await mcp._handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        })
        with pytest.raises(ProtocolError):
            await mcp._handle({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "repository.write_file", "arguments": {"path": "x", "content": "x"}},
            })
        assert broker.calls == []
        await mcp.revoke()
        with pytest.raises(ProtocolError):
            await mcp._handle({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "repository.read_file", "arguments": {"path": "README.md"}},
            })
    finally:
        await mcp.close()


@pytest.mark.asyncio
async def test_muse_uncertain_receipt_retains_quota_evidence(tmp_path: Path) -> None:
    import sys

    from forge.application.ports.subscription_gateway import SubscriptionFailure

    peer = tmp_path / "failed.py"
    peer.write_text(
        "import json, sys\nfor frame in " + repr([
            _linked(), _terminal(state="failed", reason="API error 403: Weekly usage limit reached. (quota_exhausted)")
        ]) + ":\n print(json.dumps(frame), flush=True)\nsys.exit(1)\n",
        encoding="utf-8",
    )
    executable = Path(sys.executable)
    installation = MuseInstallation(
        executable=str(executable), script=(str(peer),), cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()), model="muse-wonder-1.0", effort="medium",
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )

    class UncertainLifecycle(_Lifecycle):
        async def finished(self, receipt, result):
            await super().finished(receipt, result)
            raise RuntimeError("receipt store unavailable")

    result = await MuseGateway(installation, broker=_Broker(), lifecycle=UncertainLifecycle()).execute(_muse_request())
    assert result.failure is SubscriptionFailure.UNCERTAIN
    assert result.quota_exhaustion is not None
    assert result.launch_proof is not None


# ---------------------------------------------------------------------------
# Invariant 4: Windows launcher binds installed native executable
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Windows Muse launcher")
def test_resolve_windows_muse_native_binary(tmp_path: Path) -> None:
    launcher = tmp_path / "muse.cmd"
    launcher.write_text("@echo off\n", encoding="ascii")
    marker = tmp_path / ".muse-version"
    marker.write_text("1.4.7-R6101.1\n", encoding="ascii")
    binary = tmp_path / "muse-bin-1.4.7-R6101.1.exe"
    binary.write_bytes(b"native build")
    assert resolve_muse_executable(str(launcher)) == (
        str(binary.resolve()), hashlib.sha256(binary.read_bytes()).hexdigest(),
    )
    assert resolve_muse_executable(str(binary))[0] == str(binary.resolve())
    marker.write_text("1.4.8-R6102.1", encoding="ascii")
    updated = tmp_path / "muse-bin-1.4.8-R6102.1.exe"
    updated.write_bytes(b"updated build")
    assert resolve_muse_executable(str(launcher))[0] == str(updated.resolve())
    for invalid in ("../escape", "bad version", "1.4.7-R6101.1;evil", "x" * 65):
        marker.write_text(invalid, encoding="ascii")
        with pytest.raises(ValueError, match="marker"):
            resolve_muse_executable(str(launcher))
    marker.unlink()
    with pytest.raises(ValueError, match="marker"):
        resolve_muse_executable(str(launcher))
    try:
        marker.symlink_to(tmp_path / "external-version")
    except OSError:
        pass  # Host policy may disallow symlink creation.
    else:
        with pytest.raises(ValueError, match="marker"):
            resolve_muse_executable(str(launcher))
        marker.unlink()
    marker.write_text("1.4.8-R6102.1", encoding="ascii")
    updated.unlink()
    try:
        updated.symlink_to(binary)
    except OSError:
        pass
    else:
        with pytest.raises(ValueError, match="binary"):
            resolve_muse_executable(str(launcher))


def test_windows_muse_launch_binds_actual_native_binary(tmp_path: Path) -> None:
    import shutil

    from forge.agents.muse_runtime import muse_launch_spec

    if os.name != "nt" or not shutil.which("muse"):
        pytest.skip("installed Muse Windows launcher unavailable")
    executable = Path(shutil.which("muse") or "")
    installation = MuseInstallation(
        executable=str(executable.resolve()), cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()), model="muse-wonder-1.0", effort="medium",
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    home = MuseAuthoringHome(installation, uuid4())
    home.prepare("prompt", {"type": "object"})
    try:
        spec = muse_launch_spec(
            installation, home, mcp_name="forge",
            mcp_server={"transport": "stdio", "command": "test", "args": [], "env": {}},
            tools=(), duration=1,
        )
        assert Path(spec.argv[0]).name.startswith("muse-bin-")
        assert spec.executable_digest == hashlib.sha256(Path(spec.argv[0]).read_bytes()).hexdigest()
        assert not spec.pinned_files
    finally:
        home.cleanup()


@pytest.mark.asyncio
async def test_installed_native_muse_offline_echo_through_supervisor(tmp_path: Path) -> None:
    import shutil
    from dataclasses import replace

    from forge.agents.client_process import ClientProcessSupervisor
    from forge.agents.muse_runtime import muse_launch_spec

    executable_name = shutil.which("muse")
    if os.name != "nt" or not executable_name:
        pytest.skip("installed Windows Muse CLI unavailable")
    executable = Path(executable_name)
    installation = MuseInstallation(
        executable=str(executable), cwd=str(tmp_path.resolve()), home=str(tmp_path.resolve()),
        model="muse-wonder-1.0", effort="medium",
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    home = MuseAuthoringHome(installation, uuid4())
    home.prepare("offline echo", {"type": "object"})
    try:
        spec = muse_launch_spec(
            installation, home, mcp_name="forge",
            mcp_server={"transport": "stdio", "command": "unused", "args": [], "env": {}},
            tools=("read_file",), duration=15,
        )
        # Echo is an offline CLI test mode and does not accept model or schema.
        argv = list(spec.argv)
        schema = argv.index("--output-schema")
        del argv[schema:schema + 2]
        for option in ("--model", "--reasoning-effort"):
            index = argv.index(option)
            del argv[index:index + 2]
        mcp_config = next(index for index, value in enumerate(argv) if value.startswith("mcp_servers.forge="))
        del argv[mcp_config - 1:mcp_config + 1]
        toolset = next(index for index, value in enumerate(argv) if value.startswith("run.toolset="))
        argv[toolset] = "run.toolset=[]"
        argv[argv.index("exec") + 1:argv.index("exec") + 1] = ["--provider", "echo"]
        result = await ClientProcessSupervisor().run(replace(spec, argv=tuple(argv)), {})
        assert result.return_code == 0 and result.stop_confirmed, result.stderr
        assert result.frames
    finally:
        home.cleanup()


# ---------------------------------------------------------------------------
# Invariant 5: AttemptHome and AuthoringHome lifecycle outside repo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_muse_attempt_home_lifecycle(tmp_path: Path) -> None:
    fake_exe = tmp_path / "fake.exe"
    fake_exe.write_bytes(b"dummy")
    digest = hashlib.sha256(fake_exe.read_bytes()).hexdigest()
    installation = MuseInstallation(
        executable=str(fake_exe.resolve()),
        cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()),
        model="muse-wonder-1.0",
        effort="medium",
        executable_digest=digest,
    )
    req = _muse_request()
    from forge.agents.local_cli_mcp import LocalCliMcp

    mcp = LocalCliMcp(req, _Broker())
    await mcp.start()
    try:
        home = MuseAttemptHome(installation, mcp)
        env = home.prepare()
        assert home.path.exists()
        assert not (home.path / "muse" / "settings.json").exists()
        assert (home.path / "prompt.txt").exists()
        assert (home.path / "output_schema.json").exists()
        assert env["XDG_CONFIG_HOME"] == str(Path(installation.home) / ".config")
        home.cleanup()
        assert not home.path.exists()
    finally:
        await mcp.close()


def test_muse_authoring_home_lifecycle(tmp_path: Path) -> None:
    fake_exe = tmp_path / "fake.exe"
    fake_exe.write_bytes(b"dummy")
    digest = hashlib.sha256(fake_exe.read_bytes()).hexdigest()
    installation = MuseInstallation(
        executable=str(fake_exe.resolve()),
        cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()),
        model="muse-wonder-1.0",
        effort="medium",
        executable_digest=digest,
    )
    from uuid import uuid4

    attempt_id = uuid4()
    authoring_home = MuseAuthoringHome(installation, attempt_id)
    env = authoring_home.prepare("test prompt", {"type": "object"})
    assert authoring_home.path.exists()
    assert not (authoring_home.path / "muse" / "settings.json").exists()
    assert (authoring_home.path / "prompt.txt").exists()
    assert (authoring_home.path / "output_schema.json").exists()
    assert env["XDG_CONFIG_HOME"] == str(Path(installation.home) / ".config")
    authoring_home.cleanup()
    assert not authoring_home.path.exists()


# ---------------------------------------------------------------------------
# Invariant 6: Authoring exchange with Muse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_muse_authoring_exchange_success(tmp_path: Path) -> None:
    from forge.agents.epic_brainstorm_protocol import AuthoringUsage, muse_exchange

    from apps.orchestrator.tests.epic_brainstorm.test_gateway import _make_snapshot

    job = _make_snapshot()
    proposal_json = {
        "proposal": {
            "schema_version": 1,
            "turn_id": str(job.prompt_turn_id),
            "problem": "Clear problem statement",
            "outcomes": ["Outcome 1"],
            "scope": ["Scope 1"],
            "exclusions": ["Exclusion 1"],
            "requirements": ["Requirement 1"],
            "requirement_criteria": [{"requirement": "Requirement 1", "criteria": ["Criterion 1"]}],
            "decisions": [],
            "assumptions": [],
            "open_questions": [],
            "resolved_turn_ids": [],
            "evidence": [],
        }
    }

    class FakeSession:
        async def close_stdin(self):
            pass

        async def wait_closed(self):
            pass

    class FakeTools:
        def __init__(self):
            self.frames = [
                _linked(ROOT_ID),
                _terminal(run_id=ROOT_ID, text=json.dumps(proposal_json)),
            ]

        async def receive(self, session):
            return self.frames.pop(0) if self.frames else None

        async def record(self, kind, **fields):
            assert kind == "proposal_received" and fields == {}

    proposal = await muse_exchange(
        FakeSession(), job, (), FakeTools(), AuthoringUsage()
    )
    assert proposal.problem == "Clear problem statement"
    assert proposal.outcomes == ("Outcome 1",)


@pytest.mark.asyncio
async def test_muse_authoring_rejects_malformed_proposal() -> None:
    from forge.agents.epic_brainstorm_protocol import AuthoringUsage, muse_exchange

    from apps.orchestrator.tests.epic_brainstorm.test_gateway import _make_snapshot

    class FakeTools:
        def __init__(self):
            self.frames = [_linked(), _terminal(text='{"proposal":{"schema_version":1}}')]

        async def receive(self, session):
            return self.frames.pop(0) if self.frames else None

    with pytest.raises(ProtocolError):
        await muse_exchange(SimpleNamespace(), _make_snapshot(), (), FakeTools(), AuthoringUsage())


# ---------------------------------------------------------------------------
# PR118 repair batch: F1 annotations, F2 authoring failures, F5 cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [
    "API error 401: authentication required",
    "API error 429: rate limit per minute",
    "API error 403: permission denied",
])
async def test_muse_authoring_nonquota_failures_normalize_to_unavailable(reason: str) -> None:
    from forge.agents.epic_brainstorm_protocol import (
        AuthoringProviderFailure,
        AuthoringUsage,
        muse_exchange,
    )

    from apps.orchestrator.tests.epic_brainstorm.test_gateway import _make_snapshot

    class FakeTools:
        def __init__(self):
            self.frames = [_linked(), _terminal(state="failed", reason=reason)]

        async def receive(self, session):
            return self.frames.pop(0) if self.frames else None

    with pytest.raises(AuthoringProviderFailure) as caught:
        await muse_exchange(SimpleNamespace(), _make_snapshot(), (), FakeTools(), AuthoringUsage())
    # The durable authoring failure/diagnostic vocabularies have no
    # authentication/throttled values; only canonical quota stays distinct.
    assert caught.value.failure == "unavailable"
    assert caught.value.quota_reset_at is None


@pytest.mark.asyncio
async def test_muse_authoring_quota_failure_stays_quota_exhausted() -> None:
    from forge.agents.epic_brainstorm_protocol import (
        AuthoringProviderFailure,
        AuthoringUsage,
        muse_exchange,
    )

    from apps.orchestrator.tests.epic_brainstorm.test_gateway import _make_snapshot

    class FakeTools:
        def __init__(self):
            self.frames = [_linked(), _terminal(
                state="failed",
                reason="API error 403: Weekly usage limit reached. Resets in 1h. (quota_exhausted)",
            )]

        async def receive(self, session):
            return self.frames.pop(0) if self.frames else None

    with pytest.raises(AuthoringProviderFailure) as caught:
        await muse_exchange(SimpleNamespace(), _make_snapshot(), (), FakeTools(), AuthoringUsage())
    assert caught.value.failure == "quota_exhausted"
    assert caught.value.quota_reset_at is not None


@pytest.mark.asyncio
async def test_authoring_tools_advertise_truthful_readonly_annotations() -> None:
    from forge.agents.epic_brainstorm_protocol import AuthoringTools, AuthoringUsage
    from forge.tools.epic_brainstorm import BrainstormReadOnlyTools
    from forge.tools.repository import RepositoryReader

    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(Path.cwd().resolve()), secret_paths=()))
    tools = AuthoringTools(reader, 4, AuthoringUsage())
    await tools.mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    listed = await tools.mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert listed is not None
    entries = listed["result"]["tools"]
    assert {entry["name"] for entry in entries} == {
        "list_files", "read_file", "read_instructions", "search",
    }
    assert all(entry["annotations"] == {"readOnlyHint": True} for entry in entries)


def _cleanup_installation(tmp_path: Path) -> MuseInstallation:
    fake_exe = tmp_path / "fake.exe"
    fake_exe.write_bytes(b"dummy")
    return MuseInstallation(
        executable=str(fake_exe.resolve()),
        cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()),
        model="muse-wonder-1.0",
        effort="medium",
        executable_digest=hashlib.sha256(fake_exe.read_bytes()).hexdigest(),
    )


@pytest.mark.parametrize("kind", ["attempt", "authoring"])
def test_muse_home_cleanup_failure_is_visible_and_retriable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    import shutil

    installation = _cleanup_installation(tmp_path)
    if kind == "attempt":
        home: MuseAttemptHome | MuseAuthoringHome = MuseAttemptHome.__new__(MuseAttemptHome)
        home.installation = installation
        home.path = tmp_path / "attempt-home"
        home._identity = None
        home.path.mkdir(mode=0o700)
        info = home.path.lstat()
        home._identity = (info.st_dev, info.st_ino)
    else:
        home = MuseAuthoringHome(installation, uuid4())
        home.path = tmp_path / "authoring-home"
        home.path.mkdir(mode=0o700)
        info = home.path.lstat()
        home._identity = (info.st_dev, info.st_ino)
    (home.path / "prompt.txt").write_text("secret prompt", encoding="utf-8")

    real_rmtree = shutil.rmtree

    def locked(path, *args, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr("forge.agents.muse_runtime.shutil.rmtree", locked)
    with pytest.raises(OSError):
        home.cleanup()
    assert home._identity is not None
    assert (home.path / "prompt.txt").exists()

    monkeypatch.setattr("forge.agents.muse_runtime.shutil.rmtree", real_rmtree)
    home.cleanup()
    assert home._identity is None
    assert not home.path.exists()
    home.cleanup()  # settled cleanup stays an idempotent no-op


def test_muse_home_cleanup_identity_mismatch_and_missing_path(tmp_path: Path) -> None:
    import shutil

    installation = _cleanup_installation(tmp_path)
    home = MuseAuthoringHome(installation, uuid4())
    home.path = tmp_path / "authoring-home"
    home.path.mkdir(mode=0o700)
    info = home.path.lstat()
    home._identity = (info.st_dev, info.st_ino)

    shutil.rmtree(home.path)
    home.path.mkdir(mode=0o700)
    (home.path / "foreign.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(OSError):
        home.cleanup()
    assert home._identity is not None
    assert (home.path / "foreign.txt").exists()

    shutil.rmtree(home.path)
    home.cleanup()  # an already-removed home is a completed deletion
    assert home._identity is None


@pytest.mark.asyncio
async def test_muse_runtime_cleanup_failure_retains_uncertainty_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from forge.application.ports.subscription_gateway import SubscriptionFailure

    peer = tmp_path / "success.py"
    peer.write_text(
        "import json\nfor frame in " + repr([_linked(), _terminal()])
        + ":\n print(json.dumps(frame), flush=True)\n", encoding="utf-8",
    )
    executable = Path(sys.executable)
    installation = MuseInstallation(
        executable=str(executable), script=(str(peer),), cwd=str(tmp_path.resolve()),
        home=str(tmp_path.resolve()), model="muse-wonder-1.0", effort="medium",
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    original_cleanup = MuseAttemptHome.cleanup
    prepared: list[MuseAttemptHome] = []

    def locked(home: MuseAttemptHome) -> None:
        prepared.append(home)
        raise PermissionError("private home locked")

    monkeypatch.setattr(MuseAttemptHome, "cleanup", locked)
    result = await MuseGateway(installation, broker=_Broker(), lifecycle=_Lifecycle()).execute(_muse_request())
    assert result.failure is SubscriptionFailure.UNCERTAIN
    assert result.launch_proof is not None and result.launch_proof.stop_confirmed
    assert prepared and prepared[0]._identity is not None and prepared[0].path.exists()
    original_cleanup(prepared[0])
    assert prepared[0]._identity is None



def test_primary_guidance_says_how_to_start_work_and_what_wait_may_name() -> None:
    """Muse primaries returned `wait` on their own task instead of delegating."""
    from forge.agents.muse_runtime import _decision_guidance
    from forge.domain.subscription import SpecialistPurpose

    primary = _decision_guidance(SpecialistPurpose.PRIMARY)
    assert "do not return a task handoff" in primary
    assert "delegate" in primary
    assert "never your own task_id" in primary
    assert "delegate" not in _decision_guidance(SpecialistPurpose.ROUTINE_IMPLEMENTATION)


def test_muse_decision_schema_is_keyed_by_kind_so_any_branch_can_be_chosen() -> None:
    """Muse emits object keys alphabetically; a leading "kind" left only `wait` reachable."""
    from forge.agents.muse_runtime import _keyed_decision_schema, _unkey_decision

    schema = {
        "type": "object",
        "properties": {
            "decision": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"const": "wait", "type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["reason", "kind"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"const": "delegate", "type": "string"},
                            "children": {"type": "array"},
                            "rationale": {"type": "string"},
                        },
                        "required": ["kind", "rationale", "children"],
                        "additionalProperties": False,
                    },
                ]
            }
        },
        "required": ["decision"],
        "additionalProperties": False,
    }
    keyed = _keyed_decision_schema(schema)
    branches = keyed["properties"]["decision"]["anyOf"]
    assert [list(branch["properties"]) for branch in branches] == [["wait"], ["delegate"]]
    assert all(branch["required"] == list(branch["properties"]) for branch in branches)
    delegate = branches[1]["properties"]["delegate"]
    assert "kind" not in delegate["properties"] and "kind" not in delegate["required"]
    assert schema["properties"]["decision"]["anyOf"][1]["properties"]["kind"]  # input untouched

    assert _unkey_decision({"delegate": {"children": [], "rationale": "r"}}) == {
        "kind": "delegate",
        "children": [],
        "rationale": "r",
    }
    assert _unkey_decision({"kind": "wait", "reason": "x"}) == {"kind": "wait", "reason": "x"}
    assert _unkey_decision({"wait": {"kind": "accept"}}) == {"wait": {"kind": "accept"}}
    assert _unkey_decision({"wait": {}, "delegate": {}}) == {"wait": {}, "delegate": {}}
