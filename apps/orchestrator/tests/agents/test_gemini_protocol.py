import asyncio
import json

import pytest
from forge.agents.gemini_protocol import GeminiDualChannelProtocol
from forge.agents.subscription_protocol import ProtocolError

MAX_FRAME_BYTES = 1_048_576


def frame(method, params):
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": params})


def acp(codec, native, marker, session="s"):
    codec.receive_acp(
        frame(
            "session/update",
            {
                "sessionId": session,
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": native,
                    "status": "in_progress",
                    "title": "Forge",
                    "content": [],
                    "locations": [],
                    "kind": "other",
                },
            },
        )
    )
    codec.receive_acp(
        frame(
            "session/update",
            {
                "sessionId": session,
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": native,
                    "status": "completed",
                    "title": "Forge",
                    "content": [{"type": "content", "content": {"type": "text", "text": marker}}],
                    "locations": [],
                    "kind": "other",
                },
            },
        )
    )


def request(i, name, arguments, meta=None):
    p = {"name": name, "arguments": arguments}
    if meta is not None:
        p["_meta"] = {"progressToken": meta}
    return json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": p})


@pytest.mark.asyncio
@pytest.mark.parametrize("progress_token", [0, 12, -7, 1.0, "12"])
async def test_progress_token_is_transport_metadata_and_accepts_integer_values(progress_token):
    calls = []

    async def broker(call):
        calls.append(call)
        return {"status": "succeeded"}

    codec = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    line = request(1, "repository.read_file", {"path": "x"}, progress_token)
    response = await codec.receive_mcp(line)
    assert response["id"] == 1
    assert await codec.receive_mcp(line) == response
    assert codec.invocation_count == 1
    assert calls == []


@pytest.mark.asyncio
async def test_actual_mcp_result_round_trips_actual_acp_content_before_effect():
    calls = []

    async def broker(call):
        calls.append(call)
        return {"status": "succeeded"}

    c = GeminiDualChannelProtocol(session_id="s", turn_id="trusted-turn", broker=broker)
    prepared = await c.receive_mcp(request(1, "repository.read_file", {"path": "x"}, "p"))
    marker = prepared["result"]["content"][0]["text"]
    assert calls == []
    acp(c, "native-a", marker)
    token = json.loads(marker)["nonce"]
    result = await c.receive_mcp(request("2", "forge_execute_receipt", {"token": token}))
    receipt = json.loads(result["result"]["content"][0]["text"])
    assert receipt["receipt"]["status"] == "succeeded"
    acp(c, "native-execute-a", result["result"]["content"][0]["text"])
    assert calls[0].turn_id == "trusted-turn"


@pytest.mark.asyncio
async def test_reversed_distinct_completions_and_native_replay_concurrent_execute_once():
    calls = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def broker(call):
        calls.append(call)
        entered.set()
        await release.wait()
        return {"status": "succeeded"}

    c = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker, call_limit=12)
    a = await c.receive_mcp(request(1, "repository.read_file", {"path": "a"}))
    b = await c.receive_mcp(request(2, "repository.read_file", {"path": "b"}))
    ma = a["result"]["content"][0]["text"]
    mb = b["result"]["content"][0]["text"]
    acp(c, "n2", mb)
    acp(c, "n1", ma)
    # Fresh marker on same native and same operation is a receipt alias.
    replay = await c.receive_mcp(request(3, "repository.read_file", {"path": "a"}))
    mr = replay["result"]["content"][0]["text"]
    acp(c, "n1", mr)
    tokena = json.loads(ma)["nonce"]
    tokenr = json.loads(mr)["nonce"]
    one = asyncio.create_task(c.receive_mcp(request(4, "forge_execute_receipt", {"token": tokena})))
    two = asyncio.create_task(c.receive_mcp(request(5, "forge_execute_receipt", {"token": tokenr})))
    await entered.wait()
    release.set()
    await one
    await two
    assert len(calls) == 1 and calls[0].call_key


