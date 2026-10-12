"""Unit tests for EpicBrainstormGateway running under ClientProcessSupervisor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from forge.agents.antigravity_runtime import AntigravityAuthoringHome, AntigravityInstallation
from forge.agents.claude_gateway import ClaudeGateway, ClaudeInstallation
from forge.agents.client_process import (
    ClientLaunchSpec,
    ClientProcessError,
    ClientProcessReceipt,
    ClientProcessResult,
    ClientProcessSession,
    ClientProcessSupervisor,
    ClientSettlementUncertain,
    ProcessIdentityStatus,
)
from forge.agents.codex_gateway import CodexGateway, codex_account_identity
from forge.agents.epic_brainstorm_gateway import EpicBrainstormGateway
from forge.agents.epic_brainstorm_protocol import (
    AuthoringProviderFailure,
    AuthoringTools,
    AuthoringUsage,
    antigravity_exchange,
    claude_exchange,
    gemini_exchange,
)
from forge.agents.subscription_protocol import ProtocolError
from forge.application.ports.epic_decomposition import DecompositionGatewayResult
from forge.domain.epic_brainstorm import (
    AuthoringJobSnapshot,
    BrainstormProposal,
    FrozenBriefContent,
)
from forge.domain.local_cli import LocalCliTrust
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    ReasoningEffort,
    RouteBinding,
    RouteSpec,
    TaskBudget,
)
from forge.domain.subscription_installations import (
    AntigravityInstallationSpec,
    ClaudeInstallationSpec,
    CodexInstallationSpec,
    GeminiInstallationSpec,
)
from forge.domain.task_usage_contract import (
    Observation,
    RouteCohort,
    RouteMember,
    SourceBinding,
    SubjectKey,
    WorkUnitBinding,
)
from forge.tools.epic_brainstorm import BrainstormReadOnlyTools
from forge.tools.repository import RepositoryReader
from forge.worker.epic_decomposition import ValidatedDecompositionGateway


class MockLifecycle:
    def __init__(self, attempt_id=None) -> None:
        self.attempt_id = attempt_id or uuid4()
        self.launch_ids: list[str] = []
        self.receipts: list[object] = []
        self.results: list[object] = []
        self.diagnostics: list[tuple[str, dict[str, object]]] = []

    async def diagnostic(self, kind: str, **fields: object) -> None:
        self.diagnostics.append((kind, fields))

    async def launch_intent(self, launch_id: str) -> None:
        self.launch_ids.append(launch_id)

    async def started(self, receipt) -> None:
        self.receipts.append(receipt)

    async def finished(self, receipt, result) -> None:
        self.results.append((receipt, result))


class _RejectingSupervisor:
    def __init__(self, error: type[Exception] = ClientProcessError) -> None:
        self.error = error
        self.specs: list[ClientLaunchSpec] = []

    async def start(self, spec: ClientLaunchSpec, **_kwargs):
        self.specs.append(spec)
        if self.error is ClientSettlementUncertain:
            receipt = ClientProcessReceipt("scripted-launch", 1, "scripted-token", 0.0)
            result = ClientProcessResult(
                receipt=receipt,
                return_code=None,
                frames=(),
                stdout_byte_count=0,
                stderr="",
                stderr_byte_count=0,
                stdout_truncated=False,
                stderr_truncated=False,
                outcome="unsettled",
                stop_confirmed=False,
            )
            raise ClientSettlementUncertain(result)
        raise self.error("scripted prelaunch failure")


class _CapturingSupervisor(ClientProcessSupervisor):
    def __init__(self, *, sleeping_peer: bool = False) -> None:
        self.sleeping_peer = sleeping_peer
        self.session = None
        self.original_spec: ClientLaunchSpec | None = None
        self.launched_spec: ClientLaunchSpec | None = None

    async def start(self, spec: ClientLaunchSpec, **kwargs):
        self.original_spec = spec
        if self.sleeping_peer:
            spec = replace(spec, argv=(spec.argv[0], "-u", "-c", "import time;time.sleep(30)"))
        self.launched_spec = spec
        self.session = await super().start(spec, **kwargs)
        return self.session


@pytest.mark.asyncio
async def test_authoring_tool_start_is_recorded_before_stuck_read_without_arguments() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    events: list[tuple[str, dict[str, object]]] = []

    class StuckReader:
        async def read_file(self, _path):
            entered.set()
            await release.wait()

    async def record(kind: str, **fields: object) -> None:
        events.append((kind, fields))

    tools = AuthoringTools(StuckReader(), 2, AuthoringUsage(), diagnostic=record)
    operation = asyncio.create_task(tools.call("read-1", "read_file", {"path": "secret-path"}))
    await asyncio.wait_for(entered.wait(), 2)
    assert events == [
        ("tool_started", {"operation": "repository_read", "tool_name": "read_file"})
    ]
    assert "secret-path" not in repr(events)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert len(events) == 1


def _sleeping_codex_installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        CodexGateway, "_command", lambda self: ("-u", "-c", "import time;time.sleep(30)")
    )
    exe = Path(sys.executable)
    return CodexInstallationSpec(
        client="codex_app_server",
        account=codex_account_identity("codex@example.invalid"),
        model="gpt-5.6-luna",
        effort="medium",
        executable=str(exe),
        executable_digest=hashlib.sha256(exe.read_bytes()).hexdigest(),
        client_version="0.153.4",
        cwd=str(tmp_path),
        home=str(tmp_path),
        quota={"account": "dev-account", "pool": "default-pool"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("client", ["codex", "claude", "gemini", "antigravity"])
async def test_authoring_launch_uses_current_executable_digest_under_operator_trust(
    tmp_path: Path, client: str
) -> None:
    import hashlib

    executable = tmp_path / "client.exe"
    executable.write_bytes(b"updated client")
    actual = hashlib.sha256(executable.read_bytes()).hexdigest()
    common = {
        "account": "dev-account",
        "executable": str(executable),
        "executable_digest": "0" * 64,
        "client_version": "1.0.0",
        "cwd": str(tmp_path),
        "home": str(tmp_path),
        "quota": {"account": "dev-account", "pool": "default-pool"},
    }
    installation = {
        "codex": lambda: CodexInstallationSpec(
            client="codex_app_server",
            model="gpt-5.6-luna",
            effort="medium",
            **{**common, "account": codex_account_identity("codex@example.invalid")},
        ),
        "claude": lambda: ClaudeInstallationSpec(
            client="claude_code", model="claude-test", effort="medium", **common
        ),
        "gemini": lambda: GeminiInstallationSpec(
            client="gemini_cli", model="gemini-test", effort="low", **common
        ),
        "antigravity": lambda: AntigravityInstallationSpec(
            client="antigravity_cli", model="gemini-test", effort="low", **common
        ),
    }[client]()
    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))

    async def active() -> bool:
        return False

    supervisor = _RejectingSupervisor()
    gateway = EpicBrainstormGateway(installation=installation, supervisor=supervisor)
    await gateway.execute(_make_snapshot(), (), reader, cancelled=active, lifecycle=MockLifecycle())
    assert len(supervisor.specs) == 1
    assert supervisor.specs[0].executable_digest == actual

    verified = _RejectingSupervisor()
    gateway = EpicBrainstormGateway(
        installation=installation, supervisor=verified, trust=LocalCliTrust.VERIFIED
    )
    result = await gateway.execute(
        _make_snapshot(), (), reader, cancelled=active, lifecycle=MockLifecycle()
    )
    assert result.failure == "unavailable"
    assert verified.specs == []

    executable.unlink()
    missing = _RejectingSupervisor()
    result = await EpicBrainstormGateway(installation=installation, supervisor=missing).execute(
        _make_snapshot(), (), reader, cancelled=active, lifecycle=MockLifecycle()
    )
    assert result.failure == "unavailable"
    assert missing.specs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("client", ["gemini", "antigravity"])
async def test_recovered_retry_uses_new_scratch_and_retains_old_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: str
) -> None:
    import hashlib

    repo = tmp_path / "repo"
    repo.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(scratch))
    executable = tmp_path / "client.exe"
    executable.write_bytes(b"client")
    common = {
        "account": "dev-account",
        "executable": str(executable),
        "executable_digest": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "client_version": "1.0.0",
        "cwd": str(repo),
        "home": str(tmp_path),
        "quota": {"account": "dev-account", "pool": "default-pool"},
        "model": "gemini-test",
        "effort": "low",
    }
    installation = (
        GeminiInstallationSpec(client="gemini_cli", **common)
        if client == "gemini"
        else AntigravityInstallationSpec(client="antigravity_cli", **common)
    )
    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(repo), secret_paths=()))

    async def active() -> bool:
        return False

    job = _make_snapshot()
    first = _RejectingSupervisor(ClientSettlementUncertain)
    first_lifecycle = MockLifecycle()
    result = await EpicBrainstormGateway(installation=installation, supervisor=first).execute(
        job, (), reader, cancelled=active, lifecycle=first_lifecycle
    )
    assert result.failure == "process_unsettled"
    old_scratch = Path(first.specs[0].cwd)
    assert old_scratch.is_dir()
    marker = old_scratch / "retained-evidence.txt"
    marker.write_text("prior attempt", encoding="utf-8")

    second = _RejectingSupervisor()
    second_lifecycle = MockLifecycle()
    result = await EpicBrainstormGateway(installation=installation, supervisor=second).execute(
        job, (), reader, cancelled=active, lifecycle=second_lifecycle
    )
    assert result.failure == "invalid_output"
    assert len(second.specs) == 1
    assert Path(second.specs[0].cwd) != old_scratch
    assert (
        str(second_lifecycle.attempt_id) in second.specs[0].cwd
        or second_lifecycle.attempt_id.hex in second.specs[0].cwd
    )
    assert marker.read_text(encoding="utf-8") == "prior attempt"


def test_antigravity_authoring_rejects_repository_scratch_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    executable = tmp_path / "client.exe"
    executable.write_bytes(b"client")
    installation = AntigravityInstallation(
        executable=str(executable),
        cwd=str(tmp_path),
        home=str(tmp_path),
        model="gemini-test",
        effort="low",
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    home = AntigravityAuthoringHome(installation, uuid4())
    with pytest.raises(OSError, match="outside repositories"):
        home.prepare("forge", {"mcpServers": {}}, "Read-only authoring")
    assert not home.path.exists()


def _make_snapshot(budget: TaskBudget | None = None) -> AuthoringJobSnapshot:
    route = RouteSpec(
        provider="google",
        client="gemini_cli",
        model="scripted-brainstorm",
        effort=ReasoningEffort.LOW,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    return AuthoringJobSnapshot(
        job_id=uuid4(),
        epic_id=uuid4(),
        project_id=uuid4(),
        conversation_id=uuid4(),
        prompt_turn_id=uuid4(),
        budget=budget or TaskBudget(max_provider_attempts=2, max_duration_seconds=10),
        route=RouteBinding(requested=route, effective=route),
        input_brief_revision_id=uuid4(),
        input_brief_digest="0" * 64,
        input_draft_digest="0" * 64,
        draft_content=FrozenBriefContent(
            problem="Test problem", requirements=(), open_questions=()
        ),
        expected_epic_version=1,
        conversation_version=1,
        reservation_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_gateway_cancellation_before_start(tmp_path: Path) -> None:
    gateway = EpicBrainstormGateway()
    job = _make_snapshot()
    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))

    async def is_cancelled() -> bool:
        return True

    lifecycle = MockLifecycle()
    result = await gateway.execute(job, (), reader, cancelled=is_cancelled, lifecycle=lifecycle)
    assert result.failure == "cancelled"
    assert result.proposal is None
    assert len(lifecycle.launch_ids) == 0


@pytest.mark.asyncio
async def test_gateway_unavailable_when_no_spec(tmp_path: Path) -> None:
    gateway = EpicBrainstormGateway(installation=None, launch_spec=None)
    job = _make_snapshot()
    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))

    async def is_cancelled() -> bool:
        return False

    lifecycle = MockLifecycle()
    result = await gateway.execute(job, (), reader, cancelled=is_cancelled, lifecycle=lifecycle)
    assert result.failure == "unavailable"
    assert result.proposal is None


@pytest.mark.asyncio
async def test_gateway_dispatches_tool_calls(tmp_path: Path) -> None:
    sub_file = tmp_path / "hello.txt"
    sub_file.write_text("Hello from repository!\nSecond line.\n", encoding="utf-8")
    readme = tmp_path / "AGENTS.md"
    readme.write_text("# Agent instructions\n", encoding="utf-8")

    script = tmp_path / "tools_client.py"
    script.write_text(
        "import sys, json\n"
        "# 1. Read input job payload\n"
        "sys.stdin.readline()\n"
        "# 2. Request list_files\n"
        "print(json.dumps({'type': 'tool_call', 'id': 'c1', 'tool': 'list_files', 'arguments': {'path': '.'}}), flush=True)\n"
        "res1 = json.loads(sys.stdin.readline())\n"
        "# 3. Request read_file\n"
        "print(json.dumps({'type': 'tool_call', 'id': 'c2', 'tool': 'read_file', 'arguments': {'path': 'hello.txt'}}), flush=True)\n"
        "res2 = json.loads(sys.stdin.readline())\n"
        "# 4. Request search\n"
        "print(json.dumps({'type': 'tool_call', 'id': 'c3', 'tool': 'search', 'arguments': {'literal': 'Hello'}}), flush=True)\n"
        "res3 = json.loads(sys.stdin.readline())\n"
        "# 5. Request read_instructions\n"
        "print(json.dumps({'type': 'tool_call', 'id': 'c4', 'tool': 'read_instructions', 'arguments': {}}), flush=True)\n"
        "res4 = json.loads(sys.stdin.readline())\n"
        "# 6. Return final proposal\n"
        "print(json.dumps({'proposal': {'problem': 'Dispatched tools', 'requirements': [res2['result']['content'][:5]]}}), flush=True)\n",
        encoding="utf-8",
    )

    spec = ClientLaunchSpec(
        argv=(sys.executable, str(script)),
        cwd=str(tmp_path),
        environment={},
        duration_seconds=10,
    )
    gateway = EpicBrainstormGateway(launch_spec=spec)
    job = _make_snapshot()
    repo_reader = RepositoryReader(root=str(tmp_path), secret_paths=())
    tool_counter = {"count": 0}

    async def authorize_tool() -> None:
        tool_counter["count"] += 1

    reader = BrainstormReadOnlyTools(repo_reader, authorize=authorize_tool)

    async def is_cancelled() -> bool:
        return False

    lifecycle = MockLifecycle()
    result = await gateway.execute(job, (), reader, cancelled=is_cancelled, lifecycle=lifecycle)
    assert result.failure is None
    assert result.proposal is not None
    assert result.proposal.problem == "Dispatched tools"
    assert result.proposal.requirements == ("Hello",)
    assert tool_counter["count"] == 4


@pytest.mark.asyncio
async def test_gateway_cancellation_during_receive(tmp_path: Path) -> None:
    script = tmp_path / "hanging_client.py"
    script.write_text(
        "import sys, time\nsys.stdin.readline()\ntime.sleep(10)\n",
        encoding="utf-8",
    )
    spec = ClientLaunchSpec(
        argv=(sys.executable, str(script)),
        cwd=str(tmp_path),
        environment={},
        duration_seconds=10,
    )
    gateway = EpicBrainstormGateway(launch_spec=spec)
    job = _make_snapshot()
    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))

    check_count = 0

    async def is_cancelled() -> bool:
        nonlocal check_count
        check_count += 1
        return check_count >= 2

    lifecycle = MockLifecycle()
    result = await gateway.execute(job, (), reader, cancelled=is_cancelled, lifecycle=lifecycle)
    assert result.failure == "cancelled"
    assert result.proposal is None
    assert len(lifecycle.results) == 1


@pytest.mark.asyncio
async def test_gateway_process_exit_non_zero(tmp_path: Path) -> None:
    script = tmp_path / "failing_client.py"
    script.write_text(
        "import sys\nsys.stdin.readline()\nsys.exit(42)\n",
        encoding="utf-8",
    )
    spec = ClientLaunchSpec(
        argv=(sys.executable, str(script)),
        cwd=str(tmp_path),
        environment={},
        duration_seconds=10,
    )
    gateway = EpicBrainstormGateway(launch_spec=spec)
    job = _make_snapshot()
    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))

    async def is_cancelled() -> bool:
        return False

    lifecycle = MockLifecycle()
    result = await gateway.execute(job, (), reader, cancelled=is_cancelled, lifecycle=lifecycle)
    assert result.failure == "process_failed"
    assert result.proposal is None


@pytest.mark.asyncio
async def test_gateway_invalid_output(tmp_path: Path) -> None:
    script = tmp_path / "bad_json_client.py"
    script.write_text(
        "import sys, json\n"
        "sys.stdin.readline()\n"
        "# Proposal missing required problem field\n"
        "print(json.dumps({'proposal': {'invalid': 123}}), flush=True)\n",
        encoding="utf-8",
    )
    spec = ClientLaunchSpec(
        argv=(sys.executable, str(script)),
        cwd=str(tmp_path),
        environment={},
        duration_seconds=10,
    )
    gateway = EpicBrainstormGateway(launch_spec=spec)
    job = _make_snapshot()
    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))

    async def is_cancelled() -> bool:
        return False

    lifecycle = MockLifecycle()
    result = await gateway.execute(job, (), reader, cancelled=is_cancelled, lifecycle=lifecycle)
    assert result.failure == "invalid_output"
    assert result.proposal is None


@pytest.mark.asyncio
async def test_gateway_explicit_failure_frame(tmp_path: Path) -> None:
    script = tmp_path / "explicit_fail_client.py"
    script.write_text(
        "import sys, json\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'failure': 'quota_exhausted', 'quota_reset_at': '2026-10-05T00:00:00Z', 'telemetry': {'duration_ms': 50}}), flush=True)\n",
        encoding="utf-8",
    )
    spec = ClientLaunchSpec(
        argv=(sys.executable, str(script)),
        cwd=str(tmp_path),
        environment={},
        duration_seconds=10,
    )
    gateway = EpicBrainstormGateway(launch_spec=spec)
    job = _make_snapshot()
    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))

    async def is_cancelled() -> bool:
        return False

    lifecycle = MockLifecycle()
    result = await gateway.execute(job, (), reader, cancelled=is_cancelled, lifecycle=lifecycle)
    assert result.failure == "quota_exhausted"
    assert result.quota_reset_at == "2026-10-05T00:00:00Z"
    assert result.telemetry is not None
    assert result.telemetry.duration_ms == 50


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["success", "nonzero", "natural_exit_race", "unknown_usage", "quota", "quota_diagnostic_raises", "quota_diagnostic_stalls", "quota_diagnostic_cancelled", "long_lived", "retry", "stuck_setup", "tool_reply", "early_callback", "early_final_callback", "early_replay", "early_foreign", "early_invalid_started", "early_invalid_completed", "early_duplicate_final", "early_usage_missing", "early_usage_invalid_count", "early_usage_regressed", "early_error_malformed", "early_error_conflict", "early_valid_prefix", "early_terminal", "early_terminal_then_callback", "early_failed_terminal_then_callback", "closure_timeout", "blocked_settlement_diag"]
)
async def test_configured_codex_uses_app_server_authoring_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    import hashlib

    job = _make_snapshot().model_copy(
        update={
            "route": RouteBinding(
                requested=RouteSpec(
                    provider="openai",
                    client="codex_app_server",
                    model="gpt-5.6-luna",
                    effort=ReasoningEffort.MEDIUM,
                    auth_mode=AuthMode.SUBSCRIPTION,
                    billing_mode=BillingMode.ALLOWANCE_ONLY,
                ),
                effective=RouteSpec(
                    provider="openai",
                    client="codex_app_server",
                    model="gpt-5.6-luna",
                    effort=ReasoningEffort.MEDIUM,
                    auth_mode=AuthMode.SUBSCRIPTION,
                    billing_mode=BillingMode.ALLOWANCE_ONLY,
                ),
            )
        }
    )
    script = tmp_path / "app_server_peer.py"
    (tmp_path / "README.md").write_text("Safe read fixture", encoding="utf-8")
    script.write_text(
        "import json,sys\n"
        "def recv(method):\n"
        " f=json.loads(sys.stdin.readline()); assert f['method']==method, f; return f\n"
        "def send(f): print(json.dumps(f),flush=True)\n"
        "f=recv('initialize'); send({'id':f['id'],'result':{}})\n"
        "recv('initialized')\n"
        "f=recv('account/read'); send({'id':f['id'],'result':{'account':{'type':'chatgpt','email':'codex@example.invalid'}}})\n"
        "f=recv('model/list'); send({'id':f['id'],'result':{'data':[{'id':'gpt-5.6-luna','supportedReasoningEfforts':[{'reasoningEffort':'medium'}]}]}})\n"
        "f=recv('config/read'); "
        + ("import time; time.sleep(30); " if outcome == "stuck_setup" else "")
        + "send({'id':f['id'],'result':{'config':{}}})\n"
        "f=recv('thread/start'); assert f['params']['model']=='gpt-5.6-luna' and len(f['params']['dynamicTools'][0]['tools'])==4; send({'id':f['id'],'result':{'thread':{'id':'thread-1'},'model':'gpt-5.6-luna'}})\n"
        + f"proposal={{'proposal':{{'turn_id':'{job.prompt_turn_id}','problem':'Protocol proposal','requirements':['One'],'requirement_criteria':[{{'requirement':'One','criteria':['Within five minutes']}}]}}}}\n"
        "f=recv('turn/start'); schema=f['params']['outputSchema']; props=schema['properties']['proposal']['properties']; assert f['params']['effort']=='medium' and 'proposal' in schema['properties'] and '$defs' in schema and props['requirement_criteria']['type']=='array'; "
        + (
            "send({'method':'turn/started','params':{'threadId':'thread-1','turn':{'id':'turn-1'}}}); "
            + (
                "send({'method':'item/started','params':{'threadId':'thread-1','turnId':'turn-1','item':{'id':'bad','type':'nativeToolCall'}}}); "
                if outcome == "early_invalid_started" else
                "send({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-1','item':{'id':'bad','type':'agentMessage','text':'{'}}}); "
                if outcome == "early_invalid_completed" else
                "send({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-1','item':{'id':'first','type':'agentMessage','text':json.dumps(proposal)}}}); send({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-1','item':{'id':'second','type':'agentMessage','text':json.dumps(proposal)}}}); "
                if outcome == "early_duplicate_final" else
                "send({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-1','item':{'id':'first','type':'agentMessage','text':json.dumps(proposal)}}}); "
                if outcome == "early_final_callback" else
                "send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread-1','turnId':'turn-1','tokenUsage':{}}}); "
                if outcome == "early_usage_missing" else
                "send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread-1','turnId':'turn-1','tokenUsage':{'total':{'inputTokens':True,'outputTokens':9}}}}); "
                if outcome == "early_usage_invalid_count" else
                "send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread-1','turnId':'turn-1','tokenUsage':{'total':{'inputTokens':7,'outputTokens':9,'cachedInputTokens':0}}}}); send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread-1','turnId':'turn-1','tokenUsage':{'total':{'inputTokens':6,'outputTokens':9,'cachedInputTokens':0}}}}); "
                if outcome == "early_usage_regressed" else
                "send({'method':'error','params':{'threadId':'thread-1','turnId':'turn-1','error':{'message':'bad'},'willRetry':'no'}}); "
                if outcome == "early_error_malformed" else
                "send({'method':'error','params':{'threadId':'thread-1','turnId':'turn-1','error':{'message':'first','codexErrorInfo':'serverOverloaded'},'willRetry':False}}); send({'method':'error','params':{'threadId':'thread-1','turnId':'turn-1','error':{'message':'second','codexErrorInfo':'authenticationFailed'},'willRetry':False}}); "
                if outcome == "early_error_conflict" else
                "send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread-1','turnId':'turn-1','tokenUsage':{'total':{'inputTokens':7,'outputTokens':9,'cachedInputTokens':0}}}}); send({'method':'error','params':{'threadId':'thread-1','turnId':'turn-1','error':{'message':'transient'},'willRetry':True}}); "
                if outcome == "early_valid_prefix" else
                "send({'method':'item/started','params':{'threadId':'thread-1','turnId':'turn-1','item':{'id':'early-item','type':'dynamicToolCall'}}}); "
            )
            + "callback={'id':80,'method':'item/tool/call','params':{'callId':'early-read','threadId':'thread-1','turnId':'"
            + ("foreign" if outcome == "early_foreign" else "turn-1")
            + "','namespace':'forge','tool':'forge_read_file','arguments':{'path':'README.md'}}}; send(callback); "
            + (
                "reply=json.loads(sys.stdin.readline()); assert reply['id']==80; "
                + (
                    "send(callback); reply=json.loads(sys.stdin.readline()); assert reply['id']==80; "
                    if outcome == "early_replay" else ""
                )
                if outcome not in {"early_foreign", "early_invalid_started", "early_invalid_completed", "early_duplicate_final", "early_usage_missing", "early_usage_invalid_count", "early_usage_regressed", "early_error_malformed", "early_error_conflict"} else ""
            )
            if outcome in {"early_callback", "early_final_callback", "early_replay", "early_foreign", "early_invalid_started", "early_invalid_completed", "early_duplicate_final", "early_usage_missing", "early_usage_invalid_count", "early_usage_regressed", "early_error_malformed", "early_error_conflict", "early_valid_prefix"} else ""
        )
        + (
            "send({'method':'turn/started','params':{'threadId':'thread-1','turn':{'id':'turn-1'}}}); send({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-1','item':{'id':'item-1','type':'agentMessage','text':json.dumps(proposal)}}}); send({'method':'turn/completed','params':{'threadId':'thread-1','turn':{'id':'turn-1','status':'completed'}}}); "
            if outcome == "early_terminal" else ""
        )
        + (
            "send({'method':'turn/started','params':{'threadId':'thread-1','turn':{'id':'turn-1'}}}); send({'method':'turn/completed','params':{'threadId':'thread-1','turn':{'id':'turn-1','status':'" + ("failed" if outcome == "early_failed_terminal_then_callback" else "completed") + "'}}}); send({'id':80,'method':'item/tool/call','params':{'callId':'late-read','threadId':'thread-1','turnId':'turn-1','namespace':'forge','tool':'forge_read_file','arguments':{'path':'README.md'}}}); "
            if outcome in {"early_terminal_then_callback", "early_failed_terminal_then_callback"} else ""
        )
        + "send({'id':f['id'],'result':{'turn':{'id':'turn-1'}}})\n"
        + (
            "send({'id':80,'method':'item/tool/call','params':{'callId':'read-1','threadId':'thread-1','turnId':'turn-1','namespace':'forge','tool':'forge_read_file','arguments':{'path':'README.md'}}}); reply=json.loads(sys.stdin.readline()); assert reply['id']==80\n"
            if outcome == "tool_reply" else ""
        )
        + (
            "send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread-1','turnId':'turn-1','tokenUsage':{'total':{'inputTokens':7,'outputTokens':9,'cachedInputTokens':0}}}})\n"
            if outcome in {"success", "nonzero", "retry"}
            else ""
        )
        + (
            "send({'method':'error','params':{'threadId':'thread-1','turnId':'turn-1','error':{'message':'private transient'},'willRetry':True}})\n"
            if outcome == "retry" else ""
        )
        + (
            "send({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-1','item':{'id':'item-1','type':'agentMessage','text':json.dumps(proposal)}}})\n"
            if not outcome.startswith("quota") and outcome not in {"early_final_callback", "early_terminal", "early_terminal_then_callback", "early_failed_terminal_then_callback"}
            else ""
        )
        + (
            "send({'method':'turn/completed','params':{'threadId':'thread-1','turn':{'id':'turn-1','status':'completed'}}})\n"
            if not outcome.startswith("quota") and outcome not in {"early_terminal", "early_terminal_then_callback", "early_failed_terminal_then_callback"}
            else "send({'method':'turn/completed','params':{'threadId':'thread-1','turn':{'id':'turn-1','status':'failed','error':{'codexErrorInfo':'usageLimitExceeded','resetAfterSeconds':120}}}})\n"
            if outcome.startswith("quota") else ""
        )
        + ("sys.exit(7)\n" if outcome in {"nonzero", "natural_exit_race"} else "")
        + ("import time; time.sleep(30)\n" if outcome in {"long_lived", "closure_timeout", "blocked_settlement_diag"} else ""),
        encoding="utf-8",
    )
    monkeypatch.setattr(CodexGateway, "_command", lambda self: ("-u", str(script)))
    if outcome == "closure_timeout":
        async def stuck_completion(_self):
            await asyncio.Event().wait()

        monkeypatch.setattr(ClientProcessSession, "finish_completed", stuck_completion)
    if outcome == "natural_exit_race":
        async def watcher_wins_after_probe(self: ClientProcessSession):
            result = await self.wait_closed()
            # Reproduce the old mutable intent flag being set after the watcher
            # had already committed a natural failed exit.
            self.intentional_completed_stop = True
            return result

        monkeypatch.setattr(ClientProcessSession, "finish_completed", watcher_wins_after_probe)
    exe = Path(sys.executable)
    inst = CodexInstallationSpec(
        client="codex_app_server",
        account=codex_account_identity("codex@example.invalid"),
        model="gpt-5.6-luna",
        effort="medium",
        executable=str(exe),
        executable_digest=hashlib.sha256(exe.read_bytes()).hexdigest(),
        client_version="0.153.4",
        cwd=str(tmp_path),
        home=str(tmp_path),
        quota={"account": "dev-account", "pool": "default-pool"},
    )
    gateway = EpicBrainstormGateway(installation=inst)

    async def is_cancelled() -> bool:
        return False

    lifecycle = MockLifecycle()
    if outcome == "blocked_settlement_diag":
        original_diagnostic = lifecycle.diagnostic

        async def delayed_diagnostic(kind: str, **fields: object) -> None:
            if kind == "settlement_started":
                await asyncio.sleep(1)
            await original_diagnostic(kind, **fields)

        lifecycle.diagnostic = delayed_diagnostic  # type: ignore[method-assign]
    if outcome.startswith("quota_diagnostic_"):
        owner = asyncio.current_task()

        async def broken_diagnostic(kind: str, **fields: object) -> None:
            if kind == "provider_error" and fields.get("failure") == "quota_exhausted":
                if outcome == "quota_diagnostic_raises":
                    raise RuntimeError("diagnostic unavailable")
                if outcome == "quota_diagnostic_cancelled":
                    assert owner is not None
                    asyncio.get_running_loop().call_later(0.02, owner.cancel)
                await asyncio.Event().wait()

        lifecycle.diagnostic = broken_diagnostic  # type: ignore[method-assign]
    began = asyncio.get_running_loop().time()
    result = await gateway.execute(
        job.model_copy(update={"budget": TaskBudget(max_provider_attempts=2, max_duration_seconds=2)})
        if outcome in {"long_lived", "stuck_setup", "closure_timeout", "blocked_settlement_diag", "quota_diagnostic_stalls"}
        else job,
        (),
        BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())),
        cancelled=is_cancelled,
        lifecycle=lifecycle,
    )
    if outcome in {"nonzero", "natural_exit_race"}:
        assert result.failure == "process_failed" and result.proposal is None
    elif outcome in {"early_terminal_then_callback", "early_failed_terminal_then_callback", "early_invalid_started", "early_invalid_completed", "early_duplicate_final", "early_usage_missing", "early_usage_invalid_count", "early_usage_regressed", "early_error_malformed", "early_error_conflict"}:
        assert result.failure == "invalid_output" and result.proposal is None
        assert not any(kind == "tool_started" for kind, _ in lifecycle.diagnostics)
        assert ("provider_wait", {"operation": "tool_reply"}) not in lifecycle.diagnostics
        assert result.telemetry is not None and result.telemetry.tool_call_count == 0
    elif outcome == "stuck_setup":
        assert result.failure == "timeout" and result.proposal is None
        assert ("provider_wait", {"operation": "config_read"}) in lifecycle.diagnostics
        assert ("provider_wait", {"operation": "provider_receive"}) not in lifecycle.diagnostics
    elif outcome == "closure_timeout":
        assert result.failure == "timeout" and result.proposal is None
        assert ("proposal_received", {}) in lifecycle.diagnostics
        assert lifecycle.results[0][1].stop_confirmed
    elif outcome == "early_foreign":
        assert result.failure == "invalid_output" and result.proposal is None
        assert not any(kind == "tool_started" for kind, _ in lifecycle.diagnostics)
    elif outcome.startswith("quota"):
        assert result.failure == (
            "cancelled" if outcome == "quota_diagnostic_cancelled" else "quota_exhausted"
        ) and result.proposal is None
        assert result.quota_reset_at is not None
        assert result.quota_exhausted
        if outcome == "quota_diagnostic_stalls":
            assert asyncio.get_running_loop().time() - began < 0.8
    else:
        assert result.failure is None, [
            (item.outcome, item.return_code) for _, item in lifecycle.results
        ]
        assert result.proposal is not None and result.proposal.problem == "Protocol proposal"
        assert result.proposal.requirement_criteria == {"One": ("Within five minutes",)}
    if outcome == "blocked_settlement_diag":
        assert asyncio.get_running_loop().time() - began < 0.8
    assert result.telemetry is not None
    assert result.telemetry.input_tokens == (7 if outcome in {"success", "nonzero", "retry", "early_valid_prefix", "early_usage_regressed"} else None)
    assert result.telemetry.output_tokens == (9 if outcome in {"success", "nonzero", "retry", "early_valid_prefix", "early_usage_regressed"} else None)
    assert len(lifecycle.receipts) == len(lifecycle.results) == 1
    if outcome == "retry":
        assert ("provider_retry", {"operation": "provider_receive"}) in lifecycle.diagnostics
        assert "private transient" not in repr(lifecycle.diagnostics)
    if outcome == "tool_reply":
        assert ("provider_wait", {"operation": "tool_reply"}) in lifecycle.diagnostics
        assert ("tool_started", {"operation": "repository_read", "tool_name": "read_file"}) in lifecycle.diagnostics
    if outcome in {"early_callback", "early_final_callback", "early_replay", "early_valid_prefix"}:
        assert ("tool_started", {"operation": "repository_read", "tool_name": "read_file"}) in lifecycle.diagnostics
        if outcome == "early_valid_prefix":
            assert result.telemetry.tool_call_count == 1
            assert sum(kind == "provider_retry" for kind, _ in lifecycle.diagnostics) == 1
        if outcome == "early_final_callback":
            assert sum(kind == "proposal_received" for kind, _ in lifecycle.diagnostics) == 1
        if outcome == "early_replay":
            assert result.telemetry.tool_call_count == 1


@pytest.mark.asyncio
async def test_official_cleanup_revoke_failure_still_settles_real_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import forge.agents.epic_brainstorm_gateway as gateway_module

    async def exchange(_session, _gateway, job, *_args):
        return BrainstormProposal(
            turn_id=job.prompt_turn_id, problem="Settled proposal", requirements=("One",)
        )

    async def failed_revoke(_self):
        raise OSError("credential=private-revoke-error")

    monkeypatch.setattr(gateway_module, "codex_exchange", exchange)
    monkeypatch.setattr(AuthoringTools, "revoke", failed_revoke)
    installation = _sleeping_codex_installation(tmp_path, monkeypatch)
    supervisor = _CapturingSupervisor()
    lifecycle = MockLifecycle()
    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))

    async def active() -> bool:
        return False

    try:
        result = await EpicBrainstormGateway(
            installation=installation, supervisor=supervisor
        ).execute(_make_snapshot(), (), reader, cancelled=active, lifecycle=lifecycle)
        assert result.failure == "process_unsettled" and result.proposal is None
        assert len(lifecycle.results) == 1
        assert lifecycle.results[0][1].stop_confirmed is False
        assert (
            ClientProcessSupervisor.identity_status(lifecycle.receipts[0])
            is ProcessIdentityStatus.GONE
        )
        assert "private-revoke-error" not in str(result)
    finally:
        if supervisor.session is not None:
            await supervisor.session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("client", ["gemini", "antigravity"])
@pytest.mark.parametrize("close_error", [OSError, asyncio.CancelledError])
async def test_bridge_cleanup_failure_still_settles_real_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: str, close_error: type[BaseException]
) -> None:
    import forge.agents.epic_brainstorm_gateway as gateway_module

    async def exchange(_session, job, *_args, **_kwargs):
        return BrainstormProposal(
            turn_id=job.prompt_turn_id, problem="Settled bridge proposal", requirements=("One",)
        )

    original_close = AuthoringTools.close
    status_at_close: list[ProcessIdentityStatus] = []

    async def failed_close(self):
        await original_close(self)
        status_at_close.append(ClientProcessSupervisor.identity_status(lifecycle.receipts[0]))
        raise close_error("credential=private-bridge-error")

    monkeypatch.setattr(AuthoringTools, "close", failed_close)
    monkeypatch.setattr(
        gateway_module,
        "gemini_exchange" if client == "gemini" else "antigravity_exchange",
        exchange,
    )
    exe = Path(sys.executable)
    common = {
        "account": "dev-account",
        "model": "gemini-test",
        "effort": "low",
        "executable": str(exe),
        "executable_digest": hashlib.sha256(exe.read_bytes()).hexdigest(),
        "client_version": "1.0.0",
        "cwd": str(tmp_path),
        "home": str(tmp_path),
        "quota": {"account": "dev-account", "pool": "default-pool"},
    }
    installation = (
        GeminiInstallationSpec(client="gemini_cli", **common)
        if client == "gemini"
        else AntigravityInstallationSpec(client="antigravity_cli", **common)
    )
    supervisor = _CapturingSupervisor(sleeping_peer=True)
    lifecycle = MockLifecycle()

    async def active() -> bool:
        return False

    try:
        result = await EpicBrainstormGateway(
            installation=installation, supervisor=supervisor
        ).execute(
            _make_snapshot(),
            (),
            BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())),
            cancelled=active,
            lifecycle=lifecycle,
        )
        assert result.failure == "process_unsettled" and result.proposal is None
        assert status_at_close == [ProcessIdentityStatus.MATCH]
        assert supervisor.original_spec is not None and supervisor.launched_spec is not None
        assert supervisor.original_spec.cwd == supervisor.launched_spec.cwd
        assert supervisor.original_spec.environment == supervisor.launched_spec.environment
        assert (
            supervisor.original_spec.executable_digest == supervisor.launched_spec.executable_digest
        )
        assert len(lifecycle.receipts) == len(lifecycle.results) == 1
        assert (
            ClientProcessSupervisor.identity_status(lifecycle.receipts[0])
            is ProcessIdentityStatus.GONE
        )
        assert "private-bridge-error" not in str(result)
    finally:
        if supervisor.session is not None:
            await supervisor.session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("after_proposal", [False, True])
@pytest.mark.parametrize("blocked_diagnostic", [False, True])
async def test_repeated_cancellation_during_official_cleanup_settles_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_proposal: bool,
    blocked_diagnostic: bool,
) -> None:
    import forge.agents.epic_brainstorm_gateway as gateway_module

    exchanging, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def exchange(_session, _gateway, job, *_args):
        exchanging.set()
        if not after_proposal:
            await asyncio.Event().wait()
        return BrainstormProposal(turn_id=job.prompt_turn_id, problem="Pending settlement")

    async def delayed_revoke(self):
        cleaning.set()
        await release.wait()
        self.closed = True

    monkeypatch.setattr(gateway_module, "codex_exchange", exchange)
    monkeypatch.setattr(AuthoringTools, "revoke", delayed_revoke)
    installation = _sleeping_codex_installation(tmp_path, monkeypatch)
    supervisor = _CapturingSupervisor()
    lifecycle = MockLifecycle()
    if blocked_diagnostic:
        normal_diagnostic = lifecycle.diagnostic

        async def delayed_diagnostic(kind: str, **fields: object) -> None:
            if kind == "settlement_started":
                await asyncio.sleep(1)
            await normal_diagnostic(kind, **fields)

        lifecycle.diagnostic = delayed_diagnostic  # type: ignore[method-assign]

    async def active() -> bool:
        return False

    task = asyncio.create_task(
        EpicBrainstormGateway(installation=installation, supervisor=supervisor).execute(
            _make_snapshot(),
            (),
            BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())),
            cancelled=active,
            lifecycle=lifecycle,
        )
    )
    try:
        began = asyncio.get_running_loop().time()
        await asyncio.wait_for(exchanging.wait(), 5)
        if not after_proposal:
            task.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        task.cancel()
        release.set()
        result = await asyncio.wait_for(task, 5)
        if blocked_diagnostic:
            assert asyncio.get_running_loop().time() - began < 0.8
        assert result.failure == "cancelled" and result.proposal is None
        assert len(lifecycle.receipts) == len(lifecycle.results) == 1
        assert (
            ClientProcessSupervisor.identity_status(lifecycle.receipts[0])
            is ProcessIdentityStatus.GONE
        )
    finally:
        release.set()
        if supervisor.session is not None:
            await supervisor.session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_diagnostic", [False, True])
async def test_official_exchange_timeout_still_settles_real_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_diagnostic: bool
) -> None:
    import forge.agents.epic_brainstorm_gateway as gateway_module

    async def exchange(_session, *_args):
        await asyncio.Event().wait()

    monkeypatch.setattr(gateway_module, "codex_exchange", exchange)
    installation = _sleeping_codex_installation(tmp_path, monkeypatch)
    supervisor = _CapturingSupervisor()
    lifecycle = MockLifecycle()
    if blocked_diagnostic:
        normal_diagnostic = lifecycle.diagnostic

        async def delayed_diagnostic(kind: str, **fields: object) -> None:
            if kind == "settlement_started":
                await asyncio.sleep(1)
            await normal_diagnostic(kind, **fields)

        lifecycle.diagnostic = delayed_diagnostic  # type: ignore[method-assign]

    async def active() -> bool:
        return False

    try:
        began = asyncio.get_running_loop().time()
        result = await EpicBrainstormGateway(
            installation=installation, supervisor=supervisor
        ).execute(
            _make_snapshot(TaskBudget(max_provider_attempts=2, max_duration_seconds=1)),
            (),
            BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())),
            cancelled=active,
            lifecycle=lifecycle,
        )
        assert result.failure == "timeout" and result.proposal is None
        if blocked_diagnostic:
            assert asyncio.get_running_loop().time() - began < 1.8
        assert len(lifecycle.receipts) == len(lifecycle.results) == 1
        assert (
            ClientProcessSupervisor.identity_status(lifecycle.receipts[0])
            is ProcessIdentityStatus.GONE
        )
    finally:
        if supervisor.session is not None:
            await supervisor.session.close()


class _ScriptedSession:
    def __init__(self, frames: list[dict[str, object]]) -> None:
        self.frames = frames
        self.sent: list[dict[str, object]] = []
        self.stdin_closed = False
        self.settled = False

    async def send(self, frame: dict[str, object]) -> None:
        self.sent.append(frame)

    async def receive(self) -> dict[str, object] | None:
        return self.frames.pop(0) if self.frames else None

    async def close_stdin(self) -> None:
        self.stdin_closed = True

    async def wait_closed(self) -> None:
        self.settled = True


def _guard_proposal_settlement(
    session: _ScriptedSession,
    tools: AuthoringTools,
    monkeypatch: pytest.MonkeyPatch,
    *,
    uncertain: bool,
) -> list[str]:
    events: list[str] = []
    original_wait = session.wait_closed

    async def diagnostic(kind: str, **_fields: object) -> None:
        events.append(kind)

    async def wait_closed() -> None:
        events.append("settlement_started")
        assert "proposal_received" in events
        if uncertain:
            raise ClientSettlementUncertain(ClientProcessResult(
                receipt=ClientProcessReceipt("scripted", 1, "scripted", 0),
                return_code=None, frames=(), stdout_byte_count=0, stderr="",
                stderr_byte_count=0, stdout_truncated=False, stderr_truncated=False,
                outcome="unsettled", stop_confirmed=False,
            ))
        await original_wait()
        events.append("process_settled")

    tools.diagnostic = diagnostic
    monkeypatch.setattr(session, "wait_closed", wait_closed)
    return events


def test_authoring_usage_sparse_zero_large_and_invalid_updates_are_atomic() -> None:
    usage = AuthoringUsage()
    usage.observe(
        {"input_tokens": 0, "cached_input_tokens": 0},
        input_key="input_tokens", output_key="output_tokens", cache_key="cached_input_tokens",
    )
    assert usage.telemetry(1).input_tokens == 0
    assert usage.telemetry(1).output_tokens is None
    usage.observe(
        {"input_tokens": 10_000_017, "output_tokens": 0,
         "cached_input_tokens": 3},
        input_key="input_tokens", output_key="output_tokens", cache_key="cached_input_tokens",
    )
    measured = usage.telemetry(1)
    assert (measured.input_tokens, measured.output_tokens, measured.cached_input_tokens) == (
        10_000_017, 0, 3
    )
    for bad in (
        {"input_tokens": 10_000_018, "cached_input_tokens": 10_000_019},
        {"input_tokens": 2**63, "output_tokens": 1},
        {"input_tokens": 10_000_016, "output_tokens": 1},
        {"input_tokens": 10_000_018, "cached_input_tokens": 2},
    ):
        with pytest.raises(ProtocolError):
            usage.observe(
                bad, input_key="input_tokens", output_key="output_tokens",
                cache_key="cached_input_tokens",
            )
        assert usage.telemetry(1) == measured


def test_claude_authoring_usage_normalizes_cache_and_sparse_replay() -> None:
    usage = AuthoringUsage()
    usage.observe_claude({"input_tokens": 2, "output_tokens": 4,
                          "cache_read_input_tokens": 20,
                          "cache_creation_input_tokens": 3})
    assert (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens) == (25, 4, 23)
    usage.observe_claude({"input_tokens": 2, "output_tokens": 4,
                          "cache_read_input_tokens": 20,
                          "cache_creation_input_tokens": 3})
    assert (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens) == (25, 4, 23)
    usage.observe_claude({"output_tokens": 5})
    assert (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens) == (25, 5, 23)
    usage.observe_claude({"cache_read_input_tokens": 21, "thinking_tokens": True})
    assert (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens) == (26, 5, 24)
    before = (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens)
    for invalid in (
        {"input_tokens": True}, {"input_tokens": -1},
        {"input_tokens": 2**63}, {"input_tokens": 2**63 - 1},
        {"cache_read_input_tokens": 20},
    ):
        with pytest.raises(ProtocolError):
            usage.observe_claude(invalid)
        assert (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens) == before

    zero = AuthoringUsage()
    zero.observe_claude({"input_tokens": 0, "output_tokens": 0,
                         "cache_read_input_tokens": 0})
    assert (zero.input_tokens, zero.output_tokens, zero.cached_input_tokens) == (0, 0, 0)
    missing = AuthoringUsage()
    missing.observe_claude({"input_tokens": 2, "output_tokens": 0})
    assert (missing.input_tokens, missing.output_tokens, missing.cached_input_tokens) == (
        2, 0, None,
    )
    for cache_name in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        one_component = AuthoringUsage()
        one_component.observe_claude({"input_tokens": 2, "output_tokens": 0,
                                      cache_name: 3, "thinking_tokens": True})
        assert (one_component.input_tokens, one_component.output_tokens,
                one_component.cached_input_tokens) == (5, 0, 3)
    overflow = AuthoringUsage()
    with pytest.raises(ProtocolError):
        overflow.observe_claude({"input_tokens": 2**63 - 1,
                                 "cache_read_input_tokens": 1})
    assert (overflow.input_tokens, overflow.cached_input_tokens) == (None, None)


@pytest.mark.parametrize(
    ("case", "expected_failure"),
    [("success", None), ("invalid", "invalid_output"), ("cap", "budget_exhausted")],
)
@pytest.mark.asyncio
async def test_observed_antigravity_authoring_actual_stream_settles_before_final(
    tmp_path: Path, case: str, expected_failure: str,
) -> None:
    model = "gemini-scripted-brainstorm"
    job = _make_snapshot(
        TaskBudget(max_provider_attempts=2, max_duration_seconds=10, max_input_tokens=10)
        if case == "cap" else None
    )
    job = job.model_copy(update={"route": replace(
        job.route,
        requested=replace(job.route.requested, client="antigravity_cli", model=model),
        effective=replace(job.route.effective, client="antigravity_cli", model=model),
    )})
    lifecycle = MockLifecycle()
    route = RouteCohort.from_members((RouteMember(
        role="authoring", provider="google", model=model,
        reasoning_effort="low", ordinal=0,
    ),))
    binding = WorkUnitBinding(
        subject=SubjectKey(project_id=job.project_id, kind="authoring_job", subject_id=job.job_id),
        work_unit_id=uuid4(), admitted_phase_id="authoring_exchange",
        configured_route=route, effective_route=route,
        source=SourceBinding(kind="authoring_attempt", source_id=lifecycle.attempt_id),
    )
    session = _ScriptedSession([
        {"event": "init", "init": {"model": model},
         "conversation_id": "agy-thread"},
        {"event": "step_update", "step_update": {
            "conversation_id": "agy-thread", "usage": {"input_tokens": 2, "output_tokens": 1},
            "context": {"occupied_tokens": 5, "capacity_tokens": 100}}},
        {"event": "result", "result": {
            "conversation_id": "agy-thread", "status": "SUCCESS",
            "usage": {"input_tokens": 10_000_017, "output_tokens": 4,
                      "cache_read_tokens": 3},
            "context": {"occupied_tokens": 200 if case == "success" else 7,
                        "capacity_tokens": 100, "compaction_count": 0},
            "structured_output": {"proposal": {
                "turn_id": str(uuid4() if case == "invalid" else job.prompt_turn_id),
                "problem": "proposal"}},
        }},
    ])
    session.closed = False

    async def close(*, completed: bool = False):
        session.closed = True
        assert completed is (case != "invalid")
        session.settled = True
        return ClientProcessResult(
            receipt=ClientProcessReceipt("scripted", 1, "scripted", 0.0),
            return_code=0, frames=(), stdout_byte_count=0, stderr="",
            stderr_byte_count=0, stdout_truncated=False, stderr_truncated=False,
            outcome="completed", stop_confirmed=True,
        )

    session.close = close  # type: ignore[attr-defined, method-assign]

    class Supervisor:
        async def start(self, *_args, **_kwargs):
            return session

    class Sink:
        def __init__(self) -> None:
            self.events: list[Observation] = []

        async def observe_usage(self, event: Observation) -> None:
            if event.final:
                assert session.closed and session.settled
            self.events.append(event)

    executable = Path(sys.executable)
    installation = AntigravityInstallationSpec(
        client="antigravity_cli", account="dev-account", model=model,
        effort="low", executable=str(executable),
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
        client_version="0.59.0", cwd=str(tmp_path), home=str(tmp_path),
        quota={"account": "dev-account", "pool": "default-pool"},
    )
    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))
    sink = Sink()
    result = await EpicBrainstormGateway(
        installation=installation, supervisor=Supervisor()
    ).execute_observed(
        job, (), reader, cancelled=lambda: asyncio.sleep(0, result=False),
        lifecycle=lifecycle, usage_binding=binding, usage_sink=sink,
    )

    assert result.failure == expected_failure
    assert result.telemetry is not None and result.telemetry.input_tokens == 10_000_017
    finals = [event for event in sink.events if event.final]
    assert len(finals) == 1
    assert finals[0].final_status == (result.failure or "succeeded")
    assert finals[0].tokens.input_tokens == 10_000_017
    assert finals[0].context is not None
    assert (finals[0].context.occupied_tokens, finals[0].context.capacity_tokens,
            finals[0].context.compaction_count) == (
        (None if case == "success" else 7), 100, 0,
    )


@pytest.mark.parametrize(
    ("case", "expected_failure"),
    [("invalid", "invalid_output"), ("cap_unsettled", "process_unsettled"),
     ("cap_cancelled", "cancelled"), ("cap_rejected", "budget_exhausted"),
     ("nonzero", "process_failed"), ("large_success", None),
     ("sparse_zero", None), ("delivery_deadline", None),
     ("delivery_cancel_twice", None)],
)
@pytest.mark.asyncio
async def test_observed_gemini_authoring_final_follows_classification_and_close(
    tmp_path: Path, case: str, expected_failure: str,
) -> None:
    budget = (
        TaskBudget(max_provider_attempts=2, max_duration_seconds=10, max_input_tokens=10)
        if case.startswith("cap_") else (
            TaskBudget(max_provider_attempts=2, max_duration_seconds=1)
            if case == "delivery_deadline" else None
        )
    )
    job = _make_snapshot(budget)
    lifecycle = MockLifecycle()
    route = RouteCohort.from_members((RouteMember(
        role="authoring", provider="google", model="scripted-brainstorm",
        reasoning_effort="low", ordinal=0,
    ),))
    binding = WorkUnitBinding(
        subject=SubjectKey(project_id=job.project_id, kind="authoring_job", subject_id=job.job_id),
        work_unit_id=uuid4(), admitted_phase_id="authoring_exchange",
        configured_route=route, effective_route=route,
        source=SourceBinding(kind="authoring_attempt", source_id=lifecycle.attempt_id),
    )
    session = _ScriptedSession([
        {"jsonrpc": "2.0", "id": 1, "result": {
            "protocolVersion": 1, "agentInfo": {"name": "gemini-cli", "version": "0.59.0"}}},
        {"jsonrpc": "2.0", "id": 2, "result": {
            "sessionId": "gemini-thread", "models": {"currentModelId": "scripted-brainstorm"}}},
        {"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "gemini-thread", "update": {"sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": json.dumps({"proposal": {
                    "turn_id": str(uuid4() if case == "invalid" else job.prompt_turn_id),
                    "problem": "proposal"}})}}}},
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn", "_meta": {
            "quota": {"token_count": {
                "input_tokens": 10_000_017 if case == "large_success" else (
                    0 if case == "sparse_zero" else 17),
                "output_tokens": None if case == "sparse_zero" else 4,
                "cached_content_token_count": 0 if case == "sparse_zero" else 3},
                "model_usage": [{"model": "scripted-brainstorm"}]}}}},
    ])
    session.closed = False

    if case == "cap_cancelled":
        async def cancelled_wait_closed():
            session.settled = True
            raise asyncio.CancelledError

        session.wait_closed = cancelled_wait_closed  # type: ignore[method-assign]
    if case == "delivery_deadline":
        async def near_deadline_wait_closed():
            await asyncio.sleep(0.75)
            session.settled = True

        session.wait_closed = near_deadline_wait_closed  # type: ignore[method-assign]

    async def close(*, completed: bool = False):
        session.closed = True
        assert completed is (case in {
            "cap_unsettled", "cap_rejected", "nonzero", "large_success", "sparse_zero",
            "delivery_deadline", "delivery_cancel_twice",
        })
        if case != "cap_unsettled":
            session.settled = True
        return ClientProcessResult(
            receipt=ClientProcessReceipt("scripted", 1, "scripted", 0.0),
            return_code=1 if case == "nonzero" else 0,
            frames=(), stdout_byte_count=0, stderr="",
            stderr_byte_count=0, stdout_truncated=False, stderr_truncated=False,
            outcome="stop_uncertain" if case == "cap_unsettled" else "completed",
            stop_confirmed=case != "cap_unsettled",
        )

    session.close = close  # type: ignore[attr-defined, method-assign]

    class Supervisor:
        async def start(self, *_args, **_kwargs):
            return session

    class Sink:
        def __init__(self) -> None:
            self.events: list[Observation] = []
            self.entered = asyncio.Event()
            self.cleaning = asyncio.Event()
            self.active = False
            self.cancellations = 0
            self.delivery_task: asyncio.Task[None] | None = None

        async def observe_usage(self, event: Observation) -> None:
            assert session.closed and session.settled
            if case.startswith("delivery_"):
                self.delivery_task = asyncio.current_task()
                self.active = True
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
                    if case == "delivery_cancel_twice":
                        self.cleaning.set()
                        try:
                            await asyncio.Event().wait()
                        except asyncio.CancelledError:
                            self.cancellations += 1
                            raise
                    raise
                finally:
                    self.active = False
            self.events.append(event)

    executable = Path(sys.executable)
    installation = GeminiInstallationSpec(
        client="gemini_cli", account="dev-account", model="scripted-brainstorm",
        effort="low", executable=str(executable),
        executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
        client_version="0.59.0", cwd=str(tmp_path), home=str(tmp_path),
        quota={"account": "dev-account", "pool": "default-pool"},
    )
    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))
    sink = Sink()
    gateway = EpicBrainstormGateway(installation=installation, supervisor=Supervisor())
    started = asyncio.get_running_loop().time()
    execution = asyncio.create_task(gateway.execute_observed(
        job, (), reader, cancelled=lambda: asyncio.sleep(0, result=False),
        lifecycle=lifecycle, usage_binding=binding, usage_sink=sink,
    ))
    if case == "delivery_cancel_twice":
        await asyncio.wait_for(sink.entered.wait(), 5)
        assert session.closed and session.settled
        execution.cancel()
        await asyncio.wait_for(sink.cleaning.wait(), 1)
        execution.cancel()
    result = await asyncio.wait_for(execution, 5)
    assert result.failure == expected_failure
    assert result.telemetry is not None
    assert result.telemetry.input_tokens == (
        10_000_017 if case == "large_success" else (0 if case == "sparse_zero" else 17)
    )
    if case.startswith("delivery_"):
        assert not sink.events and not sink.active
        assert sink.delivery_task is not None and sink.delivery_task.done()
        assert "usage observation delivery incomplete" in result.telemetry.unknown_telemetry_reasons
        if case == "delivery_deadline":
            assert asyncio.get_running_loop().time() - started > job.budget.max_duration_seconds
            assert sink.cancellations == 1
        else:
            assert sink.cancellations == 2
    else:
        assert len(sink.events) == 1
        assert sink.events[0].final_status == (result.failure or "succeeded")
        assert sink.events[0].tokens.input_tokens == result.telemetry.input_tokens
    if case == "sparse_zero":
        assert result.telemetry.output_tokens is None
        assert sink.events[0].tokens.output_tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_gemini_authoring_uses_acp_and_preserves_unknown_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uncertain: bool
) -> None:
    job = _make_snapshot()
    session = _ScriptedSession(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": 1,
                    "agentInfo": {"name": "gemini-cli", "version": "0.59.0"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "sessionId": "gemini-thread",
                    "models": {"currentModelId": "scripted-brainstorm"},
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "gemini-thread",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "proposal": {
                                        "turn_id": str(job.prompt_turn_id),
                                        "problem": "ACP proposal",
                                    }
                                }
                            ),
                        },
                    },
                },
            },
            {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
        ]
    )
    usage = AuthoringUsage()
    tools = AuthoringTools(
        BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())), 10, usage
    )
    events = _guard_proposal_settlement(session, tools, monkeypatch, uncertain=uncertain)
    await tools.start()
    try:
        if uncertain:
            with pytest.raises(ClientSettlementUncertain):
                await gemini_exchange(
                    session, job, (), tools, usage, model="scripted-brainstorm", cwd=str(tmp_path)
                )
        else:
            proposal = await gemini_exchange(
                session, job, (), tools, usage, model="scripted-brainstorm", cwd=str(tmp_path)
            )
            assert proposal.problem == "ACP proposal"
    finally:
        await tools.close()
    assert [item["method"] for item in session.sent] == [
        "initialize",
        "session/new",
        "session/prompt",
    ]
    assert session.stdin_closed and session.settled is not uncertain
    assert events.index("proposal_received") < events.index("settlement_started")
    assert usage.telemetry(1).input_tokens is None


@pytest.mark.asyncio
async def test_gemini_authoring_only_accepts_echoed_forge_read_tool(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Safe context", encoding="utf-8")
    job = _make_snapshot()
    usage = AuthoringUsage()
    tools = AuthoringTools(
        BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())), 2, usage
    )
    await tools.start()
    try:
        await tools.mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        await tools.mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        response = await tools.mcp(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "README.md"}},
            }
        )
        assert response is not None
        receipt = json.loads(response["result"]["content"][0]["text"])
        prelude = "n" * 1_048_500
        session = _ScriptedSession(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"protocolVersion": 1, "agentInfo": {"name": "gemini-cli"}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "sessionId": "thread",
                        "models": {"currentModelId": "scripted-brainstorm"},
                    },
                },
                *[
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": "thread",
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {
                                    "type": "text",
                                    "text": prelude[index : index + 240_000],
                                },
                            },
                        },
                    }
                    for index in range(0, len(prelude), 240_000)
                ],
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": "thread",
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "tool-1",
                            "kind": "other",
                            "status": "in_progress",
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": "thread",
                        "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "tool-1",
                            "kind": "other",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "content",
                                    "content": {"type": "text", "text": json.dumps(receipt)},
                                }
                            ],
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": "thread",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "discard this interim answer"},
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": "thread",
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "tool-2",
                            "kind": "other",
                            "status": "in_progress",
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": "thread",
                        "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "tool-2",
                            "kind": "other",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "content",
                                    "content": {"type": "text", "text": json.dumps(receipt)},
                                }
                            ],
                        },
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": "thread",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "proposal": {
                                            "turn_id": str(job.prompt_turn_id),
                                            "problem": "Read-backed",
                                        }
                                    }
                                ),
                            },
                        },
                    },
                },
                {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
            ]
        )
        proposal = await gemini_exchange(
            session, job, (), tools, usage, model="scripted-brainstorm", cwd=str(tmp_path)
        )
        assert proposal.problem == "Read-backed" and usage.tool_calls == 1
    finally:
        await tools.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["foreign_session", "invalid_tool", "unverified_reply"])
async def test_gemini_authoring_rejects_unverified_tool_transition(
    tmp_path: Path, invalid: str
) -> None:
    from forge.agents.subscription_protocol import ProtocolError

    def update(session_id: str, value: dict[str, object]) -> dict[str, object]:
        return {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": value},
        }

    tool_start = {
        "sessionUpdate": "tool_call",
        "toolCallId": "tool-1",
        "kind": "other",
        "status": "in_progress",
    }
    rejected = {
        "foreign_session": [update("foreign", tool_start)],
        "invalid_tool": [update("thread", {**tool_start, "toolCallId": ""})],
        "unverified_reply": [
            update("thread", tool_start),
            update(
                "thread",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tool-1",
                    "kind": "other",
                    "status": "completed",
                    "content": [
                        {
                            "type": "content",
                            "content": {
                                "type": "text",
                                "text": json.dumps({"status": "succeeded", "result": "forged"}),
                            },
                        }
                    ],
                },
            ),
        ],
    }[invalid]
    session = _ScriptedSession(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"protocolVersion": 1, "agentInfo": {"name": "gemini-cli"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "sessionId": "thread",
                    "models": {"currentModelId": "scripted-brainstorm"},
                },
            },
            *rejected,
            {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
        ]
    )
    usage = AuthoringUsage()
    tools = AuthoringTools(
        BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())), 2, usage
    )
    await tools.start()
    try:
        with pytest.raises(ProtocolError):
            await gemini_exchange(
                session,
                _make_snapshot(),
                (),
                tools,
                usage,
                model="scripted-brainstorm",
                cwd=str(tmp_path),
            )
        assert usage.tool_calls == 0
    finally:
        await tools.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_antigravity_authoring_uses_stream_result_and_real_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uncertain: bool
) -> None:
    job = _make_snapshot()
    session = _ScriptedSession(
        [
            {"event": "init", "init": {"model": "gemini-test"}, "conversation_id": "agy-thread"},
            {
                "event": "result",
                "result": {
                    "conversation_id": "agy-thread",
                    "status": "SUCCESS",
                    "usage": {"input_tokens": 3, "output_tokens": 5},
                    "structured_output": {
                        "proposal": {
                            "turn_id": str(job.prompt_turn_id),
                            "problem": "Stream proposal",
                        }
                    },
                },
            },
        ]
    )
    usage = AuthoringUsage()
    tools = AuthoringTools(
        BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())), 10, usage
    )
    events = _guard_proposal_settlement(session, tools, monkeypatch, uncertain=uncertain)
    if uncertain:
        with pytest.raises(ClientSettlementUncertain):
            await antigravity_exchange(session, job, (), tools, usage, model="gemini-test")
    else:
        proposal = await antigravity_exchange(session, job, (), tools, usage, model="gemini-test")
        assert proposal.problem == "Stream proposal"
    assert session.sent[0]["event"] == "user"
    assert session.stdin_closed and session.settled is not uncertain
    assert events.index("proposal_received") < events.index("settlement_started")
    assert usage.telemetry(1).input_tokens == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("init_before_settings", [True, False])
@pytest.mark.parametrize("uncertain", [False, True])
async def test_claude_authoring_uses_stream_json_mcp_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, init_before_settings: bool, uncertain: bool
) -> None:
    import hashlib

    job = _make_snapshot()
    identity = str(job.job_id)

    def mcp(request_id: str, message: dict[str, object]) -> dict[str, object]:
        return {
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": "mcp_message", "server_name": "forge", "message": message},
        }

    settings_frame = {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": "forge_settings",
            "response": {"applied": {"model": "claude-test", "effort": "medium"}},
        },
    }
    init_frame = {
        "type": "system",
        "subtype": "init",
        "session_id": identity,
        "model": "claude-test",
    }
    session = _ScriptedSession(
        [
            mcp(
                "mcp-init",
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "peer", "version": "1"},
                    },
                },
            ),
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": "forge_initialize",
                    "response": {},
                },
            },
            mcp("mcp-ready", {"jsonrpc": "2.0", "method": "notifications/initialized"}),
            mcp("mcp-list", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            *(
                (init_frame, settings_frame)
                if init_before_settings
                else (settings_frame, init_frame)
            ),
            {
                "type": "result",
                "subtype": "success",
                "session_id": identity,
                "is_error": False,
                "usage": {"input_tokens": 4, "output_tokens": 6},
                "structured_output": {
                    "proposal": {"turn_id": str(job.prompt_turn_id), "problem": "Claude proposal"}
                },
            },
        ]
    )
    exe = Path(sys.executable)
    gateway = ClaudeGateway(
        ClaudeInstallation(
            executable=str(exe),
            cwd=str(tmp_path),
            model="claude-test",
            effort="medium",
            client_home=str(tmp_path),
            account="dev-account",
            executable_digest=hashlib.sha256(exe.read_bytes()).hexdigest(),
        ),
        trust=LocalCliTrust.OPERATOR,
    )
    usage = AuthoringUsage()
    tools = AuthoringTools(
        BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())), 10, usage
    )
    events = _guard_proposal_settlement(session, tools, monkeypatch, uncertain=uncertain)
    if uncertain:
        with pytest.raises(ClientSettlementUncertain):
            await claude_exchange(session, gateway, job, (), tools, usage)
    else:
        proposal = await claude_exchange(session, gateway, job, (), tools, usage)
        assert proposal.problem == "Claude proposal"
    assert session.sent[0]["request_id"] == "forge_initialize"
    assert session.sent[-1]["type"] == "user"
    assert session.stdin_closed and session.settled is not uncertain
    assert events.index("proposal_received") < events.index("settlement_started")
    assert usage.telemetry(1).output_tokens == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["raises", "stalls"])
async def test_claude_authoring_quota_survives_diagnostic_fault(
    tmp_path: Path, fault: str
) -> None:
    job = _make_snapshot()
    identity = str(job.job_id)

    def mcp(request_id: str, message: dict[str, object]) -> dict[str, object]:
        return {"type": "control_request", "request_id": request_id,
                "request": {"subtype": "mcp_message", "server_name": "forge", "message": message}}

    session = _ScriptedSession([
        mcp("mcp-init", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18"}}),
        {"type": "control_response", "response": {"subtype": "success",
                                               "request_id": "forge_initialize", "response": {}}},
        mcp("mcp-ready", {"jsonrpc": "2.0", "method": "notifications/initialized"}),
        mcp("mcp-list", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        {"type": "control_response", "response": {"subtype": "success",
                                               "request_id": "forge_settings",
                                               "response": {"applied": {"model": "claude-test",
                                                                        "effort": "medium"}}}},
        {"type": "system", "subtype": "init", "session_id": identity, "model": "claude-test"},
        {"type": "result", "subtype": "success", "session_id": identity, "is_error": True,
         "errors": ["You've hit your weekly limit"], "api_error_status": 429},
    ])
    exe = Path(sys.executable)
    gateway = ClaudeGateway(
        ClaudeInstallation(executable=str(exe), cwd=str(tmp_path), model="claude-test",
                           effort="medium", client_home=str(tmp_path), account="dev-account",
                           executable_digest=hashlib.sha256(exe.read_bytes()).hexdigest()),
        trust=LocalCliTrust.OPERATOR,
    )

    async def diagnostic(kind: str, **fields: object) -> None:
        if kind == "provider_error" and fields.get("failure") == "quota_exhausted":
            if fault == "raises":
                raise RuntimeError("diagnostic unavailable")
            await asyncio.Event().wait()

    usage = AuthoringUsage()
    tools = AuthoringTools(
        BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())),
        10, usage, diagnostic=diagnostic,
    )
    with pytest.raises(AuthoringProviderFailure) as caught:
        await asyncio.wait_for(claude_exchange(session, gateway, job, (), tools, usage), 0.8)
    assert caught.value.failure == "quota_exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("stop_case", [
    "uncertain_receipt", "uncertain_raise", "diagnostic_cancel", "diagnostic_deadline",
])
@pytest.mark.parametrize("known_reset", [False, True])
async def test_authoring_quota_evidence_survives_stop_and_diagnostic_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str,
    stop_case: str, known_reset: bool,
) -> None:
    import forge.agents.epic_brainstorm_gateway as gateway_module

    reset = "2099-01-01T00:00:00+00:00" if known_reset else None

    async def exchange(*_args: object) -> None:
        raise AuthoringProviderFailure("quota_exhausted", reset)

    monkeypatch.setattr(
        gateway_module, "claude_exchange" if provider == "claude" else "codex_exchange",
        exchange,
    )

    class Session:
        async def close(self, **_kwargs: object) -> ClientProcessResult:
            receipt = ClientProcessResult(
                receipt=ClientProcessReceipt("scripted", 1, "scripted", 0),
                return_code=None, frames=(), stdout_byte_count=0, stderr="",
                stderr_byte_count=0, stdout_truncated=False, stderr_truncated=False,
                outcome="exited" if stop_case.startswith("diagnostic_") else "unsettled",
                stop_confirmed=stop_case.startswith("diagnostic_"),
            )
            if stop_case == "uncertain_raise":
                raise ClientSettlementUncertain(receipt)
            return receipt

    class Supervisor:
        async def start(self, *_args: object, **_kwargs: object) -> Session:
            return Session()

    exe = Path(sys.executable)
    common = {
        "account": "dev-account", "model": "claude-test", "effort": "medium",
        "executable": str(exe), "executable_digest": hashlib.sha256(exe.read_bytes()).hexdigest(),
        "client_version": "1.0.0", "cwd": str(tmp_path), "home": str(tmp_path),
        "quota": {"account": "dev-account", "pool": "default-pool"},
    }
    if provider == "claude":
        installation = ClaudeInstallationSpec(client="claude_code", **common)
    else:
        common["account"] = codex_account_identity("codex@example.invalid")
        common["model"] = "gpt-5.6-luna"
        installation = CodexInstallationSpec(client="codex_app_server", **common)
    lifecycle = MockLifecycle()
    if stop_case.startswith("diagnostic_"):
        owner = asyncio.current_task()

        async def cancel_diagnostic(kind: str, **fields: object) -> None:
            if kind == "provider_error" and fields.get("failure") == "quota_exhausted":
                assert owner is not None
                if stop_case == "diagnostic_cancel":
                    asyncio.get_running_loop().call_later(0.02, owner.cancel)
                await asyncio.Event().wait()

        lifecycle.diagnostic = cancel_diagnostic  # type: ignore[method-assign]
    gateway = EpicBrainstormGateway(installation=installation, supervisor=Supervisor())

    async def invoke():
        return await gateway.execute(
            _make_snapshot(), (),
            BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())),
            cancelled=lambda: asyncio.sleep(0, result=False), lifecycle=lifecycle,
        )

    if stop_case == "diagnostic_deadline":
        async with asyncio.timeout(0.02) as deadline:
            result = await invoke()
        assert deadline.expired()
    else:
        result = await invoke()
    assert result.failure == (
        "cancelled" if stop_case.startswith("diagnostic_") else "process_unsettled"
    )
    assert result.quota_reset_at == reset
    assert result.quota_exhausted


@pytest.mark.parametrize("failure", ["quota_exhausted", "process_unsettled", "cancelled"])
def test_decomposition_adapter_forwards_confirmed_quota_evidence(failure: str) -> None:
    reset = "2099-01-01T00:00:00+00:00"
    result = ValidatedDecompositionGateway._validate(
        _make_snapshot(),
        DecompositionGatewayResult(
            proposal=None, telemetry=None, failure=failure,
            quota_reset_at=reset, quota_exhausted=True,
        ),
    )
    assert result.failure == failure
    assert result.quota_reset_at == reset
    assert result.quota_exhausted


@pytest.mark.asyncio
async def test_observed_official_claude_actual_exchange_normalizes_cached_input(
    tmp_path: Path,
) -> None:
    route = RouteSpec(
        provider="anthropic", client="claude_code", model="claude-test",
        effort=ReasoningEffort.MEDIUM, auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    job = _make_snapshot().model_copy(update={
        "route": RouteBinding(requested=route, effective=route),
    })
    attempt_id = uuid4()
    cohort = RouteCohort.from_members((RouteMember(
        role="authoring", provider="anthropic", model="claude-test",
        reasoning_effort="medium", ordinal=0,
    ),))
    binding = WorkUnitBinding(
        subject=SubjectKey(project_id=job.project_id, kind="authoring_job", subject_id=job.job_id),
        work_unit_id=uuid4(), admitted_phase_id="authoring_exchange",
        configured_route=cohort, effective_route=cohort,
        source=SourceBinding(kind="authoring_attempt", source_id=attempt_id),
    )
    identity = str(job.job_id)

    def mcp(request_id: str, message: dict[str, object]) -> dict[str, object]:
        return {"type": "control_request", "request_id": request_id,
                "request": {"subtype": "mcp_message", "server_name": "forge", "message": message}}

    session = _ScriptedSession([
        mcp("mcp-init", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "peer", "version": "1"}}}),
        {"type": "control_response", "response": {
            "subtype": "success", "request_id": "forge_initialize", "response": {}}},
        mcp("mcp-ready", {"jsonrpc": "2.0", "method": "notifications/initialized"}),
        mcp("mcp-list", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        {"type": "control_response", "response": {
            "subtype": "success", "request_id": "forge_settings",
            "response": {"applied": {"model": "claude-test", "effort": "medium"}}}},
        {"type": "system", "subtype": "init", "session_id": identity, "model": "claude-test"},
        mcp("mcp-call", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "list_files", "arguments": {"path": "."},
                                    "_meta": {"claudecode/toolUseId": "authoring-read-1",
                                              "progressToken": 1}}}),
        {"type": "result", "subtype": "success", "session_id": identity,
         "is_error": False,
         "usage": {"input_tokens": 2, "output_tokens": 6,
                   "cache_read_input_tokens": 20, "cache_creation_input_tokens": 3},
         "structured_output": {"proposal": {
             "turn_id": str(job.prompt_turn_id), "problem": "Cached Claude proposal"}}},
    ])
    session.closed = False

    async def close(*, completed: bool = False) -> ClientProcessResult:
        assert supervisor.before_stop is not None
        await supervisor.before_stop()
        supervisor.stop_hook_called = True
        session.closed = True
        session.completed = completed
        return ClientProcessResult(
            receipt=ClientProcessReceipt("scripted", 1, "scripted", 0.0),
            return_code=0, frames=(), stdout_byte_count=0, stderr="",
            stderr_byte_count=0, stdout_truncated=False, stderr_truncated=False,
            outcome="completed", stop_confirmed=True,
        )

    session.close = close  # type: ignore[attr-defined, method-assign]

    class Supervisor:
        before_stop = None
        stop_hook_called = False

        async def start(self, *_args, **_kwargs):
            self.before_stop = _kwargs.get("before_stop")
            return session

    class Sink:
        def __init__(self) -> None:
            self.events: list[Observation] = []

        async def observe_usage(self, event: Observation) -> None:
            assert session.closed and session.settled
            self.events.append(event)

    class MinimalLifecycle:
        async def launch_intent(self, launch_id: str) -> None:
            pass

        async def started(self, receipt: object) -> None:
            pass

        async def finished(self, receipt: object, result: object) -> None:
            pass

    executable = Path(sys.executable)
    installation = ClaudeInstallationSpec(
        client="claude_code", account="dev-account", model="claude-test", effort="medium",
        executable=str(executable), executable_digest=hashlib.sha256(executable.read_bytes()).hexdigest(),
        client_version="1.0.0", cwd=str(tmp_path), home=str(tmp_path),
        quota={"account": "dev-account", "pool": "default-pool"},
    )
    sink = Sink()
    supervisor = Supervisor()
    result = await EpicBrainstormGateway(
        installation=installation, supervisor=supervisor,
    ).execute_observed(
        job, (), BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())),
        cancelled=lambda: asyncio.sleep(0, result=False), lifecycle=MinimalLifecycle(),
        usage_binding=binding, usage_sink=sink,
    )
    assert result.failure is None and result.proposal is not None, (
        result.failure, [frame.get("type") for frame in session.sent],
        [frame.get("type") for frame in session.frames],
    )
    assert session.completed
    assert supervisor.stop_hook_called
    assert result.telemetry is not None
    assert (result.telemetry.input_tokens, result.telemetry.output_tokens,
            result.telemetry.cached_input_tokens, result.telemetry.tool_call_count) == (25, 6, 23, 1)
    assert len(sink.events) == 1 and sink.events[0].final
    assert sink.events[0].binding.source.source_id == attempt_id
    assert (sink.events[0].tokens.input_tokens, sink.events[0].tokens.cached_input_tokens) == (25, 23)
    assert any(
        frame.get("type") == "control_response"
        and isinstance(frame.get("response"), dict)
        and frame["response"].get("request_id") == "mcp-call"
        for frame in session.sent
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "init_variant",
    ["preinit", "valid", "foreign_session", "foreign_model", "malformed", "duplicate", "missing"],
)
async def test_claude_authoring_requires_identity_before_tool_callback(
    tmp_path: Path, init_variant: str
) -> None:
    from forge.agents.subscription_protocol import ProtocolError

    job = _make_snapshot()
    identity = str(job.job_id)

    def mcp(request_id: str, message: dict[str, object]) -> dict[str, object]:
        return {
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": "mcp_message", "server_name": "forge", "message": message},
        }

    init = {"type": "system", "subtype": "init", "session_id": identity, "model": "claude-test"}
    call = mcp(
        "authoring-read",
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "list_files",
                "arguments": {"path": "."},
                "_meta": {"claudecode/toolUseId": "tool-1", "progressToken": 1},
            },
        },
    )
    events = {
        "preinit": (call, init),
        "valid": (init, call),
        "foreign_session": ({**init, "session_id": "foreign"}, call),
        "foreign_model": ({**init, "model": "foreign"}, call),
        "malformed": ({**init, "model": None}, call),
        "duplicate": (init, init, call),
        "missing": (),
    }[init_variant]
    session = _ScriptedSession(
        [
            mcp(
                "mcp-init",
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                },
            ),
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": "forge_initialize",
                    "response": {},
                },
            },
            mcp("mcp-ready", {"jsonrpc": "2.0", "method": "notifications/initialized"}),
            mcp("mcp-list", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": "forge_settings",
                    "response": {"applied": {"model": "claude-test", "effort": "medium"}},
                },
            },
            *events,
            {
                "type": "result",
                "subtype": "success",
                "session_id": identity,
                "is_error": False,
                "usage": {"input_tokens": 2, "output_tokens": 3},
                "structured_output": {
                    "proposal": {"turn_id": str(job.prompt_turn_id), "problem": "Admitted"}
                },
            },
        ]
    )
    exe = Path(sys.executable)
    gateway = ClaudeGateway(
        ClaudeInstallation(
            executable=str(exe),
            cwd=str(tmp_path),
            model="claude-test",
            effort="medium",
            client_home=str(tmp_path),
            account="dev-account",
            executable_digest=hashlib.sha256(exe.read_bytes()).hexdigest(),
        ),
        trust=LocalCliTrust.OPERATOR,
    )
    usage = AuthoringUsage()
    tools = AuthoringTools(
        BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())), 10, usage
    )
    if init_variant != "valid":
        with pytest.raises(ProtocolError):
            await claude_exchange(session, gateway, job, (), tools, usage)
        assert usage.tool_calls == 0
    else:
        proposal = await claude_exchange(session, gateway, job, (), tools, usage)
        assert proposal.problem == "Admitted"
        assert usage.tool_calls == 1
        assert session.stdin_closed and session.settled


@pytest.mark.asyncio
async def test_authoring_tools_never_forward_reader_errors_or_dispatch_writes(
    tmp_path: Path,
) -> None:
    from forge.agents.subscription_protocol import ProtocolError

    async def revoked() -> None:
        raise RuntimeError("credential=private-provider-text")

    usage = AuthoringUsage()
    tools = AuthoringTools(
        BrainstormReadOnlyTools(
            RepositoryReader(root=str(tmp_path), secret_paths=()), authorize=revoked
        ),
        1,
        usage,
    )
    receipt = await tools.call("one", "read_file", {"path": "README.md"})
    assert receipt == {"status": "failed", "error": "repository read unavailable"}
    assert await tools.call("one", "read_file", {"path": "README.md"}) == receipt
    assert usage.tool_calls == 1
    with pytest.raises(ProtocolError, match="unregistered"):
        await tools.call("two", "write_file", {"path": "README.md"})
    with pytest.raises(ProtocolError, match="budget"):
        await tools.call("two", "read_file", {"path": "README.md"})
