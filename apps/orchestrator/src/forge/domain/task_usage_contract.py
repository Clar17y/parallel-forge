"""Versioned, provider-neutral facts and controls for task usage monitoring."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from uuid import UUID

from pydantic import StrictBool, StrictFloat, StrictInt

SCHEMA_VERSION = 1


def _positive(value: int, name: str, *, allow_zero: bool = False) -> None:
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise ValueError(f"{name} must be a {'nonnegative' if allow_zero else 'positive'} integer")


def _digest(value: str, name: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _nonnegative_number(value: float | None, name: str) -> None:
    if value is not None and (
        type(value) not in (int, float) or not math.isfinite(value) or value < 0
    ):
        raise ValueError(f"{name} must be a finite nonnegative number")


class CounterKind(StrEnum):
    CUMULATIVE = "cumulative"
    DELTA = "delta"


class MonitoringMode(StrEnum):
    REPORT_ONLY = "report_only"
    WARN = "warn"
    CHECKPOINT = "checkpoint"


class CheckpointState(StrEnum):
    REQUESTED = "checkpoint_requested"
    PAUSING = "pausing"
    PAUSED = "paused"
    RESUMED = "resumed"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True, kw_only=True)
class SubjectKey:
    project_id: UUID
    kind: str  # run or authoring_job
    subject_id: UUID
    schema_version: StrictInt = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.kind not in {"run", "authoring_job"}:
            raise ValueError("unknown subject kind")
        if not all(isinstance(value, UUID) for value in (self.project_id, self.subject_id)):
            raise ValueError("subject IDs must be UUIDs")
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported subject schema")


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteMember:
    role: str
    provider: str
    model: str
    ordinal: StrictInt
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value for value in (self.role, self.provider, self.model)
        ):
            raise ValueError("route member labels must be nonempty")
        _positive(self.ordinal, "route ordinal", allow_zero=True)
        if self.reasoning_effort is not None and (
            type(self.reasoning_effort) is not str or not self.reasoning_effort
        ):
            raise ValueError("reasoning effort must be a nonempty label")


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteCohort:
    fingerprint: str
    members: tuple[RouteMember, ...]

    def __post_init__(self) -> None:
        _digest(self.fingerprint, "route fingerprint")
        if not self.members:
            raise ValueError("route cohort requires at least one member")
        if any(not isinstance(member, RouteMember) for member in self.members):
            raise TypeError("route members must be typed")
        if len({(member.role, member.ordinal) for member in self.members}) != len(self.members):
            raise ValueError("route member identity is duplicated")
        if self.fingerprint != self.digest_members(self.members):
            raise ValueError("route fingerprint does not match members")

    @staticmethod
    def digest_members(members: tuple[RouteMember, ...]) -> str:
        payload = [
            (member.role, member.provider, member.model, member.ordinal, member.reasoning_effort)
            for member in members
        ]
        return sha256(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()

    @classmethod
    def from_members(cls, members: tuple[RouteMember, ...]) -> RouteCohort:
        if not members:
            raise ValueError("route cohort requires at least one member")
        return cls(fingerprint=cls.digest_members(members), members=members)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceBinding:
    kind: str  # agent_execution, subscription_attempt, authoring_attempt
    source_id: UUID
    parent_id: UUID | None = None  # subscription task ID when source is subscription_attempt

    def __post_init__(self) -> None:
        if self.kind not in {"agent_execution", "subscription_attempt", "authoring_attempt"}:
            raise ValueError("unknown source kind")
        if not isinstance(self.source_id, UUID):
            raise TypeError("source IDs must be UUIDs")
        if self.kind == "subscription_attempt" and not isinstance(self.parent_id, UUID):
            raise ValueError("subscription source requires task parent")
        if self.kind != "subscription_attempt" and self.parent_id is not None:
            raise ValueError("only subscription sources have a task parent")


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkUnitBinding:
    subject: SubjectKey
    work_unit_id: UUID
    admitted_phase_id: str
    configured_route: RouteCohort
    effective_route: RouteCohort
    source: SourceBinding
    schema_version: StrictInt = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.subject, SubjectKey) or not isinstance(self.work_unit_id, UUID):
            raise TypeError("work unit identity is invalid")
        if (
            type(self.admitted_phase_id) is not str
            or not self.admitted_phase_id
            or len(self.admitted_phase_id) > 96
        ):
            raise ValueError("admitted phase is invalid")
        if not isinstance(self.configured_route, RouteCohort) or not isinstance(
            self.effective_route, RouteCohort
        ):
            raise TypeError("route cohorts are required")
        if (
            not isinstance(self.source, SourceBinding)
            or self.source.source_id == self.subject.subject_id
        ):
            raise ValueError("source binding is invalid")
        if (self.subject.kind == "run") != (
            self.source.kind in {"agent_execution", "subscription_attempt"}
        ):
            raise ValueError("source kind does not match subject")
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported work unit schema")


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenCounts:
    """None is unmeasured; zero is measured. Cached and reasoning are subsets."""

    input_tokens: StrictInt | None = None
    output_tokens: StrictInt | None = None
    cached_input_tokens: StrictInt | None = None
    reasoning_output_tokens: StrictInt | None = None

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "reasoning_output_tokens",
        ):
            value = getattr(self, name)
            if value is not None:
                _positive(value, name, allow_zero=True)
        for subset, total in (
            (self.cached_input_tokens, self.input_tokens),
            (self.reasoning_output_tokens, self.output_tokens),
        ):
            if subset is not None and total is not None and subset > total:
                raise ValueError("token subset exceeds known total")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextWindow:
    occupied_tokens: StrictInt | None = None
    capacity_tokens: StrictInt | None = None
    compaction_count: StrictInt | None = None

    def __post_init__(self) -> None:
        for name in ("occupied_tokens", "capacity_tokens", "compaction_count"):
            value = getattr(self, name)
            if value is not None:
                _positive(value, name, allow_zero=True)
        if (
            self.capacity_tokens is not None
            and self.occupied_tokens is not None
            and self.occupied_tokens > self.capacity_tokens
        ):
            raise ValueError("context occupancy exceeds capacity")


@dataclass(frozen=True, slots=True, kw_only=True)
class Observation:
    binding: WorkUnitBinding
    event_id: str
    sequence: StrictInt
    counter_kind: CounterKind
    tokens: TokenCounts
    observed_at: datetime | None = None
    context: ContextWindow | None = None
    scope: str = "source"  # never silently aggregate different sources
    unknown_dimensions: tuple[str, ...] = ()
    final: StrictBool = False
    final_status: str | None = None
    reconciles_event_id: str | None = None
    schema_version: StrictInt = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.binding, WorkUnitBinding)
            or type(self.event_id) is not str
            or not self.event_id
            or len(self.event_id) > 256
        ):
            raise ValueError("observation identity is invalid")
        _positive(self.sequence, "sequence")
        if not isinstance(self.counter_kind, CounterKind) or not isinstance(
            self.tokens, TokenCounts
        ):
            raise TypeError("observation counters are invalid")
        if self.context is not None and not isinstance(self.context, ContextWindow):
            raise TypeError("context measurement is invalid")
        values = {
            "input_tokens": self.tokens.input_tokens,
            "output_tokens": self.tokens.output_tokens,
            "cached_input_tokens": self.tokens.cached_input_tokens,
            "reasoning_output_tokens": self.tokens.reasoning_output_tokens,
            "context_occupied_tokens": None
            if self.context is None
            else self.context.occupied_tokens,
            "context_capacity_tokens": None
            if self.context is None
            else self.context.capacity_tokens,
            "compaction_count": None if self.context is None else self.context.compaction_count,
        }
        if len(set(self.unknown_dimensions)) != len(self.unknown_dimensions) or any(
            name not in values or values[name] is not None for name in self.unknown_dimensions
        ):
            raise ValueError("unknown dimensions must be unique unmeasured fields")
        if self.scope != "source":
            raise ValueError("only source-scoped observations are admitted")
        if type(self.final) is not bool:
            raise TypeError("final flag must be a boolean")
        if self.final and not self.final_status:
            raise ValueError("final observation requires status")
        if not self.final and (
            self.final_status is not None or self.reconciles_event_id is not None
        ):
            raise ValueError("live observation cannot reconcile final state")
        if self.reconciles_event_id == self.event_id:
            raise ValueError("observation cannot reconcile itself")
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported observation schema")


@dataclass(frozen=True, slots=True, kw_only=True)
class MonitoringPolicy:
    project_id: UUID
    revision: StrictInt = 1
    mode: MonitoringMode = MonitoringMode.REPORT_ONLY
    warning_multiplier: StrictFloat | StrictInt = 1.5
    checkpoint_multiplier: StrictFloat | StrictInt = 2.0
    history_limit: StrictInt = 50
    minimum_comparables: StrictInt = 20
    hard_token_cap: StrictInt | None = None
    hard_context_cap: StrictInt | None = None

    def __post_init__(self) -> None:
        import math

        if not isinstance(self.project_id, UUID) or not isinstance(self.mode, MonitoringMode):
            raise TypeError("monitoring policy identity is invalid")
        for name in ("revision", "history_limit", "minimum_comparables"):
            _positive(getattr(self, name), name)
        if self.minimum_comparables > self.history_limit:
            raise ValueError("minimum comparables exceeds history limit")
        for name in ("warning_multiplier", "checkpoint_multiplier"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 1:
                raise ValueError(f"{name} must be finite and at least one")
        if self.checkpoint_multiplier < self.warning_multiplier:
            raise ValueError("checkpoint multiplier precedes warning")
        for name in ("hard_token_cap", "hard_context_cap"):
            value = getattr(self, name)
            if value is not None:
                _positive(value, name)


@dataclass(frozen=True, slots=True, kw_only=True)
class BaselineReference:
    binding: WorkUnitBinding
    policy_revision: StrictInt
    sample_count: StrictInt
    status: str  # ready or insufficient_history
    history_digest: str
    snapshot_digest: str
    estimator_version: str
    window_limit: StrictInt
    minimum_comparables: StrictInt
    cohort_digest: str
    reference_tokens: StrictInt | StrictFloat | None = None
    median_tokens: StrictInt | StrictFloat | None = None
    p90_tokens: StrictInt | StrictFloat | None = None
    mean_tokens: StrictInt | StrictFloat | None = None
    coverage_count: StrictInt = 0
    outcome_count: StrictInt = 0
    source_count: StrictInt = 0
    window_started_at: datetime | None = None
    window_ended_at: datetime | None = None
    warning_threshold_tokens: StrictInt | StrictFloat | None = None
    checkpoint_threshold_tokens: StrictInt | StrictFloat | None = None
    frozen_at: datetime | None = None
    schema_version: StrictInt = SCHEMA_VERSION
    completeness: str = "complete"
    comparison_scope: str = "subject"

    def __post_init__(self) -> None:
        if not isinstance(self.binding, WorkUnitBinding):
            raise TypeError("baseline requires a work unit binding")
        _positive(self.policy_revision, "policy revision")
        _positive(self.sample_count, "sample count", allow_zero=True)
        _digest(self.history_digest, "history digest")
        _digest(self.snapshot_digest, "snapshot digest")
        _digest(self.cohort_digest, "cohort digest")
        if self.cohort_digest != self.binding.configured_route.fingerprint:
            raise ValueError("baseline cohort differs from admitted configured route")
        if type(self.estimator_version) is not str or not self.estimator_version:
            raise ValueError("estimator version is required")
        _positive(self.window_limit, "window limit")
        _positive(self.minimum_comparables, "minimum comparables")
        if self.minimum_comparables > self.window_limit or self.sample_count > self.window_limit:
            raise ValueError("baseline sample/window counts exceed window limit")
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported baseline schema")
        if self.status not in {"ready", "insufficient_history"}:
            raise ValueError("unknown baseline status")
        for name in ("reference_tokens", "median_tokens", "p90_tokens"):
            _nonnegative_number(getattr(self, name), name)
        _nonnegative_number(self.mean_tokens, "mean_tokens")
        _nonnegative_number(self.warning_threshold_tokens, "warning threshold")
        _nonnegative_number(self.checkpoint_threshold_tokens, "checkpoint threshold")
        if (
            self.warning_threshold_tokens is not None
            and self.checkpoint_threshold_tokens is not None
            and self.checkpoint_threshold_tokens < self.warning_threshold_tokens
        ):
            raise ValueError("checkpoint threshold precedes warning")
        for name in ("coverage_count", "outcome_count", "source_count"):
            _positive(getattr(self, name), name, allow_zero=True)
        if self.coverage_count > self.sample_count or self.outcome_count > self.sample_count:
            raise ValueError("baseline counts exceed samples")
        if (
            self.completeness not in {"complete", "lower_bound", "unknown"}
            or self.comparison_scope != "subject"
        ):
            raise ValueError("baseline scope/completeness is invalid")
        for name in ("frozen_at", "window_started_at", "window_ended_at"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, datetime) or value.tzinfo is None):
                raise ValueError(f"{name} must be timezone aware")
        if self.frozen_at is None:
            raise ValueError("baseline frozen time is required")
        if (self.window_started_at is None) != (self.window_ended_at is None):
            raise ValueError("sampling window must have both boundaries")
        if (
            self.window_started_at is not None
            and self.window_ended_at is not None
            and (
                self.window_started_at > self.window_ended_at
                or self.window_ended_at > self.frozen_at
            )
        ):
            raise ValueError("sampling window is invalid")
        if self.status == "insufficient_history" and any(
            value is not None
            for value in (
                self.reference_tokens,
                self.warning_threshold_tokens,
                self.checkpoint_threshold_tokens,
            )
        ):
            raise ValueError("insufficient history cannot have resolved thresholds")
        if self.status == "ready" and (
            self.sample_count < self.minimum_comparables
            or self.window_started_at is None
            or any(
                value is None
                for value in (
                    self.reference_tokens,
                    self.median_tokens,
                    self.p90_tokens,
                    self.mean_tokens,
                    self.warning_threshold_tokens,
                    self.checkpoint_threshold_tokens,
                )
            )
        ):
            raise ValueError("ready baseline requires comparable window and resolved thresholds")


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckpointRecord:
    checkpoint_id: UUID
    binding: WorkUnitBinding
    baseline_binding: WorkUnitBinding
    state: CheckpointState
    policy_revision: StrictInt
    reference_digest: str
    trigger_event_id: str
    version: StrictInt = 0
    process_settled: StrictBool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.checkpoint_id, UUID)
            or not isinstance(self.binding, WorkUnitBinding)
            or not isinstance(self.baseline_binding, WorkUnitBinding)
        ):
            raise TypeError("checkpoint identity is invalid")
        if self.baseline_binding.subject != self.binding.subject:
            raise ValueError("checkpoint trigger and baseline must share a subject")
        if not isinstance(self.state, CheckpointState):
            raise TypeError("checkpoint state is invalid")
        _positive(self.policy_revision, "policy revision")
        _positive(self.version, "checkpoint version", allow_zero=True)
        _digest(self.reference_digest, "reference digest")
        if type(self.trigger_event_id) is not str or not self.trigger_event_id:
            raise ValueError("checkpoint requires trigger event")
        if type(self.process_settled) is not bool:
            raise TypeError("process settlement must be a boolean")
        if (
            self.state in {CheckpointState.PAUSED, CheckpointState.RESUMED}
            and not self.process_settled
        ):
            raise ValueError("paused or resumed checkpoint requires process settlement")


@dataclass(frozen=True, slots=True, kw_only=True)
class OwnerCommand:
    command_id: UUID
    checkpoint_id: UUID
    expected_version: StrictInt
    idempotency_key: str
    action: str
    actor_id: UUID
    override_note: str | None = None
    continue_mode: str | None = None
    requested_policy: MonitoringPolicy | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, UUID)
            for value in (self.command_id, self.checkpoint_id, self.actor_id)
        ):
            raise TypeError("owner command IDs must be UUIDs")
        _positive(self.expected_version, "expected version", allow_zero=True)
        if (
            type(self.idempotency_key) is not str
            or not self.idempotency_key
            or self.action
            not in {
                "continue",
                "resume",
                "resolve",
                "set_policy",
                "override_checkpoint",
            }
        ):
            raise ValueError("owner command is invalid")
        if self.action == "continue":
            if self.continue_mode not in {"next_checkpoint", "without_usage_pauses"}:
                raise ValueError("continue command requires an explicit mode")
        elif self.continue_mode is not None:
            raise ValueError("only continue commands have a continue mode")
        if (self.action == "set_policy") != (self.requested_policy is not None):
            raise ValueError("set_policy requires requested policy")
        if self.requested_policy is not None and not isinstance(
            self.requested_policy, MonitoringPolicy
        ):
            raise TypeError("requested policy must be typed")
        if self.override_note is not None and type(self.override_note) is not str:
            raise TypeError("override note must be text")