@pytest.mark.asyncio
async def test_transport_replay_changed_body_foreign_and_conflicting_native_deny():
    async def broker(call):
        return {"status": "succeeded"}

    c = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    first = await c.receive_mcp(request(1, "repository.read_file", {"path": "x"}))
    assert await c.receive_mcp(request(1, "repository.read_file", {"path": "x"})) == first
    with pytest.raises(ProtocolError):
        await c.receive_mcp(request(1, "repository.read_file", {"path": "y"}))
    marker = first["result"]["content"][0]["text"]
    acp(c, "n", marker)
    second = await c.receive_mcp(request(2, "repository.read_file", {"path": "y"}))
    with pytest.raises(ProtocolError):
        acp(c, "n", second["result"]["content"][0]["text"])
    with pytest.raises(ProtocolError):
        acp(c, "n", marker, session="foreign")


@pytest.mark.asyncio
async def test_close_cancel_and_bounds_fail_closed():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def broker(call):
        entered.set()
        await release.wait()
        return {"status": "succeeded"}

    c = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker, call_limit=3)
    p = await c.receive_mcp(request(1, "repository.read_file", {"path": "x"}))
    marker = p["result"]["content"][0]["text"]
    acp(c, "n", marker)
    task = asyncio.create_task(
        c.receive_mcp(request(2, "forge_execute_receipt", {"token": json.loads(marker)["nonce"]}))
    )
    await entered.wait()
    c.close()
    release.set()
    assert (
        json.loads((await task)["result"]["content"][0]["text"])["receipt"]["status"] == "not_ready"
    )
    with pytest.raises(ProtocolError):
        c.receive_acp("x" * 1_048_577)
    with pytest.raises(ProtocolError):
        await c.receive_mcp(request(3, "repository.read_file", {"path": "x"}))


def test_malformed_actual_shape_and_missing_start_deny():
    c = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=lambda _: None)
    p = c.prepare("repository.read_file", {"path": "x"})
    with pytest.raises(ProtocolError):
        c.receive_acp(
            frame(
                "session/update",
                {
                    "sessionId": "s",
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "n",
                        "status": "completed",
                        "kind": "other",
                        "content": [],
                    },
                },
            )
        )
    with pytest.raises(ProtocolError):
        c.receive_acp(
            frame(
                "session/update",
                {
                    "sessionId": "s",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "n",
                        "status": "in_progress",
                        "kind": "other",
                        "content": [],
                    },
                },
            )
        )
    assert p.marker


def test_starts_are_bounded_and_duplicate_start_is_idempotent():
    c = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=lambda _: None, call_limit=2)
    start = lambda n: frame(
        "session/update",
        {
            "sessionId": "s",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": n,
                "status": "in_progress",
                "title": "Forge",
                "content": [],
                "locations": [],
                "kind": "other",
            },
        },
    )
    c.receive_acp(start("a"))
    c.receive_acp(start("a"))
    c.receive_acp(start("b"))
    with pytest.raises(ProtocolError):
        c.receive_acp(start("c"))


@pytest.mark.asyncio
async def test_concurrent_same_transport_id_reserves_before_broker_and_changed_body_denies():
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def broker(call):
        calls.append(call)
        entered.set()
        await release.wait()
        return {"status": "succeeded"}

    c = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    proposal = await c.receive_mcp(request(1, "repository.read_file", {"path": "x"}))
    marker = proposal["result"]["content"][0]["text"]
    acp(c, "n", marker)
    token = json.loads(marker)["nonce"]
    line = request(2, "forge_execute_receipt", {"token": token})
    one = asyncio.create_task(c.receive_mcp(line))
    two = asyncio.create_task(c.receive_mcp(line))
    await entered.wait()
    with pytest.raises(ProtocolError):
        await c.receive_mcp(request(2, "forge_execute_receipt", {"token": "changed"}))
    release.set()
    await one
    await two
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_not_ready_and_receipt_completion_require_issued_exact_auxiliary():
    async def broker(call):
        return {"status": "succeeded"}

    c = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    proposal = await c.receive_mcp(request(1, "repository.read_file", {"path": "x"}))
    marker = proposal["result"]["content"][0]["text"]
    token = json.loads(marker)["nonce"]
    early = await c.receive_mcp(request(2, "forge_execute_receipt", {"token": token}))
    acp(c, "execute-n", early["result"]["content"][0]["text"])
    acp(c, "n", marker)
    settled = await c.receive_mcp(request(3, "forge_execute_receipt", {"token": token}))
    acp(c, "execute-n-settled", settled["result"]["content"][0]["text"])


