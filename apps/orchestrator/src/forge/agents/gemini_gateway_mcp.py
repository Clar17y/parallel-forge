"""Authenticated, bounded MCP transport owned by one supervised Gemini invocation."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import socket
import sys
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from forge.agents.subscription_protocol import ProtocolError, json_value, parse_json

MAX_FRAME_BYTES = 256 * 1024
Handler = Callable[[Mapping[str, object]], Awaitable[Mapping[str, object] | None]]


@dataclass(slots=True)
class GeminiMcpBridge:
    secret: str = field(repr=False)
    handler: Handler = field(repr=False)
    server: asyncio.Server | None = field(default=None, init=False, repr=False)
    port: int | None = field(default=None, init=False)
    _peers: dict[asyncio.Task[None], asyncio.StreamWriter] = field(
        default_factory=dict, init=False, repr=False
    )
    _authenticated: asyncio.StreamWriter | None = field(default=None, init=False, repr=False)
    _failed: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _closing: bool = field(default=False, init=False)
    _close_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.secret) is not str
            or not 16 <= len(self.secret) <= 256
            or not self.secret.isascii()
            or any(ord(char) < 33 for char in self.secret)
            or not callable(self.handler)
        ):
            raise ValueError("invalid private MCP bridge configuration")

    async def start(self) -> None:
        if self.server is not None or self._closing:
            raise RuntimeError("bridge cannot be restarted")
        self.server = await asyncio.start_server(self._peer, "127.0.0.1", 0, limit=MAX_FRAME_BYTES)
        self.port = self.server.sockets[0].getsockname()[1]

    def descriptor(self) -> dict[str, object]:
        if self.port is None or self._closing:
            raise RuntimeError("bridge is not active")
        # ACP v1's stdio variant has no `type` field and uses named env pairs.
        return {
            "name": "forge",
            "command": sys.executable,
            "args": ["-I", "-m", "forge.agents.gemini_gateway_mcp"],
            "env": [
                {"name": "FORGE_GEMINI_BRIDGE_PORT", "value": str(self.port)},
                {"name": "FORGE_GEMINI_BRIDGE_SECRET", "value": self.secret},
            ],
        }

    async def wait_failed(self) -> None:
        await self._failed.wait()
        raise ProtocolError("authenticated Gemini MCP transport failed")

    async def _peer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        admitted = False
        if self._closing or len(self._peers) >= 8:
            writer.close()
            return
        self._peers[task] = writer
        try:
            async with asyncio.timeout(1):
                token = await reader.readline()
            if (
                self._closing
                or self._authenticated is not None
                or not hmac.compare_digest(token, self.secret.encode() + b"\n")
            ):
                return
            admitted = True
            self._authenticated = writer
            while not self._closing and (line := await reader.readline()):
                if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
                    raise ProtocolError("Gemini MCP frame exceeds bound")
                reply = await self.handler(parse_json(line.decode("utf-8")))
                if reply is None:
                    continue
                encoded = (
                    json.dumps(json_value(reply), separators=(",", ":"), allow_nan=False).encode()
                    + b"\n"
                )
                if len(encoded) > MAX_FRAME_BYTES:
                    raise ProtocolError("Gemini MCP response exceeds bound")
                writer.write(encoded)
                await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never expose frames or authentication material
            if admitted and not self._closing:
                self._failed.set()
        finally:
            if self._authenticated is writer:
                self._authenticated = None
            self._peers.pop(task, None)
            writer.close()
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except Exception:  # noqa: BLE001 - always abort a socket that cannot finish closing
                writer.transport.abort()

    async def close(self) -> None:
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._shutdown())
        interrupted = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                interrupted = True
        self._close_task.result()
        if interrupted:
            raise asyncio.CancelledError

    async def _shutdown(self) -> None:
        if self.server is not None:
            self.server.close()
        peers = tuple(self._peers.items())
        for task, writer in peers:
            writer.transport.abort()
            task.cancel()
        await asyncio.gather(*(task for task, _ in peers), return_exceptions=True)
        if self.server is not None:
            await self.server.wait_closed()


def main() -> int:
    """Run as the official client's child, contained in its supervisor process tree.

    The stdin pump is a daemon thread: TCP closure can terminate the proxy even
    while the client still holds stdin open. Notifications need no synthetic reply.
    """
    try:
        port = int(os.environ["FORGE_GEMINI_BRIDGE_PORT"])
        secret = os.environ["FORGE_GEMINI_BRIDGE_SECRET"]
        if (
            not 1 <= port <= 65535
            or not 16 <= len(secret) <= 256
            or not secret.isascii()
            or "\n" in secret
        ):
            return 1
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.settimeout(None)
            connection.sendall(secret.encode() + b"\n")

            def input_pump() -> None:
                pending = bytearray()
                try:
                    # A blocked daemon must not hold BufferedReader's lock at
                    # interpreter shutdown. Read only the raw OS descriptor.
                    while chunk := os.read(sys.stdin.fileno(), 4096):
                        pending.extend(chunk)
                        while (boundary := pending.find(b"\n")) >= 0:
                            if boundary + 1 > MAX_FRAME_BYTES:
                                return
                            connection.sendall(pending[: boundary + 1])
                            del pending[: boundary + 1]
                        if len(pending) >= MAX_FRAME_BYTES:
                            return
                except OSError:
                    pass
                finally:
                    with contextlib.suppress(OSError):
                        connection.shutdown(socket.SHUT_RDWR)

            threading.Thread(target=input_pump, daemon=True, name="forge-mcp-stdin").start()
            with connection.makefile("rb") as replies:
                while line := replies.readline(MAX_FRAME_BYTES + 1):
                    if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
                        return 1
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
        return 0
    except KeyError, OSError, ValueError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
