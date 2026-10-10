"""Optional usage observation and durable repository contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from forge.domain.task_usage_contract import (
    BaselineReference,
    CheckpointRecord,
    MonitoringPolicy,
    Observation,
    OwnerCommand,
    WorkUnitBinding,
)
from pydantic import StrictBool, StrictInt


class TaskUsageObserver(Protocol):
    """Companion capability; existing provider gateways need not implement it."""

    async def observe_usage(self, observation: Observation) -> None: ...


class TaskUsageRepository(Protocol):
    async def bind(self, binding: WorkUnitBinding) -> None: ...
    async def append_observation(self, observation: Observation) -> bool: ...
    async def freeze_baseline(self, reference: BaselineReference) -> None: ...
    async def policy(self, project_id: UUID) -> MonitoringPolicy: ...
    async def append_policy_revision(
        self, policy: MonitoringPolicy, *, expected_revision: int
    ) -> None: ...
    async def checkpoint(self, checkpoint: CheckpointRecord) -> None: ...
    async def apply_owner_command(self, command: OwnerCommand) -> OwnerCommandReceipt: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class OwnerCommandReceipt:
    command_id: UUID
    checkpoint_id: UUID
    result_version: StrictInt
    state: str
    actor_id: UUID
    action: str
    affected_snapshot: str
    warnings: tuple[str, ...] = ()
    replayed: StrictBool = False

    def __post_init__(self) -> None:
        from forge.domain.task_usage_contract import CheckpointState, _digest, _positive

        if not all(
            isinstance(value, UUID)
            for value in (self.command_id, self.checkpoint_id, self.actor_id)
        ):
            raise TypeError("receipt IDs must be UUIDs")
        _positive(self.result_version, "result version", allow_zero=True)
        _digest(self.affected_snapshot, "affected snapshot")
        if self.action not in {
            "continue",
            "resume",
            "resolve",
            "set_policy",
            "override_checkpoint",
        } or self.state not in {state.value for state in CheckpointState}:
            raise ValueError("receipt action/state invalid")
        if type(self.replayed) is not bool or any(
            type(warning) is not str for warning in self.warnings
        ):
            raise TypeError("receipt replay/warnings invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageReport:
    binding: WorkUnitBinding
    policy_revision: StrictInt
    baseline: BaselineReference | None
    input_tokens: StrictInt | None
    output_tokens: StrictInt | None
    context_occupied_tokens: StrictInt | None
    freshness: str
    checkpoint_version: StrictInt | None = None
    cached_input_tokens: StrictInt | None = None
    reasoning_output_tokens: StrictInt | None = None
    context_capacity_tokens: StrictInt | None = None
    compaction_count: StrictInt | None = None
    completeness: str = "unknown"
    checkpoint: CheckpointRecord | None = None
    warning: str | None = None

    def __post_init__(self) -> None:
        from forge.domain.task_usage_contract import ContextWindow, TokenCounts, _positive

        if not isinstance(self.binding, WorkUnitBinding) or not isinstance(
            self.baseline, (BaselineReference, type(None))
        ):
            raise TypeError("report binding/baseline invalid")
        if self.baseline is not None and self.baseline.binding.subject != self.binding.subject:
            raise ValueError("report baseline belongs to another subject")
        _positive(self.policy_revision, "policy revision")
        if self.checkpoint_version is not None:
            _positive(self.checkpoint_version, "checkpoint version", allow_zero=True)
        TokenCounts(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_input_tokens=self.cached_input_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens,
        )
        ContextWindow(
            occupied_tokens=self.context_occupied_tokens,
            capacity_tokens=self.context_capacity_tokens,
            compaction_count=self.compaction_count,
        )
        if self.completeness not in {
            "complete",
            "lower_bound",
            "unknown",
        } or self.freshness not in {"live", "final", "stale", "unknown"}:
            raise ValueError("report completeness/freshness invalid")
        if self.checkpoint is not None and self.checkpoint_version != self.checkpoint.version:
            raise ValueError("checkpoint version differs from report")
        if self.checkpoint is not None and self.checkpoint.binding.subject != self.binding.subject:
            raise ValueError("report checkpoint belongs to another subject")
        if self.checkpoint is not None and (
            self.baseline is None
            or self.checkpoint.baseline_binding != self.baseline.binding
            or self.checkpoint.reference_digest != self.baseline.snapshot_digest
            or self.checkpoint.policy_revision != self.baseline.policy_revision
        ):
            raise ValueError("report checkpoint differs from frozen baseline")
