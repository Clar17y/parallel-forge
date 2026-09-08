"""Bounded, authenticated SSE framing over durable event pages."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from time import monotonic

from forge.application.services.auth import AuthenticationError
from forge.application.services.public_data import public_payload
from forge.domain.event import RunEvent, thaw_payload

MAX_CURSOR = 2**63 - 1
_CURSOR = re.compile(r"[0-9]{1,19}\Z", re.ASCII)
_EVENT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,95}\Z", re.ASCII)


def parse_cursor(last_event_id: str | None, after: str | None) -> int:
    value = last_event_id if last_event_id is not None else after
    if value is None:
        return 0
    if not _CURSOR.fullmatch(value) or int(value) > MAX_CURSOR:
        raise ValueError("invalid event cursor")
    return int(value)


def encode_event(event: RunEvent) -> bytes:
    if event.sequence is None or not 1 <= event.sequence <= MAX_CURSOR:
        raise ValueError("invalid persisted event sequence")
    name = event.event_type if _EVENT_NAME.fullmatch(event.event_type) else "run.event"
    data = {
        "event_id": str(event.event_id),
        "run_id": str(event.run_id),
        "sequence": event.sequence,
        "run_version": event.run_version,
        "event_type": event.event_type,
        "actor_class": event.actor_class,
        "actor_id": str(event.actor_id) if event.actor_id else None,
        "payload_schema_version": event.payload_schema_version,
        "payload": public_payload(thaw_payload(event.payload)),
        "occurred_at": event.occurred_at.isoformat(),
    }
    encoded = json.dumps(data, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return f"id: {event.sequence}\nevent: {name}\ndata: {encoded}\n\n".encode()


async def event_stream(
    *,
    cursor: int,
    read_page: Callable[[int], Awaitable[Sequence[RunEvent]]],
    check_session: Callable[[], Awaitable[object]],
    disconnected: Callable[[], Awaitable[bool]],
    shutdown: asyncio.Event,
    poll_seconds: float = 0.5,
    heartbeat_seconds: float = 15.0,
) -> AsyncIterator[bytes]:
    """Keep polling read-only state; a stream never refreshes session expiry."""
    next_heartbeat = monotonic() + heartbeat_seconds
    while not shutdown.is_set():
        if await disconnected():
            return
        try:
            await check_session()
        except AuthenticationError:
            return
        events = await read_page(cursor)
        # Recheck after the database wait, before releasing any event bytes.
        if shutdown.is_set() or await disconnected():
            return
        try:
            await check_session()
        except AuthenticationError:
            return
        for event in events:
            if shutdown.is_set() or await disconnected():
                return
            try:
                await check_session()
            except AuthenticationError:
                return
            if event.sequence is None or event.sequence <= cursor:
                raise ValueError("event page is not strictly ordered after cursor")
            yield encode_event(event)
            cursor = event.sequence
        if monotonic() >= next_heartbeat:
            yield b": heartbeat\n\n"
            next_heartbeat = monotonic() + heartbeat_seconds
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=poll_seconds)
        except TimeoutError:
            pass
