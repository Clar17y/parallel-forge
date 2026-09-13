"""The per-invocation MCP pipe retains bounded lifetime and no leaked peers."""

import asyncio
import json
import sys

import pytest
from forge.agents.client_process import ClientLaunchSpec, ClientProcessSupervisor
from forge.agents.gemini_gateway_mcp import GeminiMcpBridge
from forge.agents.subscription_protocol import ProtocolError


async def test_bridge_descriptor_matches_pinned_acp_stdio_schema():
    async def handler(frame):
        return {"jsonrpc": "2.0", "id": frame["id"], "result": {}}

    bridge = GeminiMcpBridge("fixture-bridge-secret", handler)
    await bridge.start()
    try:
        descriptor = bridge.descriptor()
        assert set(descriptor) == {"name", "command", "args", "env"}
        assert isinstance(descriptor["env"], list)
        assert descriptor["args"][:2] == ["-I", "-m"]
        assert {item["name"] for item in descriptor["env"]} == {
            "FORGE_GEMINI_BRIDGE_PORT",
            "FORGE_GEMINI_BRIDGE_SECRET",
        }
        assert "fixture-bridge-secret" not in repr(bridge)
    finally:
        await bridge.close()


async def test_bridge_close_finishes_authenticated_connections():
    async def handler(frame):
        return {"jsonrpc": "2.0", "id": frame["id"], "result": {}}

    bridge = GeminiMcpBridge("fixture-bridge-secret", handler)
    await bridge.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
    writer.write(b'fixture-bridge-secret\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
    await writer.drain()
    assert json.loads(await asyncio.wait_for(reader.readline(), 1))["id"] == 1
    try:
        await asyncio.wait_for(bridge.close(), 1)
        assert await asyncio.wait_for(reader.read(), 1) == b""
    finally:
        writer.close()
        await writer.wait_closed()


async def test_supervised_proxy_forwards_notifications_and_exits_with_stdin_open(tmp_path):
    notifications = []

    async def handler(frame):
        if "id" not in frame:
            notifications.append(frame["method"])
            return None
        return {"jsonrpc": "2.0", "id": frame["id"], "result": {}}

    bridge = GeminiMcpBridge("fixture-bridge-secret", handler)
    await bridge.start()
    descriptor = bridge.descriptor()
    session = await ClientProcessSupervisor().start(
        ClientLaunchSpec(
            argv=(sys.executable, *descriptor["args"]),
            cwd=str(tmp_path),
            environment={item["name"]: item["value"] for item in descriptor["env"]},
            allowed_environment=frozenset(item["name"] for item in descriptor["env"]),
            duration_seconds=10,
        )
    )
    try:
        await session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await session.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        reply = await session.receive()
        if reply is not None:
            assert reply["id"] == 1
            assert notifications == ["notifications/initialized"]
        await bridge.close()
        assert await asyncio.wait_for(session.receive(), 3) is None
    finally:
        await bridge.close()
        result = await session.close(completed=True)
    assert reply is not None, result.stderr
    assert result.stop_confirmed and result.return_code == 0, result.stderr


@pytest.mark.parametrize("bad", [b"wrong-secret\n", b"\n"])
async def test_unauthenticated_peer_cannot_call_handler_or_poison_bridge(bad):
    calls = []

    async def handler(frame):
        calls.append(frame)

    bridge = GeminiMcpBridge("fixture-bridge-secret", handler)
    await bridge.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
    writer.write(bad + b'{"id":1,"method":"ping"}\n')
    await writer.drain()
    try:
        assert await asyncio.wait_for(reader.read(), 1) == b""
        assert not calls
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(bridge.wait_failed(), 0.02)
    finally:
        writer.close()
        await writer.wait_closed()
        await bridge.close()


async def test_authenticated_protocol_failure_is_sanitized_and_signalled():
    async def handler(frame):
        raise RuntimeError("private provider payload must not escape")

    bridge = GeminiMcpBridge("fixture-bridge-secret", handler)
    await bridge.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
    writer.write(b'fixture-bridge-secret\n{"id":1,"method":"ping"}\n')
    await writer.drain()
    try:
        assert await asyncio.wait_for(reader.read(), 1) == b""
        with pytest.raises(ProtocolError, match="transport failed") as error:
            await bridge.wait_failed()
        assert "private" not in str(error.value)
    finally:
        writer.close()
        await writer.wait_closed()
        await bridge.close()


@pytest.mark.parametrize(
    "payload",
    [b'{"id":1,"id":2}\n', b'{"x":NaN}\n', b"x" * (256 * 1024 + 1) + b"\n"],
    ids=["duplicate_key", "nonfinite", "oversized"],
)
async def test_authenticated_malformed_or_oversized_frames_fail_closed(payload):
    calls = []

    async def handler(frame):
        calls.append(frame)

    bridge = GeminiMcpBridge("fixture-bridge-secret", handler)
    await bridge.start()
    _reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
    try:
        writer.write(b"fixture-bridge-secret\n" + payload)
        await writer.drain()
        with pytest.raises(ProtocolError, match="transport failed"):
            await asyncio.wait_for(bridge.wait_failed(), 2)
        assert not calls
    finally:
        writer.close()
        await writer.wait_closed()
        await bridge.close()


async def test_second_authenticated_connection_cannot_replace_current_peer():
    calls = []

    async def handler(frame):
        calls.append(frame["id"])
        return {"jsonrpc": "2.0", "id": frame["id"], "result": {}}

    bridge = GeminiMcpBridge("fixture-bridge-secret", handler)
    await bridge.start()
    first, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
    second, other = await asyncio.open_connection("127.0.0.1", bridge.port)
    try:
        writer.write(b'fixture-bridge-secret\n{"id":1}\n')
        await writer.drain()
        assert json.loads(await asyncio.wait_for(first.readline(), 1))["id"] == 1
        other.write(b'fixture-bridge-secret\n{"id":2}\n')
        await other.drain()
        assert await asyncio.wait_for(second.read(), 1) == b""
        writer.write(b'{"id":3}\n')
        await writer.drain()
        assert json.loads(await asyncio.wait_for(first.readline(), 1))["id"] == 3
        assert calls == [1, 3]
    finally:
        writer.close()
        other.close()
        await asyncio.gather(writer.wait_closed(), other.wait_closed())
        await bridge.close()
