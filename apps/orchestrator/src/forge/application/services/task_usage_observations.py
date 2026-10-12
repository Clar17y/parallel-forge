"""Deterministic, source-scoped projection of bounded usage observations."""

from __future__ import annotations

from dataclasses import dataclass

from forge.domain.task_usage_contract import MAX_SIGNED_BIGINT, CounterKind, Observation

_DIMENSIONS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_output_tokens",
)


@dataclass(frozen=True, slots=True)
class SourceUsage:
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    reasoning_output_tokens: int | None
    context_occupied_tokens: int | None
    context_capacity_tokens: int | None
    compaction_count: int | None
    completeness: str
    final: bool
    final_status: str | None
    event_id: str | None
    sequence: int
    disagreements: tuple[str, ...]

    @property
    def known_total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return _checked_sum([self.input_tokens or 0, self.output_tokens or 0])


def _checked_sum(values: list[int]) -> int:
    total = sum(values)
    if total > MAX_SIGNED_BIGINT:
        raise ValueError("observed usage exceeds signed-64-bit range")
    return total


def project_source(observations: tuple[Observation, ...]) -> SourceUsage:
    """Project one source; a whole-source final supersedes every provisional epoch."""

    if not observations:
        return SourceUsage(*(None,) * 7, "unknown", False, None, None, 0, ())
    binding = observations[0].binding
    by_event: dict[str, Observation] = {}
    by_sequence: dict[int, Observation] = {}
    for item in observations:
        if item.binding != binding:
            raise ValueError("source projection mixes bindings")
        prior = by_event.get(item.event_id)
        if prior is not None:
            if prior != item:
                raise ValueError("event replay conflicts")
            continue
        if item.sequence in by_sequence:
            raise ValueError("observation sequence conflicts")
        by_event[item.event_id] = item
        by_sequence[item.sequence] = item
    ordered = tuple(by_sequence[key] for key in sorted(by_sequence))
    provisional = [item for item in ordered if not item.final]
    final = next((item for item in reversed(ordered) if item.final), None)
    disagreements: list[str] = []
    if len([item for item in ordered if item.final]) > 1:
        disagreements.append("multiple_finals")
    totals: dict[str, int | None] = {}
    unknown_required = False
    for name in _DIMENSIONS:
        epochs: dict[str, tuple[CounterKind, int | None]] = {}
        for item in provisional:
            epoch = getattr(item, "counter_epoch", "source")
            value = getattr(item.tokens, name)
            if name in ("input_tokens", "output_tokens") and (
                name in item.unknown_dimensions or value is None
            ):
                unknown_required = True
            prior_epoch = epochs.get(epoch)
            if prior_epoch is not None and prior_epoch[0] is not item.counter_kind:
                raise ValueError("counter kind changes within epoch")
            if item.counter_kind is CounterKind.DELTA:
                current = prior_epoch[1] if prior_epoch else None
                epochs[epoch] = (
                    CounterKind.DELTA,
                    _checked_sum([current or 0, value]) if value is not None else current,
                )
            elif value is not None:
                if (
                    prior_epoch is not None
                    and prior_epoch[1] is not None
                    and value < prior_epoch[1]
                ):
                    raise ValueError("cumulative counter decrement within epoch")
                epochs[epoch] = (CounterKind.CUMULATIVE, value)
            elif prior_epoch is None:
                epochs[epoch] = (CounterKind.CUMULATIVE, None)
        known = [value for _, value in epochs.values() if value is not None]
        live = _checked_sum(known) if known else None
        if name in ("input_tokens", "output_tokens") and any(
            value is None for _, value in epochs.values()
        ):
            unknown_required = True
        final_value = getattr(final.tokens, name) if final is not None else None
        if final is not None:
            if live is not None and final_value is not None and final_value < live:
                disagreements.append("final_below_live")
            # A final with an absent dimension cannot invalidate an earlier
            # positive measurement. It remains a lower bound, never an exact final.
            totals[name] = final_value if final_value is not None else (
                live if live is not None and live > 0 else None
            )
        else:
            totals[name] = live
    selected = final or ordered[-1]
    context = selected.context
    if final is not None:
        unknown_required = any(
            getattr(final.tokens, name) is None or name in final.unknown_dimensions
            for name in ("input_tokens", "output_tokens")
        )
    completeness = (
        "unknown"
        if totals["input_tokens"] is None and totals["output_tokens"] is None
        else "lower_bound"
        if unknown_required
        else "complete"
    )
    return SourceUsage(
        input_tokens=totals["input_tokens"],
        output_tokens=totals["output_tokens"],
        cached_input_tokens=totals["cached_input_tokens"],
        reasoning_output_tokens=totals["reasoning_output_tokens"],
        context_occupied_tokens=None if context is None else context.occupied_tokens,
        context_capacity_tokens=None if context is None else context.capacity_tokens,
        compaction_count=None if context is None else context.compaction_count,
        completeness=completeness,
        final=final is not None,
        final_status=None if final is None else final.final_status,
        event_id=selected.event_id,
        sequence=selected.sequence,
        disagreements=tuple(sorted(set(disagreements))),
    )