@pytest.mark.asyncio
async def test_distinct_native_ids_with_identical_arguments_each_execute_once():
    calls = []

    async def broker(call):
        calls.append(call)
        return {"status": "succeeded"}

    c = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    first = await c.receive_mcp(request(1, "repository.read_file", {"path": "x"}))
    second = await c.receive_mcp(request(2, "repository.read_file", {"path": "x"}))
    for native, response, execution in (("prepare-a", first, 3), ("prepare-b", second, 4)):
        marker = response["result"]["content"][0]["text"]
        acp(c, native, marker)
        executed = await c.receive_mcp(
            request(execution, "forge_execute_receipt", {"token": json.loads(marker)["nonce"]})
        )
        acp(c, f"execute-{native}", executed["result"]["content"][0]["text"])
    assert len(calls) == 2 and calls[0].call_key != calls[1].call_key


@pytest.mark.asyncio
async def test_oversized_broker_receipt_is_uncertain_and_not_reexecuted():
    calls = []

    async def broker(call):
        calls.append(call)
        return {"status": "succeeded", "output": "x" * 1_048_576}

    c = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    proposed = await c.receive_mcp(request(1, "repository.read_file", {"path": "x"}))
    marker = proposed["result"]["content"][0]["text"]
    acp(c, "prepare", marker)
    token = json.loads(marker)["nonce"]
    with pytest.raises(ProtocolError):
        await c.receive_mcp(request(2, "forge_execute_receipt", {"token": token}))
    retry = await c.receive_mcp(request(3, "forge_execute_receipt", {"token": token}))
    assert json.loads(retry["result"]["content"][0]["text"])["receipt"]["status"] == "not_ready"
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("auxiliary_first", [False, True])
async def test_native_identity_cannot_change_between_proposal_and_auxiliary(auxiliary_first):
    calls = []

    async def broker(call):
        calls.append(call)
        return {"status": "succeeded"}

    codec = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    prepared = await codec.receive_mcp(request(1, "repository.read_file", {"path": "x"}))
    marker = prepared["result"]["content"][0]["text"]
    early = await codec.receive_mcp(
        request(2, "forge_execute_receipt", {"token": json.loads(marker)["nonce"]})
    )
    auxiliary = early["result"]["content"][0]["text"]
    first, second = (auxiliary, marker) if auxiliary_first else (marker, auxiliary)
    acp(codec, "one-native-id", first)
    with pytest.raises(ProtocolError):
        acp(codec, "one-native-id", second)
    assert calls == []


@pytest.mark.asyncio
async def test_escaped_receipt_must_fit_the_complete_outgoing_mcp_frame():
    calls = []

    async def broker(call):
        calls.append(call)
        # The receipt alone fits; JSON escaping inside the MCP text does not.
        return {"status": "succeeded", "output": '"' * 300_000}

    codec = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    proposed = await codec.receive_mcp(request(1, "repository.read_file", {"path": "x"}))
    marker = proposed["result"]["content"][0]["text"]
    acp(codec, "prepare", marker)
    token = json.loads(marker)["nonce"]
    with pytest.raises(ProtocolError):
        await codec.receive_mcp(request(2, "forge_execute_receipt", {"token": token}))
    retry = await codec.receive_mcp(request(3, "forge_execute_receipt", {"token": token}))
    assert json.loads(retry["result"]["content"][0]["text"])["receipt"]["status"] == "not_ready"
    assert len(json.dumps(retry).encode()) <= MAX_FRAME_BYTES
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancelled_caller_can_rejoin_the_same_pending_transport_without_new_effect():
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def broker(call):
        calls.append(call)
        entered.set()
        await release.wait()
        return {"status": "succeeded"}

    codec = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    proposed = await codec.receive_mcp(request(1, "repository.read_file", {"path": "x"}))
    marker = proposed["result"]["content"][0]["text"]
    acp(codec, "prepare", marker)
    line = request(2, "forge_execute_receipt", {"token": json.loads(marker)["nonce"]})
    original = asyncio.create_task(codec.receive_mcp(line))
    await entered.wait()
    original.cancel()
    with pytest.raises(asyncio.CancelledError):
        await original
    replay = asyncio.create_task(codec.receive_mcp(line))
    release.set()
    result = await replay
    assert json.loads(result["result"]["content"][0]["text"])["receipt"]["status"] == "succeeded"
    assert len(calls) == 1 and codec.invocation_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "cancelled", "deep", "nonfinite", "nonobject"])
