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
async def test_roots_notification_is_advisory_and_preserves_handshake_and_authority(
    already_initialized,
):
    broker = _Broker()
    value = request()
    mcp = LocalCliMcp(value, broker)
    if already_initialized:
        await initialize(mcp)
    before = mcp.metadata_calls
    notification = {
        "jsonrpc": "2.0",
        "method": "notifications/roots/list_changed",
        "params": {"_meta": {}},
    }
    assert await mcp._handle(notification) is None
    assert mcp.metadata_calls == before + 1
    assert mcp.allowed == value.authorization.permitted_tools
    assert mcp.request == value and not broker.calls
    assert mcp._initialized is already_initialized
    call = {
        "jsonrpc": "2.0",
        "id": "read",
        "method": "tools/call",
        "params": {"name": "repository.read_file", "arguments": {"path": "README.md"}},
    }
    if not already_initialized:
        with pytest.raises(ProtocolError, match="MCP tools unavailable"):
            await mcp._handle(call)
        await initialize(mcp)
    assert (await mcp._handle(call))["result"]["isError"] is False
    assert len(broker.calls) == 1 and mcp.calls == 1
    await mcp.revoke()
    with pytest.raises(ProtocolError, match="closed or invalid"):
        await mcp._handle(notification)


async def test_roots_notifications_are_bounded_and_malformed_frames_still_fail():
    mcp = LocalCliMcp(request(), _Broker())
    frame = {"jsonrpc": "2.0", "method": "notifications/roots/list_changed"}
    for invalid in ({"id": None}, {"params": []}, {"method": "notifications/unknown"}):
        with pytest.raises(ProtocolError):
            await mcp._handle(frame | invalid)
    for _ in range(64):
        assert await mcp._handle(frame) is None
    with pytest.raises(ProtocolError, match="MCP metadata limit exceeded"):
        await mcp._handle(frame)
    assert not mcp._initialized and mcp.calls == 0


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
        exhausted = await mcp._handle(
            {
                **frame,
                "id": 2,
                "params": {"name": "repository.read_file", "arguments": arguments},
            }
        )
        assert exhausted["result"]["isError"] is True
        assert "Tool budget exhausted" in exhausted["result"]["content"][0]["text"]
    assert broker.calls == [] and mcp.calls == 1 and mcp.checks == 0


async def test_invalid_diff_scope_can_be_corrected_before_broker_dispatch():
    value = request()
    value = replace(
        value,
        authorization=replace(value.authorization, permitted_tools=frozenset({ToolName.GIT_DIFF})),
    )
    broker = _Broker()
    mcp = LocalCliMcp(value, broker)
    await initialize(mcp)
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "git.diff", "arguments": {"scope": "staged"}},
    }
    response = await mcp._handle(frame)
    assert response["result"]["isError"] is True
    assert "working_tree" in response["result"]["content"][0]["text"]
    assert broker.calls == [] and mcp.calls == 1 and mcp.checks == 0
    response = await mcp._handle(
        {**frame, "id": 2, "params": {"name": "git.diff", "arguments": {"scope": "snapshot"}}}
    )
    assert response["result"]["isError"] is False
    assert len(broker.calls) == 1 and mcp.calls == 2 and mcp.checks == 0


async def test_broker_denied_call_returns_tool_error_and_keeps_the_session_usable():
    """One refused callback must not tear down the whole provider attempt."""
    from forge.application.services.subscription_broker import BrokerDenied

    allowed = _Broker()
    refusals = []

    async def broker(call):
        if call.arguments.get("path") == "denied.md":
            refusals.append(call)
            raise BrokerDenied()
        return await allowed(call)

    mcp = LocalCliMcp(request(), broker)
    await initialize(mcp)
    denied = await mcp._handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "repository.read_file", "arguments": {"path": "denied.md"}},
    })
    assert denied["result"]["isError"] is True
    assert "refused" in denied["result"]["content"][0]["text"]
    assert refusals and mcp.calls == 1
    ok = await mcp._handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "repository.read_file", "arguments": {"path": "README.md"}},
    })
    assert ok["result"]["isError"] is False
    assert len(allowed.calls) == 1 and mcp.calls == 2


def _named_check_request():
    from test_gemini_gateway import _google_request

    return _google_request(tools=frozenset({ToolName.BUILD_RUN_NAMED_CHECK}))


def _check_call(ident):
    return {
        "jsonrpc": "2.0",
        "id": ident,
        "method": "tools/call",
        "params": {"name": "build.run_named_check", "arguments": {"command_name": "unit"}},
    }


