from dataclasses import replace

import pytest
from forge.agents.local_cli_mcp import LocalCliMcp
from forge.agents.subscription_protocol import ProtocolError
from forge.domain.tool import ToolName
from test_antigravity_runtime import request
from test_codex_gateway import _Broker


async def initialize(mcp):
    return await mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )


@pytest.mark.parametrize("already_initialized", [False, True])
async def test_unsupported_method_returns_rpc_error_and_allows_normal_handshake(
    already_initialized,
):
    broker = _Broker()
    mcp = LocalCliMcp(request(), broker)
    if already_initialized:
        await initialize(mcp)
    response = await mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": "optional-probe",
            "method": "server/discover",
            "params": {"untrusted": "do not echo payloads"},
        }
    )
    assert response == {
        "jsonrpc": "2.0",
        "id": "optional-probe",
        "error": {"code": -32601, "message": "Method not found"},
    }
    assert broker.calls == [] and mcp.calls == 0
    if not already_initialized:
        await initialize(mcp)
    tools = await mcp._handle({"jsonrpc": "2.0", "id": "list", "method": "tools/list"})
    assert [tool["name"] for tool in tools["result"]["tools"]] == ["repository.read_file"]
    await mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": "read",
            "method": "tools/call",
            "params": {"name": "repository.read_file", "arguments": {"path": "README.md"}},
        }
    )
    assert len(broker.calls) == 1 and mcp.calls == 1


@pytest.mark.parametrize("invalid", [{"id": True}, {"method": []}, {"method": ""}, {"params": []}])
async def test_method_not_found_does_not_accept_malformed_requests(invalid):
    broker = _Broker()
    mcp = LocalCliMcp(request(), broker)
    frame = {"jsonrpc": "2.0", "id": 1, "method": "client/optional_capability"} | invalid
    with pytest.raises(ProtocolError):
        await mcp._handle(frame)
    assert broker.calls == [] and mcp.calls == 0


async def test_unknown_methods_still_consume_the_metadata_budget():
    mcp = LocalCliMcp(request(), _Broker())
    for ident in range(64):
        reply = await mcp._handle(
            {"jsonrpc": "2.0", "id": ident, "method": "client/optional_capability"}
        )
        assert reply["error"]["code"] == -32601
    with pytest.raises(ProtocolError, match="MCP metadata limit exceeded"):
        await initialize(mcp)
    assert mcp.calls == 0


async def test_optional_extensions_do_not_authorize_tools_or_reinitialization():
    broker = _Broker()
    mcp = LocalCliMcp(request(), broker)
    await mcp._handle({"jsonrpc": "2.0", "id": 1, "method": "client/optional_capability"})
    tool = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "repository.read_file", "arguments": {"path": "README.md"}},
    }
    with pytest.raises(ProtocolError, match="MCP tools unavailable"):
        await mcp._handle(tool)
    with pytest.raises(ProtocolError, match="unsupported MCP request"):
        await mcp._handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    await initialize(mcp)
    with pytest.raises(ProtocolError, match="unsupported MCP request"):
        await initialize(mcp)
    await mcp.revoke()
    with pytest.raises(ProtocolError, match="closed or invalid"):
        await mcp._handle(tool)
    assert broker.calls == [] and broker.revoked


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        {"command": "unit"},
        {"command_name": None},
        {"command_name": "unit", "timeout": 10},
    ],
)
async def test_invalid_named_check_arguments_can_be_repaired_without_dispatch(arguments):
    value = request()
    value = replace(
        value,
        authorization=replace(
            value.authorization, permitted_tools=frozenset({ToolName.BUILD_RUN_NAMED_CHECK})
        ),
    )
    broker = _Broker()
    mcp = LocalCliMcp(value, broker)
    await initialize(mcp)
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "build.run_named_check", "arguments": arguments},
    }
    response = await mcp._handle(frame)
    assert response["result"]["isError"] is True
    assert "command_name" in response["result"]["content"][0]["text"]
    assert broker.calls == [] and mcp.calls == 1 and mcp.checks == 0
    response = await mcp._handle(
        {
            **frame,
            "id": 2,
            "params": {"name": "build.run_named_check", "arguments": {"command_name": "unit"}},
        }
    )
    assert response["result"]["isError"] is False
    assert len(broker.calls) == 1 and mcp.calls == 2 and mcp.checks == 1


async def test_argument_errors_keep_permission_checks_and_consume_the_tool_budget():
    value = request()
    value = replace(
        value, task=replace(value.task, budget=replace(value.task.budget, max_tool_calls=1))
    )
    broker = _Broker()
    mcp = LocalCliMcp(value, broker)
    await initialize(mcp)
    frame = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
    with pytest.raises(ProtocolError, match="task permission"):
        await mcp._handle({**frame, "params": {"name": "build.run_named_check", "arguments": {}}})
    assert mcp.calls == 0
    response = await mcp._handle(
        {**frame, "params": {"name": "repository.read_file", "arguments": {"wrong": "private"}}}
    )
    assert response["result"]["isError"] is True
    assert "private" not in response["result"]["content"][0]["text"]
    for arguments in ({}, {"path": "README.md"}):
        with pytest.raises(ProtocolError, match="tool budget exhausted"):
            await mcp._handle(
                {
                    **frame,
                    "id": 2,
                    "params": {"name": "repository.read_file", "arguments": arguments},
                }
            )
    assert broker.calls == [] and mcp.calls == 1 and mcp.checks == 0