async def test_uncertain_broker_result_never_reissues_effect(failure):
    calls = []

    async def broker(call):
        calls.append(call)
        if failure == "exception":
            raise RuntimeError("fake broker failure")
        if failure == "cancelled":
            raise asyncio.CancelledError
        if failure == "nonobject":
            return []
        if failure == "nonfinite":
            return {"usage": float("nan")}
        result = {}
        for _ in range(30):
            result = {"nested": result}
        return result

    codec = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    proposed = await codec.receive_mcp(request(1, "repository.read_file", {"path": "x"}))
    marker = proposed["result"]["content"][0]["text"]
    acp(codec, "prepare", marker)
    token = json.loads(marker)["nonce"]
    line = request(2, "forge_execute_receipt", {"token": token})
    if failure == "nonobject":
        result = await codec.receive_mcp(line)
        assert (
            json.loads(result["result"]["content"][0]["text"])["receipt"]["status"] == "not_ready"
        )
    else:
        error = asyncio.CancelledError if failure == "cancelled" else RuntimeError
        with pytest.raises(error):
            await codec.receive_mcp(line)
        with pytest.raises(error):
            await codec.receive_mcp(line)
    retry = await codec.receive_mcp(request(3, "forge_execute_receipt", {"token": token}))
    assert json.loads(retry["result"]["content"][0]["text"])["receipt"]["status"] == "not_ready"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_restart_loses_nonce_authority_and_native_rebinding_is_denied():
    calls = []

    async def broker(call):
        calls.append(call)
        return {"status": "succeeded"}

    old = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    proposed = await old.receive_mcp(request(1, "repository.read_file", {"path": "x"}))
    marker = proposed["result"]["content"][0]["text"]
    acp(old, "prepare", marker)
    with pytest.raises(ProtocolError):
        acp(old, "rebound", marker)
    old.close()
    restarted = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    with pytest.raises(ProtocolError):
        acp(restarted, "prepare", marker)
    result = await restarted.receive_mcp(
        request(2, "forge_execute_receipt", {"token": json.loads(marker)["nonce"]})
    )
    assert json.loads(result["result"]["content"][0]["text"])["receipt"]["status"] == "not_ready"
    assert calls == []


@pytest.mark.asyncio
async def test_prepare_and_execute_consume_finite_invocations_but_transport_replay_does_not():
    calls = []

    async def broker(call):
        calls.append(call)
        return {"status": "succeeded"}

    codec = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker, call_limit=2)
    prepare = request(1, "repository.read_file", {"path": "x"})
    proposed = await codec.receive_mcp(prepare)
    assert await codec.receive_mcp(prepare) == proposed
    assert codec.invocation_count == 1
    marker = proposed["result"]["content"][0]["text"]
    acp(codec, "prepare", marker)
    execute = request(2, "forge_execute_receipt", {"token": json.loads(marker)["nonce"]})
    result = await codec.receive_mcp(execute)
    assert await codec.receive_mcp(execute) == result
    with pytest.raises(ProtocolError):
        await codec.receive_mcp(request(3, "repository.read_file", {"path": "y"}))
    assert codec.invocation_count == 2 and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [1, "1", 1.0, -2, 0])
async def test_mcp_number_and_string_identifiers_round_trip(identity):
    codec = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=lambda _: None)
    line = request(identity, "repository.read_file", {"path": "x"})
    result = await codec.receive_mcp(line)
    assert result["id"] == identity
    assert await codec.receive_mcp(line) == result
    assert codec.invocation_count == 1


@pytest.mark.asyncio
async def test_string_and_numeric_transport_ids_are_distinct():
    codec = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=lambda _: None)
    number = await codec.receive_mcp(request(1, "repository.read_file", {"path": "x"}))
    string = await codec.receive_mcp(request("1", "repository.read_file", {"path": "x"}))
    assert number["result"] != string["result"]
    assert codec.invocation_count == 2


