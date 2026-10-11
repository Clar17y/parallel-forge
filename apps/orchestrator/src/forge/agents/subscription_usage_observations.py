"""Shared subscription usage observation helpers for supervised gateways."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from forge.application.ports.subscription_gateway import SubscriptionInvocationRequest
from forge.application.ports.task_usage import TaskUsageObserver
from forge.domain.task_usage_contract import (
    MAX_SIGNED_BIGINT,
    ContextWindow,
    CounterKind,
    Observation,
    TokenCounts,
    WorkUnitBinding,
)

__all__ = [
    "_UsagePublisher",
    "_extract_context_window",
    "_extract_unknown_dimensions",
    "_optional_reasoning",
    "_validate_usage_binding",
]


def _optional_reasoning(value: object, output: int | None) -> int | None:
    """Discard malformed optional reasoning without changing measured output."""
    if type(value) is not int or not 0 <= value <= MAX_SIGNED_BIGINT:
        return None
    return value if output is None or value <= output else None


def _validate_usage_binding(
    binding: WorkUnitBinding, request: SubscriptionInvocationRequest
) -> None:
    if not isinstance(binding, WorkUnitBinding):
        raise TypeError("usage_binding must be a WorkUnitBinding")
    if binding.subject.kind != "run" or binding.subject.subject_id != request.attempt.run_id:
        raise ValueError("usage binding subject does not match request run")
    if binding.source.kind != "subscription_attempt":
        raise ValueError("usage binding source kind must be subscription_attempt")
    if binding.source.source_id != request.attempt.attempt_id:
        raise ValueError("usage binding source does not match attempt identity")
    if binding.source.parent_id != request.attempt.task_id:
        raise ValueError("usage binding source parent does not match task identity")
    effective = request.task.route.effective
    role_str = (
        request.task.purpose.value
        if hasattr(request.task.purpose, "value")
        else str(request.task.purpose)
    )
    effort_str = effective.effort.value if effective.effort is not None else None
    if not any(
        m.role == role_str
        and m.provider == effective.provider
        and m.model == effective.model
        and m.reasoning_effort == effort_str
        for m in binding.effective_route.members
    ):
        raise ValueError("usage binding effective route does not match request effective route")


def _extract_context_window(payload: Mapping[str, Any]) -> ContextWindow | None:
    target: Mapping[str, Any] = payload
    for key in ("context", "context_window", "contextWindow"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            target = nested
            break
    occupied = target.get(
        "occupied_tokens",
        target.get(
            "occupiedTokens",
            target.get("occupied", target.get("current_tokens", target.get("currentTokens"))),
        ),
    )
    capacity = target.get(
        "capacity_tokens",
        target.get(
            "capacityTokens",
            target.get("capacity", target.get("max_tokens", target.get("maxTokens"))),
        ),
    )
    compaction = target.get(
        "compaction_count", target.get("compactionCount", target.get("compactions"))
    )
    def measured(value: object) -> int | None:
        return value if type(value) is int and 0 <= value <= MAX_SIGNED_BIGINT else None

    occ_int = measured(occupied)
    cap_int = measured(capacity)
    comp_int = measured(compaction)
    if occ_int is not None and cap_int is not None and occ_int > cap_int:
        occ_int = None
    if occ_int is None and cap_int is None and comp_int is None:
        return None
    return ContextWindow(
        occupied_tokens=occ_int, capacity_tokens=cap_int, compaction_count=comp_int
    )


def _bounded_identity(value: object, *, fallback: str, limit: int) -> str:
    """Keep safe provider identities, hash oversized ones, and replace absent ones."""
    if type(value) is not str or not value.strip():
        return fallback
    if len(value) <= limit and value.isprintable() and not value.startswith("forge:"):
        return value
    return "forge:sha256:" + hashlib.sha256(
        value.encode("utf-8", "surrogatepass")
    ).hexdigest()


def _extract_unknown_dimensions(
    tokens: TokenCounts, context: ContextWindow | None
) -> tuple[str, ...]:
    unknowns: list[str] = []
    if tokens.input_tokens is None:
        unknowns.append("input_tokens")
    if tokens.output_tokens is None:
        unknowns.append("output_tokens")
    if tokens.cached_input_tokens is None:
        unknowns.append("cached_input_tokens")
    if tokens.reasoning_output_tokens is None:
        unknowns.append("reasoning_output_tokens")
    if context is None:
        unknowns.extend(["context_occupied_tokens", "context_capacity_tokens", "compaction_count"])
    else:
        if context.occupied_tokens is None:
            unknowns.append("context_occupied_tokens")
        if context.capacity_tokens is None:
            unknowns.append("context_capacity_tokens")
        if context.compaction_count is None:
            unknowns.append("compaction_count")
    return tuple(unknowns)


class _UsagePublisher:
    def __init__(
        self,
        binding: WorkUnitBinding,
        sink: TaskUsageObserver,
        *,
        counter_epoch: str = "source",
        timeout_seconds: float = 0.5,
    ) -> None:
        self.binding = binding
        self.sink = sink
        self.counter_epoch = counter_epoch
        self.timeout_seconds = timeout_seconds
        self.sequence = 0
        self.last_event_id: str | None = None
        self.last_context: ContextWindow | None = None
        self.queue: asyncio.Queue[Observation] = asyncio.Queue(maxsize=64)
        self.dropped_or_failed = False
        self._worker_task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        self._worker_task = asyncio.create_task(self._run_worker())

    async def _run_worker(self) -> None:
        while True:
            try:
                obs = await self.queue.get()
            except asyncio.CancelledError:
                break
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    await self.sink.observe_usage(obs)
            except asyncio.CancelledError:
                self.dropped_or_failed = True
                break
            except Exception:  # noqa: BLE001 - sink errors are isolated and must not fail worker
                self.dropped_or_failed = True
            finally:
                self.queue.task_done()

    def publish_live(
        self,
        *,
        event_id: object,
        tokens: TokenCounts,
        context: ContextWindow | None = None,
        counter_kind: CounterKind = CounterKind.CUMULATIVE,
        counter_epoch: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        if self._closed:
            return
        self.sequence += 1
        event_id = _bounded_identity(
            event_id, fallback=f"forge:missing-event:{self.sequence}", limit=256
        )
        if context is not None:
            self.last_context = context
        unknowns = _extract_unknown_dimensions(tokens, context)
        obs = Observation(
            binding=self.binding,
            event_id=event_id,
            sequence=self.sequence,
            counter_kind=counter_kind,
            tokens=tokens,
            counter_epoch=_bounded_identity(
                counter_epoch or self.counter_epoch, fallback="source", limit=96
            ),
            observed_at=observed_at,
            context=context,
            unknown_dimensions=unknowns,
            final=False,
        )
        try:
            self.queue.put_nowait(obs)
            self.last_event_id = event_id
        except asyncio.QueueFull:
            self.dropped_or_failed = True

    async def publish_final(
        self,
        *,
        event_id: object,
        status: str,
        tokens: TokenCounts,
        context: ContextWindow | None = None,
        counter_kind: CounterKind = CounterKind.CUMULATIVE,
        counter_epoch: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        self.sequence += 1
        # The durable key is (work_unit_id, event_id). Provider IDs in the
        # reserved forge: namespace are hashed by publish_live, so this stable
        # final identity is disjoint from every live event, including earlier
        # events whose IDs are no longer the reconciliation target.
        event_id = f"forge:final:{self.binding.work_unit_id}"
        context = context or self.last_context
        unknowns = _extract_unknown_dimensions(tokens, context)
        final_obs = Observation(
            binding=self.binding,
            event_id=event_id,
            sequence=self.sequence,
            counter_kind=counter_kind,
            tokens=tokens,
            counter_epoch=_bounded_identity(
                counter_epoch or self.counter_epoch, fallback="source", limit=96
            ),
            observed_at=observed_at,
            context=context,
            unknown_dimensions=unknowns,
            final=True,
            final_status=status,
            reconciles_event_id=self.last_event_id,
        )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self.queue.join()
        except asyncio.CancelledError:
            self.dropped_or_failed = True
        except Exception:  # noqa: BLE001 - timeout/join failures are isolated
            self.dropped_or_failed = True

        await self.close()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self.sink.observe_usage(final_obs)
        except asyncio.CancelledError:
            self.dropped_or_failed = True
        except Exception:  # noqa: BLE001 - sink errors on final are isolated
            self.dropped_or_failed = True

    async def close(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
