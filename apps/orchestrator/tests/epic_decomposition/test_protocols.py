"""Tests verifying local CLI provider protocols for assisted epic decomposition."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from forge.agents.epic_brainstorm_protocol import (
    AuthoringTools,
    AuthoringUsage,
    antigravity_exchange,
    authoring_prompt,
    authoring_schema,
    claude_exchange,
    codex_exchange,
    gemini_exchange,
    proposal_from_output,
)
from forge.agents.subscription_protocol import ProtocolError
from forge.domain.epic_brainstorm import (
    AuthoringJobSnapshot,
    FrozenBriefContent,
)
from forge.domain.epic_brief import BriefContent, BriefRequirement
from forge.domain.epic_decomposition import DecompositionProposal
from forge.domain.operation import canonical_digest
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    ReasoningEffort,
    RouteBinding,
    RouteSpec,
    TaskBudget,
)
from forge.tools.epic_brainstorm import BrainstormReadOnlyTools
from forge.tools.repository import RepositoryReader


def _make_decomposition_snapshot(
    budget: TaskBudget | None = None,
) -> tuple[AuthoringJobSnapshot, BriefRequirement]:
    req = BriefRequirement(
        requirement_id=uuid4(), text="Core requirement", acceptance_criteria=["Criteria 1"]
    )
    brief = BriefContent(
        problem="Test problem",
        outcomes=["Outcome 1"],
        scope=["Scope 1"],
        exclusions=[],
        requirements=[req],
        decisions=[],
        assumptions=["Assumption 1"],
        open_questions=["Question 1?"],
    )
    brief.require_adoptable()
    digest = canonical_digest(brief.model_dump(mode="json"))
    brief_revision_id = uuid4()
    route = RouteSpec(
        provider="google",
        client="gemini_cli",
        model="scripted-model",
        effort=ReasoningEffort.LOW,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    snapshot = AuthoringJobSnapshot(
        job_id=uuid4(),
        epic_id=uuid4(),
        project_id=uuid4(),
        conversation_id=uuid4(),
        kind="decomposition",
        prompt_turn_id=uuid4(),
        budget=budget or TaskBudget(max_provider_attempts=2, max_duration_seconds=10),
        route=RouteBinding(requested=route, effective=route),
        input_brief_revision_id=brief_revision_id,
        input_brief_digest=digest,
        input_draft_digest="0" * 64,
        draft_content=FrozenBriefContent.model_validate(brief.model_dump(mode="json")),
        accepted_content=FrozenBriefContent.model_validate(brief.model_dump(mode="json")),
        expected_epic_version=1,
        conversation_version=1,
        reservation_id=uuid4(),
    )
    return snapshot, req


def _make_valid_decomposition_proposal(
    snapshot: AuthoringJobSnapshot, req: BriefRequirement
) -> dict[str, object]:
    return {
        "turn_id": str(snapshot.prompt_turn_id),
        "epic_id": str(snapshot.epic_id),
        "project_id": str(snapshot.project_id),
        "brief_revision_id": str(snapshot.input_brief_revision_id),
        "brief_digest": snapshot.input_brief_digest,
        "items": [
            {
                "item_id": str(uuid4()),
                "disposition": "required",
                "ordinal": 0,
                "title": "Item 1",
                "outcome": "Outcome 1",
                "acceptance_criteria": ["Criteria 1"],
                "source_requirement_ids": [str(req.requirement_id)],
                "dependency_item_ids": [],
            }
        ],
        "summary": "Decomposition breakdown",
        "assumptions": list(snapshot.accepted_content.assumptions),
        "open_questions": list(snapshot.accepted_content.open_questions),
        "resolved_turn_ids": [],
        "evidence": [],
    }


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


def test_authoring_schema_kind() -> None:
    decomp_schema = authoring_schema("decomposition")
    prop_props = decomp_schema["properties"]["proposal"]["properties"]
    assert "items" in prop_props
    assert "brief_digest" in prop_props
    assert "brief_revision_id" in prop_props

    brainstorm_schema = authoring_schema("brainstorm")
    bs_props = brainstorm_schema["properties"]["proposal"]["properties"]
    assert "items" not in bs_props
    assert "problem" in bs_props


def test_authoring_prompt_kind() -> None:
    decomp_job, _req = _make_decomposition_snapshot()
    prompt = authoring_prompt(decomp_job, ())
    assert prompt.startswith(
        "Propose a decomposition of this epic into child work items with dependency relationships."
    )
    # Parse embedded context JSON
    json_part = prompt[prompt.index("\n") + 1 :]
    ctx = json.loads(json_part)
    assert ctx["brief_revision_id"] == str(decomp_job.input_brief_revision_id)
    assert ctx["brief_digest"] == decomp_job.input_brief_digest
    assert ctx["epic_id"] == str(decomp_job.epic_id)
    assert ctx["project_id"] == str(decomp_job.project_id)
    assert ctx["prompt_turn_id"] == str(decomp_job.prompt_turn_id)


def test_proposal_from_output_decomposition() -> None:
    snapshot, req = _make_decomposition_snapshot()
    proposal_dict = _make_valid_decomposition_proposal(snapshot, req)

    # Valid
    result = proposal_from_output({"proposal": proposal_dict}, snapshot)
    assert isinstance(result, DecompositionProposal)
    assert result.summary == "Decomposition breakdown"
    assert len(result.items) == 1

    # Foreign turn ID
    foreign = dict(proposal_dict, turn_id=str(uuid4()))
    with pytest.raises(ProtocolError, match="foreign authoring turn"):
        proposal_from_output({"proposal": foreign}, snapshot)

    # Invalid envelope
    with pytest.raises(ProtocolError, match="invalid authoring result envelope"):
        proposal_from_output({"not_proposal": proposal_dict}, snapshot)
    with pytest.raises(ProtocolError, match="invalid authoring result envelope"):
        proposal_from_output({"proposal": proposal_dict, "extra": True}, snapshot)

    # Malformed proposal (schema violation)
    invalid_proposal = dict(proposal_dict, items="not-a-list")
    with pytest.raises(ProtocolError, match="invalid authoring proposal"):
        proposal_from_output({"proposal": invalid_proposal}, snapshot)

    # Kind mismatch: BrainstormProposal provided when decomposition expected
    bs_proposal = {
        "turn_id": str(snapshot.prompt_turn_id),
        "problem": "Brainstorm problem",
    }
    with pytest.raises(ProtocolError, match="invalid authoring proposal"):
        proposal_from_output({"proposal": bs_proposal}, snapshot)


@pytest.mark.asyncio
async def test_codex_exchange_decomposition(tmp_path: Path) -> None:
    from forge.agents.codex_gateway import CodexGateway, codex_account_identity
    from forge.domain.subscription_installations import CodexInstallationSpec

    snapshot, req = _make_decomposition_snapshot()
    proposal_data = _make_valid_decomposition_proposal(snapshot, req)

    thread_id = "thread-decomp"
    turn_id = "turn-decomp"

    received_requests: list[dict[str, object]] = []

    async def scripted_rpc(session, call_id, method, params, **_kwargs):
        received_requests.append({"id": call_id, "method": method, "params": params})
        if method == "initialize":
            return {}
        if method == "account/read":
            return {"account": {"type": "chatgpt", "email": "codex@example.invalid"}}
        if method == "model/list":
            return {
                "data": [
                    {
                        "id": "gpt-5.6-luna",
                        "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
                    }
                ]
            }
        if method == "config/read":
            return {"config": {}}
        if method == "thread/start":
            assert "decomposition" in params.get("baseInstructions", "")
            return {"thread": {"id": thread_id}, "model": "gpt-5.6-luna"}
        if method == "turn/start":
            assert "items" in params.get("outputSchema", {}).get("properties", {}).get(
                "proposal", {}
            ).get("properties", {})
            return {"turn": {"id": turn_id}}
        return {}

    inst = CodexInstallationSpec(
        client="codex_app_server",
        account=codex_account_identity("codex@example.invalid"),
        model="gpt-5.6-luna",
        effort="medium",
        executable=str(tmp_path / "codex.exe"),
        executable_digest="0" * 64,
        client_version="0.153.4",
        cwd=str(tmp_path),
        home=str(tmp_path),
        quota={"account": "dev-account", "pool": "default-pool"},
    )
    from forge.agents.codex_gateway import CodexInstallation

    runtime = CodexInstallation(
        executable=inst.executable,
        cwd=inst.cwd,
        model=inst.model,
        effort=inst.effort,
        client_home=inst.home,
        account=inst.account,
        executable_digest=inst.executable_digest,
        client_version=inst.client_version,
        quota_limit_id=inst.quota_limit_id,
        duration_seconds=10.0,
        disabled_mcp_servers=inst.disabled_mcp_servers,
    )
    from forge.domain.local_cli import LocalCliTrust

    gateway = CodexGateway(runtime, trust=LocalCliTrust.OPERATOR)
    gateway._rpc = scripted_rpc

    session = _ScriptedSession(
        [
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "tokenUsage": {"total": {"inputTokens": 12, "outputTokens": 20}},
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {
                        "id": "item-1",
                        "type": "agentMessage",
                        "text": json.dumps({"proposal": proposal_data}),
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "status": "completed"},
                },
            },
        ]
    )
    usage = AuthoringUsage()
    tools = AuthoringTools(
        BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())), 10, usage
    )
    result = await codex_exchange(session, gateway, snapshot, (), tools, usage)
    assert isinstance(result, DecompositionProposal)
    assert result.summary == "Decomposition breakdown"
    assert session.stdin_closed and session.settled


@pytest.mark.asyncio
async def test_gemini_exchange_decomposition(tmp_path: Path) -> None:
    snapshot, req = _make_decomposition_snapshot()
    proposal_data = _make_valid_decomposition_proposal(snapshot, req)

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
                    "sessionId": "gemini-decomp-thread",
                    "models": {"currentModelId": "gemini-2.5-pro"},
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "gemini-decomp-thread",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {
                            "type": "text",
                            "text": json.dumps({"proposal": proposal_data}),
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
    await tools.start()
    try:
        result = await gemini_exchange(
            session, snapshot, (), tools, usage, model="gemini-2.5-pro", cwd=str(tmp_path)
        )
    finally:
        await tools.close()

    assert isinstance(result, DecompositionProposal)
    assert result.summary == "Decomposition breakdown"
    # Verify session prompt sent schema with items
    prompt_call = next(item for item in session.sent if item.get("method") == "session/prompt")
    prompt_text = prompt_call["params"]["prompt"][0]["text"]
    assert "Decomposition" in prompt_text or "items" in prompt_text


@pytest.mark.asyncio
async def test_antigravity_exchange_decomposition(tmp_path: Path) -> None:
    snapshot, req = _make_decomposition_snapshot()
    proposal_data = _make_valid_decomposition_proposal(snapshot, req)

    session = _ScriptedSession(
        [
            {
                "event": "init",
                "init": {"model": "gemini-2.5-pro"},
                "conversation_id": "agy-decomp-thread",
            },
            {
                "event": "result",
                "result": {
                    "conversation_id": "agy-decomp-thread",
                    "status": "SUCCESS",
                    "usage": {"input_tokens": 15, "output_tokens": 25},
                    "structured_output": {"proposal": proposal_data},
                },
            },
        ]
    )
    usage = AuthoringUsage()
    tools = AuthoringTools(
        BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())), 10, usage
    )
    result = await antigravity_exchange(session, snapshot, (), tools, usage, model="gemini-2.5-pro")
    assert isinstance(result, DecompositionProposal)
    assert result.summary == "Decomposition breakdown"
    assert session.stdin_closed and session.settled


@pytest.mark.asyncio
async def test_claude_exchange_decomposition(tmp_path: Path) -> None:
    from forge.agents.claude_gateway import ClaudeGateway, ClaudeInstallation

    snapshot, req = _make_decomposition_snapshot()
    proposal_data = _make_valid_decomposition_proposal(snapshot, req)
    identity = str(snapshot.job_id)

    runtime = ClaudeInstallation(
        executable=str(tmp_path / "claude.exe"),
        cwd=str(tmp_path),
        model="claude-3-7-sonnet",
        effort="medium",
        client_home=str(tmp_path),
        account="dev-account",
        executable_digest="0" * 64,
        client_version="1.0.0",
        duration_seconds=10.0,
        quota_limit_types=frozenset(),
    )
    from forge.domain.local_cli import LocalCliTrust

    gateway = ClaudeGateway(runtime, trust=LocalCliTrust.OPERATOR)

    def mcp(request_id: str, message: dict[str, object]) -> dict[str, object]:
        return {
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": "mcp_message", "server_name": "forge", "message": message},
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
            {
                "type": "system",
                "subtype": "init",
                "session_id": identity,
                "model": "claude-3-7-sonnet",
            },
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": "forge_settings",
                    "response": {"applied": {"model": "claude-3-7-sonnet", "effort": "medium"}},
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "session_id": identity,
                "is_error": False,
                "usage": {"input_tokens": 10, "output_tokens": 15},
                "structured_output": {"proposal": proposal_data},
            },
        ]
    )
    usage = AuthoringUsage()
    tools = AuthoringTools(
        BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())), 10, usage
    )
    result = await claude_exchange(session, gateway, snapshot, (), tools, usage)
    assert isinstance(result, DecompositionProposal)
    assert result.summary == "Decomposition breakdown"
    assert session.stdin_closed and session.settled


@pytest.mark.asyncio
async def test_antigravity_exchange_foreign_turn_id(tmp_path: Path) -> None:
    snapshot, req = _make_decomposition_snapshot()
    proposal_data = _make_valid_decomposition_proposal(snapshot, req)
    proposal_data["turn_id"] = str(uuid4())

    session = _ScriptedSession(
        [
            {
                "event": "init",
                "init": {"model": "gemini-2.5-pro"},
                "conversation_id": "agy-foreign-turn",
            },
            {
                "event": "result",
                "result": {
                    "conversation_id": "agy-foreign-turn",
                    "status": "SUCCESS",
                    "usage": {"input_tokens": 5, "output_tokens": 5},
                    "structured_output": {"proposal": proposal_data},
                },
            },
        ]
    )
    usage = AuthoringUsage()
    tools = AuthoringTools(
        BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=())), 10, usage
    )
    with pytest.raises(ProtocolError, match="foreign authoring turn"):
        await antigravity_exchange(session, snapshot, (), tools, usage, model="gemini-2.5-pro")


@pytest.mark.asyncio
async def test_gateway_decomposition_inner_roundtrip(tmp_path: Path) -> None:
    from forge.agents.client_process import ClientLaunchSpec
    from forge.agents.epic_brainstorm_gateway import EpicBrainstormGateway

    snapshot, req = _make_decomposition_snapshot()
    proposal_data = _make_valid_decomposition_proposal(snapshot, req)

    # Launch spec running python command outputting valid decomposition proposal
    script = (
        "import sys, json; "
        "sys.stdin.readline(); "
        f"print(json.dumps({{'proposal': {json.dumps(proposal_data)}}}), flush=True)"
    )
    spec = ClientLaunchSpec(
        argv=(sys.executable, "-u", "-c", script),
        cwd=str(tmp_path),
        environment={},
        duration_seconds=5,
    )
    gateway = EpicBrainstormGateway(launch_spec=spec)

    async def is_cancelled() -> bool:
        return False

    class SimpleLifecycle:
        async def launch_intent(self, _id):
            pass

        async def started(self, _receipt):
            pass

        async def finished(self, _receipt, _result):
            pass

    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))
    result = await gateway.execute(
        snapshot, (), reader, cancelled=is_cancelled, lifecycle=SimpleLifecycle()
    )
    assert result.failure is None
    assert isinstance(result.proposal, DecompositionProposal)
    assert result.proposal.summary == "Decomposition breakdown"


@pytest.mark.asyncio
async def test_gateway_decomposition_process_failure(tmp_path: Path) -> None:
    from forge.agents.client_process import ClientLaunchSpec
    from forge.agents.epic_brainstorm_gateway import EpicBrainstormGateway

    snapshot, _req = _make_decomposition_snapshot()

    spec = ClientLaunchSpec(
        argv=(sys.executable, "-u", "-c", "import sys; sys.exit(2)"),
        cwd=str(tmp_path),
        environment={},
        duration_seconds=5,
    )
    gateway = EpicBrainstormGateway(launch_spec=spec)

    async def is_cancelled() -> bool:
        return False

    class SimpleLifecycle:
        async def launch_intent(self, _id):
            pass

        async def started(self, _receipt):
            pass

        async def finished(self, _receipt, _result):
            pass

    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))
    result = await gateway.execute(
        snapshot, (), reader, cancelled=is_cancelled, lifecycle=SimpleLifecycle()
    )
    assert result.failure == "process_failed"
    assert result.proposal is None