@pytest.mark.asyncio
async def test_forged_or_changed_auxiliary_content_cannot_mint_authority():
    calls = []

    async def broker(call):
        calls.append(call)
        return {"status": "succeeded"}

    codec = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    prepared = await codec.receive_mcp(request(1, "repository.read_file", {"path": "x"}))
    marker = prepared["result"]["content"][0]["text"]
    token = json.loads(marker)["nonce"]
    early = await codec.receive_mcp(request(2, "forge_execute_receipt", {"token": token}))
    text = early["result"]["content"][0]["text"]
    forged = json.loads(text)
    forged["receipt"] = {"status": "succeeded"}
    with pytest.raises(ProtocolError):
        acp(codec, "forged", json.dumps(forged))
    acp(codec, "auxiliary", text)
    acp(codec, "auxiliary", text)
    assert calls == []
    acp(codec, "prepare", marker)
    settled = await codec.receive_mcp(request(3, "forge_execute_receipt", {"token": token}))
    with pytest.raises(ProtocolError):
        acp(codec, "auxiliary", settled["result"]["content"][0]["text"])
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_prepared_arguments_are_immutable_and_native_keys_include_trusted_session_and_turn():
    calls = []

    async def broker(call):
        calls.append(call)
        return {"status": "succeeded"}

    for session, turn in (("s", "t"), ("other", "t"), ("s", "next-turn")):
        codec = GeminiDualChannelProtocol(session_id=session, turn_id=turn, broker=broker)
        arguments = {"path": "original"}
        prepared = codec.prepare("repository.read_file", arguments)
        arguments["path"] = "changed"
        acp(codec, "same-native", prepared.marker, session=session)
        await codec.receive_mcp(request(1, "forge_execute_receipt", {"token": prepared.token}))
    assert [call.arguments["path"] for call in calls] == ["original"] * 3
    assert len({call.call_key for call in calls}) == 3
    assert all(len(call.call_key) == 64 for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line",
    [
        '{"jsonrpc":"2.0","jsonrpc":"2.0"}',
        '{"jsonrpc":"2.0","id":NaN,"method":"tools/call","params":{}}',
        '{"jsonrpc":"2.0","id":Infinity,"method":"tools/call","params":{}}',
        "[]",
        "{",
        "x" * (MAX_FRAME_BYTES + 1),
        "\ud800",
        request(True, "repository.read_file", {"path": "x"}),
        request(None, "repository.read_file", {"path": "x"}),
        request(1, "shell.execute", {"command": "denied"}),
        request(1, "repository.read_file", {"path": 3}),
        request(1, "repository.read_file", {"path": "x", "extra": "denied"}),
        request(1, "repository.read_file", {"path": "x"}, True),
        request(1.5, "repository.read_file", {"path": "x"}),
        request(1, "repository.read_file", {"path": "x"}, 1.5),
    ],
    ids=[
        "duplicate",
        "nan",
        "infinite",
        "array",
        "truncated",
        "oversize",
        "unicode",
        "bool-id",
        "null-id",
        "unknown-tool",
        "bad-args",
        "extra-args",
        "bool-progress",
        "fractional-id",
        "fractional-progress",
    ],
)
async def test_malformed_or_unregistered_mcp_input_is_rejected_before_broker(line):
    calls = []

    async def broker(call):
        calls.append(call)
        return {"status": "succeeded"}

    codec = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    with pytest.raises(ProtocolError):
        await codec.receive_mcp(line)
    assert calls == []


async def test_closed_codec_drains_cancelled_effect_reconciliation():
    entered, release, reconciled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def broker(call):
        entered.set()
        try:
            await release.wait()
            return {"status": "succeeded"}
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            reconciled.set()
            raise

    codec = GeminiDualChannelProtocol(session_id="s", turn_id="t", broker=broker)
    prepared = codec.prepare("repository.read_file", {"path": "x"})
    acp(codec, "native", prepared.marker)
    pending = asyncio.create_task(
        codec.receive_mcp(request(1, "forge_execute_receipt", {"token": prepared.token}))
    )
    await asyncio.wait_for(entered.wait(), 1)
    try:
        codec.close()
        await asyncio.wait_for(codec.drain(), 1)
        assert reconciled.is_set()
        assert isinstance(
            (await asyncio.gather(pending, return_exceptions=True))[0], asyncio.CancelledError
        )
        with pytest.raises(ProtocolError):
            await codec.receive_mcp(request(2, "repository.read_file", {"path": "later"}))
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