async def test_slow_named_check_outlives_the_client_call_and_is_joined_not_rerun():
    """A client tool timeout must not cancel a long check or start a second one."""
    import asyncio

    release = asyncio.Event()
    calls = []

    async def broker(call):
        calls.append(call)
        await release.wait()
        return {"status": "succeeded", "operation_id": "op-1"}

    mcp = LocalCliMcp(_named_check_request(), broker, named_check_wait_seconds=0.05)
    await initialize(mcp)
    first = await mcp._handle(_check_call(1))
    assert first["result"]["isError"] is True
    assert "still running" in first["result"]["content"][0]["text"]
    second = await mcp._handle(_check_call(2))
    assert second["result"]["isError"] is True
    assert len(calls) == 1 and mcp.calls == 1 and mcp.checks == 1

    release.set()
    joined = await mcp._handle(_check_call(3))
    assert joined["result"]["isError"] is False
    assert "op-1" in joined["result"]["content"][0]["text"]
    assert len(calls) == 1

    rerun = await mcp._handle(_check_call(4))
    assert rerun["result"]["isError"] is False
    assert len(calls) == 2 and mcp.calls == 2 and mcp.checks == 2


async def test_close_lets_an_in_flight_check_finish_instead_of_cancelling_it():
    """The check settles through the broker with a receipt; nothing waits on restart recovery."""
    import asyncio

    release = asyncio.Event()
    cancelled = []

    async def broker(call):
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(call)
            raise
        return {"status": "succeeded", "operation_id": "op-1"}

    mcp = LocalCliMcp(_named_check_request(), broker, named_check_wait_seconds=0.01)
    await mcp.start()
    await initialize(mcp)
    first = await mcp._handle(_check_call(1))
    assert "still running" in first["result"]["content"][0]["text"]
    closing = asyncio.create_task(mcp.close())
    await asyncio.sleep(0.05)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, 1)
    assert cancelled == [] and not mcp._inflight


async def test_close_cancels_a_check_that_outlasts_the_finish_bound_and_awaits_it():
    import asyncio

    started, settled = asyncio.Event(), asyncio.Event()

    async def broker(call):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # stopping the container and settling the effect
            settled.set()
            raise

    mcp = LocalCliMcp(
        _named_check_request(), broker, named_check_wait_seconds=0.01, finish_seconds=0.05
    )
    await mcp.start()
    await initialize(mcp)
    await mcp._handle(_check_call(1))
    assert started.is_set()
    await mcp.close()
    assert settled.is_set()


async def test_close_reports_a_check_that_does_not_settle_in_time():
    import asyncio

    released = asyncio.Event()

    async def broker(call):
        while not released.is_set():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue  # does not settle until the test releases it
        return {"status": "cancelled"}

    mcp = LocalCliMcp(
        _named_check_request(),
        broker,
        named_check_wait_seconds=0.01,
        finish_seconds=0.01,
        settle_seconds=0.1,
    )
    await mcp.start()
    await initialize(mcp)
    await mcp._handle(_check_call(1))
    with pytest.raises(TimeoutError):
        await mcp.close()
    released.set()
    for task in list(mcp._inflight.values()):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_unjoined_check_failure_is_retrieved_even_when_close_times_out():
    """A check that fails after its soft reply must not log an unretrieved exception."""
    import asyncio
    import gc

    release = asyncio.Event()

    async def broker(call):
        if call.arguments["command_name"] == "fails":
            await asyncio.sleep(0.02)  # fails after the client already got its soft reply
            raise RuntimeError("runner failed")
        while not release.is_set():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue  # settles only after close() gives up
        return {"status": "cancelled"}

    def frame(ident, name):
        return {
            "jsonrpc": "2.0",
            "id": ident,
            "method": "tools/call",
            "params": {"name": "build.run_named_check", "arguments": {"command_name": name}},
        }

    mcp = LocalCliMcp(
        _named_check_request(),
        broker,
        named_check_wait_seconds=0.01,
        finish_seconds=0.01,
        settle_seconds=0.05,
    )
    await mcp.start()
    await initialize(mcp)
    for ident, name in ((1, "fails"), (2, "slow")):
        result = await mcp._handle(frame(ident, name))
        assert "still running" in result["result"]["content"][0]["text"]
    await asyncio.sleep(0.05)
    unretrieved = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))
    try:
        with pytest.raises(TimeoutError):
            await mcp.close()
        release.set()
        slow = [task for task in mcp._inflight.values() if not task.done()]
        for task in slow:
            task.cancel()  # wake the sleeping check so it observes the release
        await asyncio.wait(slow)
        mcp._inflight.clear()
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)
    assert not [c for c in unretrieved if "never retrieved" in str(c.get("message"))]


async def test_cancelled_close_stops_the_checks_it_was_waiting_for():
    import asyncio

    cancelled = asyncio.Event()

    async def broker(call):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    mcp = LocalCliMcp(_named_check_request(), broker, named_check_wait_seconds=0.01)
    await mcp.start()
    await initialize(mcp)
    await mcp._handle(_check_call(1))
    closing = asyncio.create_task(mcp.close())
    await asyncio.sleep(0.02)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await asyncio.wait_for(cancelled.wait(), 1)
