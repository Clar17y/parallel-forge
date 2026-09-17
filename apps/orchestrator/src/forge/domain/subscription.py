"""Immutable subscription runtime domain contracts and validation models."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from forge.domain.agent import ReviewOutput
from forge.domain.paths import normalize_policy_paths, policy_path_key
from forge.domain.payload import validate_durable_payload
from forge.domain.tool import ToolName

_WORKTREE_ID = re.compile(r"\A[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")
_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z", re.ASCII)
_COMMIT_SHA = re.compile(r"\A[0-9a-f]{40}\Z", re.ASCII)
_COMMAND_NAME = re.compile(r"\A[a-zA-Z0-9_-]+\Z")
_SHELL_META = re.compile(r"[\r\n;&|$`()<>`]")


def _validate_non_nil_uuid(value: UUID, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{name} must be a UUID")
    if value.int == 0:
        raise ValueError(f"{name} must not be nil")
    return value


def _validate_non_blank_text(value: str, name: str, *, max_length: int = 10_000) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value or not value.strip():
        raise ValueError(f"{name} must not be blank")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds maximum length of {max_length}")
    return value


def _validate_sha256_digest(value: str, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical lowercase SHA-256 digest")
    return value


def _validate_bool(value: bool, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a boolean")
    return value


def _freeze_texts(values: Sequence[str], name: str, *, maximum: int = 64) -> tuple[str, ...]:
    frozen = tuple(values)
    if len(frozen) > maximum:
        raise ValueError(f"{name} exceeds maximum item count of {maximum}")
    for value in frozen:
        _validate_non_blank_text(value, name)
    return frozen


class SpecialistPurpose(StrEnum):
    """Closed vocabulary of specialist purposes separating purpose from tools."""

    PRIMARY = "primary"
    ROUTINE_IMPLEMENTATION = "routine_implementation"
    COMPLEX_IMPLEMENTATION = "complex_implementation"
    INDEPENDENT_REVIEW = "independent_review"
    PLANNING = "planning"
    EXPLORATION = "exploration"
    SECURITY = "security"
    INTEGRATION = "integration"
    VERIFICATION = "verification"


class Capability(StrEnum):
    """Granular tool capabilities assigned to specialist purposes."""

    DELEGATE = "delegate"
    READ = "read"
    WRITE = "write"
    CHECK = "check"
    COMMIT = "commit"
    REVIEW_READ = "review_read"


class ReasoningEffort(StrEnum):
    """Provider-agnostic reasoning effort level."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAXIMUM = "maximum"


class AuthMode(StrEnum):
    """Authentication mode used to access the provider client."""

    SUBSCRIPTION = "subscription"
    API_KEY = "api_key"


class BillingMode(StrEnum):
    """Billing and charge boundary for an execution route."""

    ALLOWANCE_ONLY = "allowance_only"
    PAID_OPT_IN = "paid_opt_in"


class QuotaStatus(StrEnum):
    """Telemetry status for subscription or account quota."""

    OK = "ok"
    EXHAUSTED = "exhausted"
    UNKNOWN = "unknown"


class FailureReason(StrEnum):
    """Closed taxonomy of execution failures for fallback classification."""

    QUOTA_EXHAUSTED = "quota_exhausted"
    LOGIN_FAILED = "login_failed"
    UNSUPPORTED_MODEL = "unsupported_model"
    OUTAGE = "outage"
    BUDGET_EXHAUSTED = "budget_exhausted"
    UNCERTAIN_TERMINATION = "uncertain_termination"
    POLICY_DENIED = "policy_denied"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"


class HandoffStatus(StrEnum):
    """Terminal or handoff state of a worker attempt."""

    COMPLETED = "completed"
    REPAIRS_EXHAUSTED = "repairs_exhausted"
    SCOPE_EXPANSION_REQUESTED = "scope_expansion_requested"
    BLOCKED = "blocked"
    FAILED = "failed"


class DecisionKind(StrEnum):
    """Closed set of primary and worker structured decisions."""

    DELEGATE = "delegate"
    WAIT = "wait"
    SCOPE_REQUEST = "scope_request"
    SCOPE_RESPONSE = "scope_response"
    ACCEPT = "accept"
    REASSIGN = "reassign"
    SELECT_REVIEW = "select_review"


# Capability mappings separating specialist purpose from raw tool permissions.
SPECIALIST_CAPABILITIES: Mapping[SpecialistPurpose, frozenset[Capability]] = MappingProxyType(
    {
        SpecialistPurpose.PRIMARY: frozenset(
            {Capability.DELEGATE, Capability.READ, Capability.WRITE, Capability.CHECK}
        ),
        SpecialistPurpose.ROUTINE_IMPLEMENTATION: frozenset(
            {Capability.READ, Capability.WRITE, Capability.CHECK}
        ),
        SpecialistPurpose.COMPLEX_IMPLEMENTATION: frozenset(
            {Capability.READ, Capability.WRITE, Capability.CHECK}
        ),
        SpecialistPurpose.INDEPENDENT_REVIEW: frozenset({Capability.READ, Capability.REVIEW_READ}),
        SpecialistPurpose.PLANNING: frozenset({Capability.READ}),
        SpecialistPurpose.EXPLORATION: frozenset({Capability.READ, Capability.CHECK}),
        SpecialistPurpose.SECURITY: frozenset({Capability.READ}),
        SpecialistPurpose.INTEGRATION: frozenset(
            {Capability.READ, Capability.WRITE, Capability.CHECK, Capability.COMMIT}
        ),
        SpecialistPurpose.VERIFICATION: frozenset({Capability.READ, Capability.CHECK}),
    }
)

_READ_TOOLS: frozenset[ToolName] = frozenset(
    {
        ToolName.REPOSITORY_LIST_FILES,
        ToolName.REPOSITORY_READ_FILE,
        ToolName.REPOSITORY_SEARCH,
        ToolName.REPOSITORY_READ_INSTRUCTIONS,
        ToolName.GIT_STATUS,
        ToolName.GIT_DIFF,
    }
)

CANDIDATE_READ_TOOLS: frozenset[ToolName] = _READ_TOOLS | frozenset(
    {
        ToolName.VALIDATION_RESULTS_READ,
        ToolName.REVIEW_ARTIFACTS_READ,
    }
)

_MUTATION_TOOLS: frozenset[ToolName] = frozenset(
    {
        ToolName.REPOSITORY_WRITE_FILE,
        ToolName.REPOSITORY_DELETE_FILE,
        ToolName.REPOSITORY_RENAME_FILE,
    }
)

SPECIALIST_ALLOWED_TOOLS: Mapping[SpecialistPurpose, frozenset[ToolName]] = MappingProxyType(
    {
        SpecialistPurpose.PRIMARY: frozenset(
            _READ_TOOLS
            | {
                *_MUTATION_TOOLS,
                ToolName.GIT_COMMIT,
                ToolName.BUILD_RUN_NAMED_CHECK,
                ToolName.VALIDATION_RESULTS_READ,
                ToolName.REVIEW_ARTIFACTS_READ,
            }
        ),
        SpecialistPurpose.ROUTINE_IMPLEMENTATION: frozenset(
            _READ_TOOLS
            | {
                *_MUTATION_TOOLS,
                ToolName.BUILD_RUN_NAMED_CHECK,
            }
        ),
        SpecialistPurpose.COMPLEX_IMPLEMENTATION: frozenset(
            _READ_TOOLS
            | {
                *_MUTATION_TOOLS,
                ToolName.BUILD_RUN_NAMED_CHECK,
            }
        ),
        SpecialistPurpose.INDEPENDENT_REVIEW: CANDIDATE_READ_TOOLS,
        SpecialistPurpose.PLANNING: frozenset(_READ_TOOLS),
        SpecialistPurpose.EXPLORATION: frozenset(_READ_TOOLS | {ToolName.BUILD_RUN_NAMED_CHECK}),
        SpecialistPurpose.SECURITY: frozenset(_READ_TOOLS),
        SpecialistPurpose.INTEGRATION: frozenset(
            _READ_TOOLS
            | {
                *_MUTATION_TOOLS,
                ToolName.BUILD_RUN_NAMED_CHECK,
                ToolName.GIT_COMMIT,
            }
        ),
        SpecialistPurpose.VERIFICATION: frozenset(
            _READ_TOOLS | {ToolName.BUILD_RUN_NAMED_CHECK, ToolName.VALIDATION_RESULTS_READ}
        ),
    }
)


def can_delegate(purpose: SpecialistPurpose) -> bool:
    """Only the primary role may create worker delegation tasks."""
    return purpose is SpecialistPurpose.PRIMARY


def is_read_only(purpose: SpecialistPurpose) -> bool:
    """Return whether the specialist is strictly read-only."""
    return purpose in {
        SpecialistPurpose.INDEPENDENT_REVIEW,
        SpecialistPurpose.PLANNING,
        SpecialistPurpose.SECURITY,
    }


def permits_fallback(reason: FailureReason) -> bool:
    """Policy denials never authorize an alternate route."""
    if not isinstance(reason, FailureReason):
        raise TypeError("reason must be a FailureReason")
    return reason not in {FailureReason.POLICY_DENIED, FailureReason.UNSUPPORTED_CAPABILITY}


def validate_tool_permission(purpose: SpecialistPurpose, tool_name: ToolName) -> None:
    """Validate that the given specialist role is permitted to invoke the tool."""
    if not isinstance(purpose, SpecialistPurpose):
        raise TypeError("purpose must be a SpecialistPurpose")
    if not isinstance(tool_name, ToolName):
        raise TypeError("tool_name must be a ToolName")
    allowed = SPECIALIST_ALLOWED_TOOLS.get(purpose, frozenset())
    if tool_name not in allowed:
        raise ValueError(f"tool {tool_name.value} is not permitted for role {purpose.value}")


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteSpec:
    """Concrete route identity for provider, client, model, effort, auth and billing."""

    provider: str
    client: str
    model: str
    effort: ReasoningEffort = ReasoningEffort.LOW
    auth_mode: AuthMode = AuthMode.SUBSCRIPTION
    billing_mode: BillingMode = BillingMode.ALLOWANCE_ONLY

    def __post_init__(self) -> None:
        _validate_non_blank_text(self.provider, "provider", max_length=96)
        _validate_non_blank_text(self.client, "client", max_length=96)
        _validate_non_blank_text(self.model, "model", max_length=255)
        if not isinstance(self.effort, ReasoningEffort):
            raise TypeError("effort must be a ReasoningEffort")
        if not isinstance(self.auth_mode, AuthMode):
            raise TypeError("auth_mode must be an AuthMode")
        if not isinstance(self.billing_mode, BillingMode):
            raise TypeError("billing_mode must be a BillingMode")


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelMapping:
    """Explicit operator-approved mapping between requested and effective models."""

    requested_model: str
    effective_model: str
    approved_by: str
    reason: str

    def __post_init__(self) -> None:
        _validate_non_blank_text(self.requested_model, "requested_model", max_length=255)
        _validate_non_blank_text(self.effective_model, "effective_model", max_length=255)
        _validate_non_blank_text(self.approved_by, "approved_by", max_length=255)
        _validate_non_blank_text(self.reason, "reason", max_length=1000)


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteMapping:
    """Operator-approved complete requested/effective route substitution."""

    requested: RouteSpec
    effective: RouteSpec
    approved_by: str
    approval_id: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.requested, RouteSpec) or not isinstance(self.effective, RouteSpec):
            raise TypeError("route mapping routes must be RouteSpec instances")
        _validate_non_blank_text(self.approved_by, "approved_by", max_length=255)
        _validate_non_blank_text(self.approval_id, "approval_id", max_length=255)
        _validate_non_blank_text(self.reason, "reason", max_length=1000)


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteBinding:
    """Pairing of requested route and effective route with explicit approval mapping."""

    requested: RouteSpec
    effective: RouteSpec
    mapping_applied: RouteMapping | None = None
    is_primary: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.requested, RouteSpec):
            raise TypeError("requested route must be a RouteSpec")
        if not isinstance(self.effective, RouteSpec):
            raise TypeError("effective route must be a RouteSpec")
        _validate_bool(self.is_primary, "is_primary")
        if self.mapping_applied is not None and not isinstance(self.mapping_applied, RouteMapping):
            raise TypeError("mapping_applied must be a RouteMapping")

        if self.requested != self.effective:
            if self.mapping_applied is None:
                raise ValueError(
                    "differing requested and effective route requires explicit approved mapping"
                )
            if self.mapping_applied.requested != self.requested:
                raise ValueError("mapping requested route does not match requested route")
            if self.mapping_applied.effective != self.effective:
                raise ValueError("mapping effective route does not match effective route")

        if self.is_primary and self.requested != self.effective:
            raise ValueError("primary route substitution is prohibited")


@dataclass(frozen=True, slots=True, kw_only=True)
class RolePreference:
    """Operator preference for one specialist purpose with optional fallbacks."""

    purpose: SpecialistPurpose
    preferred_route: RouteSpec
    fallback_routes: tuple[RouteSpec, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.purpose, SpecialistPurpose):
            raise TypeError("purpose must be a SpecialistPurpose")
        if not isinstance(self.preferred_route, RouteSpec):
            raise TypeError("preferred_route must be a RouteSpec")
        fallbacks = tuple(self.fallback_routes)
        for route in fallbacks:
            if not isinstance(route, RouteSpec):
                raise TypeError("fallback routes must be RouteSpec instances")
            if route == self.preferred_route:
                raise ValueError("fallback route cannot duplicate preferred route")
        if len(fallbacks) != len(set(fallbacks)):
            raise ValueError("fallback routes must not contain duplicates")
        if self.purpose is SpecialistPurpose.PRIMARY and fallbacks:
            raise ValueError("primary fallback prohibited")
        object.__setattr__(self, "fallback_routes", fallbacks)


@dataclass(frozen=True, slots=True, kw_only=True)
class OperatorProfile:
    """Versioned, immutable profile specifying default routes and mappings."""

    profile_id: UUID
    version: int
    preferences: tuple[RolePreference, ...]
    approved_mappings: tuple[ModelMapping, ...] = ()
    default_billing_mode: BillingMode = BillingMode.ALLOWANCE_ONLY

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.profile_id, "profile_id")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("profile version must be an integer >= 1")
        prefs = tuple(self.preferences)
        purposes = [p.purpose for p in prefs]
        if len(purposes) != len(set(purposes)):
            raise ValueError("operator profile contains duplicate preferences for a purpose")
        object.__setattr__(self, "preferences", prefs)
        mappings = tuple(self.approved_mappings)
        keys = [(m.requested_model, m.effective_model) for m in mappings]
        if len(keys) != len(set(keys)):
            raise ValueError("operator profile contains duplicate approved mappings")
        object.__setattr__(self, "approved_mappings", mappings)
        if not isinstance(self.default_billing_mode, BillingMode):
            raise TypeError("default_billing_mode must be a BillingMode")

    def preference_for(self, purpose: SpecialistPurpose) -> RolePreference:
        """Retrieve preference for a purpose or raise KeyError."""
        for pref in self.preferences:
            if pref.purpose == purpose:
                return pref
        raise KeyError(f"no preference configured for purpose {purpose.value}")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionEnvelope:
    """Frozen per-run execution envelope separating safety policy from profile."""

    run_id: UUID
    profile_id: UUID
    profile_version: int
    safety_policy_version: int
    routes: tuple[tuple[SpecialistPurpose, RouteBinding], ...]
    allowed_fallbacks: tuple[tuple[SpecialistPurpose, tuple[RouteSpec, ...]], ...] = ()
    billing_mode: BillingMode = BillingMode.ALLOWANCE_ONLY
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.run_id, "run_id")
        _validate_non_nil_uuid(self.profile_id, "profile_id")
        if type(self.profile_version) is not int or self.profile_version < 1:
            raise ValueError("profile_version must be an integer >= 1")
        if type(self.safety_policy_version) is not int or self.safety_policy_version < 1:
            raise ValueError("safety_policy_version must be an integer >= 1")
        if not isinstance(self.billing_mode, BillingMode):
            raise TypeError("billing_mode must be a BillingMode")

        route_items = tuple(self.routes)
        route_map: dict[SpecialistPurpose, RouteBinding] = {}
        for route_item in route_items:
            if not isinstance(route_item, tuple) or len(route_item) != 2:
                raise TypeError("routes must be pairs of (SpecialistPurpose, RouteBinding)")
            purpose, binding = route_item
            if not isinstance(purpose, SpecialistPurpose):
                raise TypeError("route purpose must be a SpecialistPurpose")
            if not isinstance(binding, RouteBinding):
                raise TypeError("route binding must be a RouteBinding")
            if purpose in route_map:
                raise ValueError(f"duplicate route binding for purpose {purpose.value}")
            if purpose is SpecialistPurpose.PRIMARY and binding.requested != binding.effective:
                raise ValueError("primary route substitution is prohibited")
            if self.billing_mode is BillingMode.ALLOWANCE_ONLY and (
                binding.effective.auth_mode is AuthMode.API_KEY
                or binding.effective.billing_mode is BillingMode.PAID_OPT_IN
            ):
                raise ValueError("allowance-only envelope rejects api_key mode and paid overage")
            route_map[purpose] = binding

        fallback_items = tuple(self.allowed_fallbacks)
        fallback_map: dict[SpecialistPurpose, tuple[RouteSpec, ...]] = {}

        for fallback_item in fallback_items:
            if not isinstance(fallback_item, tuple) or len(fallback_item) != 2:
                raise TypeError("allowed_fallbacks must be pairs")
            fallback_purpose, fallbacks = fallback_item
            if not isinstance(fallback_purpose, SpecialistPurpose):
                raise TypeError("fallback purpose must be a SpecialistPurpose")
            if fallback_purpose not in route_map:
                raise ValueError("fallback purpose must have a bound route")
            if fallback_purpose in fallback_map:
                raise ValueError(f"duplicate fallback routes for purpose {fallback_purpose.value}")
            frozen_fallbacks = tuple(fallbacks)
            if len(frozen_fallbacks) > 16:
                raise ValueError("fallback routes exceed maximum item count of 16")
            for fallback in frozen_fallbacks:
                if not isinstance(fallback, RouteSpec):
                    raise TypeError("fallback routes must be RouteSpec instances")
                if fallback == route_map[fallback_purpose].effective:
                    raise ValueError("fallback route cannot duplicate bound effective route")
                if self.billing_mode is BillingMode.ALLOWANCE_ONLY and (
                    fallback.auth_mode is AuthMode.API_KEY
                    or fallback.billing_mode is BillingMode.PAID_OPT_IN
                ):
                    raise ValueError(
                        "allowance-only envelope rejects api_key mode and paid overage"
                    )
            if fallback_purpose is SpecialistPurpose.PRIMARY and fallbacks:
                raise ValueError("primary fallback prohibited in execution envelope")
            fallback_map[fallback_purpose] = frozen_fallbacks
        object.__setattr__(self, "routes", tuple(route_map.items()))
        object.__setattr__(self, "allowed_fallbacks", tuple(fallback_map.items()))

    def route_for(self, purpose: SpecialistPurpose) -> RouteBinding:
        """Lookup route binding for the given specialist purpose."""
        for p, binding in self.routes:
            if p == purpose:
                return binding
        raise KeyError(f"no route bound for {purpose.value}")

    def fallbacks_for(self, purpose: SpecialistPurpose) -> tuple[RouteSpec, ...]:
        """Lookup allowed fallback routes for the given specialist purpose."""
        for p, fallbacks in self.allowed_fallbacks:
            if p == purpose:
                return fallbacks
        return ()

    def permits_route(self, purpose: SpecialistPurpose, binding: RouteBinding) -> bool:
        """Return whether a frozen preferred binding or approved specialist fallback is usable."""
        if not isinstance(purpose, SpecialistPurpose) or not isinstance(binding, RouteBinding):
            return False
        try:
            preferred = self.route_for(purpose)
        except KeyError:
            return False
        if binding == preferred:
            return True
        return (
            not preferred.is_primary
            and not binding.is_primary
            and binding.requested == preferred.requested
            and binding.effective in self.fallbacks_for(purpose)
            and binding.effective.auth_mode == preferred.effective.auth_mode
            and binding.effective.billing_mode == preferred.effective.billing_mode
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskLineage:
    """Task audit lineage separating task hierarchy from provider session."""

    task_id: UUID
    lineage_root_task_id: UUID
    parent_task_id: UUID | None = None
    provider_session_id: str | None = None
    fresh_conversation: bool = True

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.task_id, "task_id")
        _validate_non_nil_uuid(self.lineage_root_task_id, "lineage_root_task_id")
        if self.parent_task_id is not None:
            _validate_non_nil_uuid(self.parent_task_id, "parent_task_id")
        if self.provider_session_id is not None:
            _validate_non_blank_text(self.provider_session_id, "provider_session_id")
        _validate_bool(self.fresh_conversation, "fresh_conversation")


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptTelemetry:
    """Attempt-level telemetry with nullable unknown values (never defaulted to 0)."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    duration_ms: int = 0
    tool_call_count: int = 0
    named_check_count: int = 0
    estimated_api_cost_minor: int | None = None
    currency: str | None = None
    subscription_allowance_charge: str | None = None
    quota_status: QuotaStatus = QuotaStatus.UNKNOWN
    unknown_telemetry_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, val in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("cached_input_tokens", self.cached_input_tokens),
            ("estimated_api_cost_minor", self.estimated_api_cost_minor),
        ):
            if val is not None and (type(val) is not int or val < 0):
                raise ValueError(f"{name} must be a nonnegative integer or None")
        for name, val in (
            ("duration_ms", self.duration_ms),
            ("tool_call_count", self.tool_call_count),
            ("named_check_count", self.named_check_count),
        ):
            if type(val) is not int or val < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name, value in (
            ("currency", self.currency),
            ("subscription_allowance_charge", self.subscription_allowance_charge),
        ):
            if value is not None:
                _validate_non_blank_text(value, name, max_length=4096)
        if not isinstance(self.quota_status, QuotaStatus):
            raise TypeError("quota_status must be a QuotaStatus")
        object.__setattr__(
            self,
            "unknown_telemetry_reasons",
            _freeze_texts(self.unknown_telemetry_reasons, "unknown_telemetry_reason"),
        )

    @property
    def is_token_telemetry_known(self) -> bool:
        return self.input_tokens is not None and self.output_tokens is not None

    @property
    def is_cost_known(self) -> bool:
        return self.estimated_api_cost_minor is not None

    @property
    def is_quota_known(self) -> bool:
        return self.quota_status is not QuotaStatus.UNKNOWN


@dataclass(frozen=True, slots=True, kw_only=True)
class UnknownTelemetryPolicy:
    """Policy bounding allowed unknown telemetry for billing and auditing."""

    allow_unknown_tokens: bool = True
    allow_unknown_cost: bool = True
    allow_unknown_quota: bool = True
    max_uncertain_attempts: int = 1

    def validate_telemetry(self, telemetry: AttemptTelemetry, billing_mode: BillingMode) -> None:
        """Validate telemetry against policy and billing mode."""
        if billing_mode is BillingMode.PAID_OPT_IN and telemetry.estimated_api_cost_minor is None:
            raise ValueError("paid opt-in billing mode requires explicit cost telemetry")
        if not self.allow_unknown_tokens and not telemetry.is_token_telemetry_known:
            raise ValueError("unknown token telemetry is disallowed by policy")
        if not self.allow_unknown_cost and not telemetry.is_cost_known:
            raise ValueError("unknown cost telemetry is disallowed by policy")
        if not self.allow_unknown_quota and not telemetry.is_quota_known:
            raise ValueError("unknown quota telemetry is disallowed by policy")

    def __post_init__(self) -> None:
        _validate_bool(self.allow_unknown_tokens, "allow_unknown_tokens")
        _validate_bool(self.allow_unknown_cost, "allow_unknown_cost")
        _validate_bool(self.allow_unknown_quota, "allow_unknown_quota")
        if type(self.max_uncertain_attempts) is not int or self.max_uncertain_attempts < 0:
            raise ValueError("max_uncertain_attempts must be a nonnegative integer")


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskBudget:
    """Budget limits for task execution, retries, and concurrent reservations."""

    max_duration_seconds: int = 1800
    max_tool_calls: int = 100
    max_named_checks: int = 10
    max_provider_attempts: int = 3
    max_repairs: int = 3
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_minor: int | None = None
    billing_mode: BillingMode = BillingMode.ALLOWANCE_ONLY
    unknown_telemetry_policy: UnknownTelemetryPolicy = field(default_factory=UnknownTelemetryPolicy)

    def __post_init__(self) -> None:
        for name in (
            "max_duration_seconds",
            "max_tool_calls",
            "max_named_checks",
            "max_provider_attempts",
            "max_repairs",
        ):
            val = getattr(self, name)
            if type(val) is not int or val < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in ("max_input_tokens", "max_output_tokens", "max_cost_minor"):
            val = getattr(self, name)
            if val is not None and (type(val) is not int or val < 0):
                raise ValueError(f"{name} must be a nonnegative integer or None")
        if not isinstance(self.billing_mode, BillingMode):
            raise TypeError("billing_mode must be a BillingMode")
        if not isinstance(self.unknown_telemetry_policy, UnknownTelemetryPolicy):
            raise TypeError("unknown_telemetry_policy must be an UnknownTelemetryPolicy")


@dataclass(frozen=True, slots=True, kw_only=True)
class BudgetPool:
    """Pure checked arithmetic; persistence must serialize reservations transactionally."""

    total_budget: TaskBudget
    reserved_duration_seconds: int = 0
    reserved_tool_calls: int = 0
    reserved_named_checks: int = 0
    reserved_provider_attempts: int = 0
    reserved_repairs: int = 0
    reserved_input_tokens: int = 0
    reserved_output_tokens: int = 0
    reserved_cost_minor: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.total_budget, TaskBudget):
            raise TypeError("total_budget must be a TaskBudget")
        for name in fields(self):
            if name.name.startswith("reserved_"):
                value = getattr(self, name.name)
                if type(value) is not int or value < 0:
                    raise ValueError(f"{name.name} must be a nonnegative integer")

    def can_reserve(self, requested: TaskBudget) -> bool:
        """Check if requested budget can be reserved from available capacity."""
        if (
            self.reserved_duration_seconds + requested.max_duration_seconds
            > self.total_budget.max_duration_seconds
        ):
            return False
        if self.reserved_tool_calls + requested.max_tool_calls > self.total_budget.max_tool_calls:
            return False
        if (
            self.reserved_named_checks + requested.max_named_checks
            > self.total_budget.max_named_checks
        ):
            return False
        if (
            self.reserved_provider_attempts + requested.max_provider_attempts
            > self.total_budget.max_provider_attempts
        ):
            return False
        for total, reserved, requested_limit in (
            (self.total_budget.max_repairs, self.reserved_repairs, requested.max_repairs),
            (
                self.total_budget.max_input_tokens,
                self.reserved_input_tokens,
                requested.max_input_tokens,
            ),
            (
                self.total_budget.max_output_tokens,
                self.reserved_output_tokens,
                requested.max_output_tokens,
            ),
            (self.total_budget.max_cost_minor, self.reserved_cost_minor, requested.max_cost_minor),
        ):
            if total is None:
                continue
            if requested_limit is None or reserved + requested_limit > total:
                return False
        return True

    def reserve(self, requested: TaskBudget) -> BudgetPool:
        """Return checked arithmetic result; DB row locking supplies atomicity."""
        if not self.can_reserve(requested):
            raise ValueError("requested budget exceeds available capacity")
        return BudgetPool(
            total_budget=self.total_budget,
            reserved_duration_seconds=self.reserved_duration_seconds
            + requested.max_duration_seconds,
            reserved_tool_calls=self.reserved_tool_calls + requested.max_tool_calls,
            reserved_named_checks=self.reserved_named_checks + requested.max_named_checks,
            reserved_provider_attempts=self.reserved_provider_attempts
            + requested.max_provider_attempts,
            reserved_repairs=self.reserved_repairs + requested.max_repairs,
            reserved_input_tokens=self.reserved_input_tokens + (requested.max_input_tokens or 0),
            reserved_output_tokens=self.reserved_output_tokens + (requested.max_output_tokens or 0),
            reserved_cost_minor=self.reserved_cost_minor + (requested.max_cost_minor or 0),
        )

    def release(self, released: TaskBudget) -> BudgetPool:
        """Release a known reservation; underflow is rejected rather than clamped."""
        amounts = {
            "reserved_duration_seconds": released.max_duration_seconds,
            "reserved_tool_calls": released.max_tool_calls,
            "reserved_named_checks": released.max_named_checks,
            "reserved_provider_attempts": released.max_provider_attempts,
            "reserved_repairs": released.max_repairs,
            "reserved_input_tokens": released.max_input_tokens or 0,
            "reserved_output_tokens": released.max_output_tokens or 0,
            "reserved_cost_minor": released.max_cost_minor or 0,
        }
        if any(getattr(self, name) < amount for name, amount in amounts.items()):
            raise ValueError("cannot release an unreserved budget")
        return BudgetPool(
            total_budget=self.total_budget,
            **{name: getattr(self, name) - amount for name, amount in amounts.items()},
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptanceCriterion:
    """Bounded typed acceptance condition, independent of untrusted task prose."""

    criterion_id: str
    description: str
    required_check_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_non_blank_text(self.criterion_id, "criterion_id", max_length=96)
        _validate_non_blank_text(self.description, "description", max_length=2_000)
        checks = _freeze_texts(self.required_check_names, "required_check_name", maximum=32)
        for check in checks:
            if _COMMAND_NAME.fullmatch(check) is None:
                raise ValueError(f"required check {check!r} is invalid")
        object.__setattr__(self, "required_check_names", checks)


@dataclass(frozen=True, slots=True, kw_only=True)
class LogicalTaskContract:
    """Logical delegation task contract persisted independently of provider attempts."""

    run_id: UUID
    task_id: UUID
    purpose: SpecialistPurpose
    route: RouteBinding
    budget: TaskBudget
    parent_task_id: UUID | None = None
    dependency_task_ids: tuple[UUID, ...] = ()
    owned_paths: tuple[str, ...] = ()
    typed_acceptance: tuple[AcceptanceCriterion, ...] = ()
    named_checks: tuple[str, ...] = ()
    untrusted_context_refs: tuple[str, ...] = ()
    max_repairs: int = 3

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.run_id, "run_id")
        _validate_non_nil_uuid(self.task_id, "task_id")
        if self.parent_task_id is not None:
            _validate_non_nil_uuid(self.parent_task_id, "parent_task_id")
            if self.parent_task_id == self.task_id:
                raise ValueError("task cannot be its own parent")
        if not isinstance(self.purpose, SpecialistPurpose):
            raise TypeError("purpose must be a SpecialistPurpose")
        if not isinstance(self.route, RouteBinding):
            raise TypeError("route must be a RouteBinding")
        if (
            self.purpose is SpecialistPurpose.PRIMARY
            and self.route.requested != self.route.effective
        ):
            raise ValueError("primary route substitution is prohibited")
        if not isinstance(self.budget, TaskBudget):
            raise TypeError("budget must be a TaskBudget")
        if type(self.max_repairs) is not int or self.max_repairs < 0:
            raise ValueError("max_repairs must be a nonnegative integer")

        deps = tuple(self.dependency_task_ids)
        for dep in deps:
            _validate_non_nil_uuid(dep, "dependency_task_id")
            if dep == self.task_id:
                raise ValueError("task cannot depend on itself")
        if len(deps) != len(set(deps)):
            raise ValueError("dependency_task_ids must not contain duplicates")
        object.__setattr__(self, "dependency_task_ids", deps)

        normalized_paths = normalize_policy_paths(self.owned_paths)
        if is_read_only(self.purpose) and normalized_paths:
            raise ValueError(f"read-only specialist {self.purpose.value} cannot own writable paths")
        object.__setattr__(self, "owned_paths", normalized_paths)

        checks = tuple(self.named_checks)
        for check in checks:
            _validate_non_blank_text(check, "named_check")
            if _SHELL_META.search(check) or not _COMMAND_NAME.fullmatch(check):
                raise ValueError(f"named check {check!r} is invalid")
        object.__setattr__(self, "named_checks", checks)

        context_refs = tuple(self.untrusted_context_refs)
        validate_durable_payload(list(context_refs))
        object.__setattr__(self, "untrusted_context_refs", context_refs)

        acceptance = tuple(self.typed_acceptance)
        if len(acceptance) > 64:
            raise ValueError("typed_acceptance exceeds maximum item count of 64")
        for criterion in acceptance:
            if not isinstance(criterion, AcceptanceCriterion):
                raise TypeError("typed_acceptance must contain AcceptanceCriterion instances")
        if len({criterion.criterion_id for criterion in acceptance}) != len(acceptance):
            raise ValueError("typed_acceptance must not contain duplicate criterion IDs")
        object.__setattr__(self, "typed_acceptance", acceptance)


def validate_task_dag(tasks: Sequence[LogicalTaskContract]) -> None:
    """Validate task DAG for uniqueness, common run, acyclicity, and scope containment."""
    if not tasks:
        return

    task_map: dict[UUID, LogicalTaskContract] = {}
    run_id = tasks[0].run_id

    for task in tasks:
        if not isinstance(task, LogicalTaskContract):
            raise TypeError("tasks must contain only LogicalTaskContract instances")
        if task.task_id in task_map:
            raise ValueError(f"duplicate task identifier {task.task_id}")
        if task.run_id != run_id:
            raise ValueError("tasks contain mismatched run identifiers (foreign lineage)")
        task_map[task.task_id] = task

    for task in tasks:
        # Parent validation
        if task.parent_task_id is not None:
            parent = task_map.get(task.parent_task_id)
            if parent is None:
                raise ValueError(
                    f"task {task.task_id} parent {task.parent_task_id} does not exist in run"
                )
            if not can_delegate(parent.purpose):
                raise ValueError(
                    f"parent task {parent.task_id} with purpose {parent.purpose.value} cannot delegate"
                )
            # Scope containment: child owned paths must be a subset of parent owned paths
            for child_path in task.owned_paths:
                if not any(
                    policy_path_key(child_path) == policy_path_key(parent_path)
                    or policy_path_key(child_path).startswith(policy_path_key(parent_path) + "/")
                    for parent_path in parent.owned_paths
                ):
                    raise ValueError(
                        f"child task {task.task_id} escalates scope with path {child_path}"
                    )

        # Dependency validation
        for dep_id in task.dependency_task_ids:
            if dep_id not in task_map:
                raise ValueError(
                    f"task {task.task_id} depends on task {dep_id} which does not exist in run"
                )

    for task in tasks:
        ancestors: set[UUID] = set()
        current = task
        while current.parent_task_id is not None:
            if current.parent_task_id in ancestors or current.parent_task_id == task.task_id:
                raise ValueError("task parents contain a cycle")
            ancestors.add(current.parent_task_id)
            current = task_map[current.parent_task_id]

    # Cycle detection via topological sort (Kahn's algorithm)
    in_degree: dict[UUID, int] = {t.task_id: 0 for t in tasks}
    graph: dict[UUID, list[UUID]] = {t.task_id: [] for t in tasks}

    for task in tasks:
        for dep_id in task.dependency_task_ids:
            graph[dep_id].append(task.task_id)
            in_degree[task.task_id] += 1

    zero_degree = [tid for tid, deg in in_degree.items() if deg == 0]
    visited_count = 0

    while zero_degree:
        curr = zero_degree.pop()
        visited_count += 1
        for successor in graph[curr]:
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                zero_degree.append(successor)

    if visited_count != len(tasks):
        raise ValueError("task dependencies contain a cycle")

    roots = [task for task in tasks if task.parent_task_id is None]
    primaries = [task for task in roots if task.purpose is SpecialistPurpose.PRIMARY]
    if len(primaries) != 1:
        raise ValueError("run must contain exactly one primary root task")
    if len(roots) != 1:
        raise ValueError("run must not contain worker root tasks")


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptIdentity:
    """Typed attempt identity distinct from logical task identity."""

    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    attempt_number: int = 1

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.run_id, "run_id")
        _validate_non_nil_uuid(self.task_id, "task_id")
        _validate_non_nil_uuid(self.attempt_id, "attempt_id")
        if type(self.attempt_number) is not int or self.attempt_number < 1:
            raise ValueError("attempt_number must be an integer >= 1")


@dataclass(frozen=True, slots=True, kw_only=True)
class BrokerAuthorizationBinding:
    """Per-attempt broker authority binding preventing caller-selected authority."""

    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    worktree_id: str
    role: SpecialistPurpose
    policy_version: int
    permitted_tools: frozenset[ToolName]
    broker_token: str = field(repr=False)

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.run_id, "run_id")
        _validate_non_nil_uuid(self.task_id, "task_id")
        _validate_non_nil_uuid(self.attempt_id, "attempt_id")
        if not isinstance(self.worktree_id, str) or not _WORKTREE_ID.fullmatch(self.worktree_id):
            raise ValueError("invalid worktree_id")
        if not isinstance(self.role, SpecialistPurpose):
            raise TypeError("role must be a SpecialistPurpose")
        if type(self.policy_version) is not int or self.policy_version < 1:
            raise ValueError("policy_version must be an integer >= 1")
        _validate_non_blank_text(self.broker_token, "broker_token")

        permitted_tools = frozenset(self.permitted_tools)
        allowed = SPECIALIST_ALLOWED_TOOLS.get(self.role, frozenset())
        for tool in permitted_tools:
            if not isinstance(tool, ToolName):
                raise TypeError("permitted_tools must contain ToolName instances")
            if tool not in allowed:
                raise ValueError(
                    f"tool {tool.value} exceeds allowed tool permissions for {self.role.value}"
                )
        object.__setattr__(self, "permitted_tools", permitted_tools)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallBinding:
    """Mapping of ephemeral provider-call key to durable Forge operation identity."""

    attempt_id: UUID
    provider_call_key: str
    durable_operation_id: UUID
    tool_name: ToolName
    arguments_digest: str
    admitted_at: datetime | None = None
    receipt_id: UUID | None = None

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.attempt_id, "attempt_id")
        _validate_non_blank_text(self.provider_call_key, "provider_call_key")
        _validate_non_nil_uuid(self.durable_operation_id, "durable_operation_id")
        if not isinstance(self.tool_name, ToolName):
            raise TypeError("tool_name must be a ToolName")
        _validate_sha256_digest(self.arguments_digest, "arguments_digest")
        if self.receipt_id is not None:
            _validate_non_nil_uuid(self.receipt_id, "receipt_id")


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckResultEvidence:
    """Actual command execution evidence returned by worker validation."""

    command_name: str
    exit_code: int
    passed: bool
    output_digest: str
    duration_ms: int
    receipt_id: str | None = None

    def __post_init__(self) -> None:
        _validate_non_blank_text(self.command_name, "command_name")
        if type(self.exit_code) is not int:
            raise TypeError("exit_code must be an integer")
        if type(self.passed) is not bool:
            raise TypeError("passed must be a boolean")
        if self.passed != (self.exit_code == 0):
            raise ValueError("passed must agree with exit_code")
        _validate_sha256_digest(self.output_digest, "output_digest")
        if type(self.duration_ms) is not int or self.duration_ms < 0:
            raise ValueError("duration_ms must be a nonnegative integer")
        if self.receipt_id is not None:
            _validate_non_blank_text(self.receipt_id, "receipt_id")


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskHandoff:
    """Compact structured handoff from worker to primary with evidence references."""

    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    status: HandoffStatus
    candidate_commit: str | None = None
    candidate_tree_digest: str = ""
    changed_paths: tuple[str, ...] = ()
    check_results: tuple[CheckResultEvidence, ...] = ()
    evidence_receipt_ids: tuple[str, ...] = ()
    residual_concerns: tuple[str, ...] = ()
    summary: str = ""
    scope_request_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.run_id, "run_id")
        _validate_non_nil_uuid(self.task_id, "task_id")
        _validate_non_nil_uuid(self.attempt_id, "attempt_id")
        if not isinstance(self.status, HandoffStatus):
            raise TypeError("status must be a HandoffStatus")
        if self.candidate_commit is not None and not _COMMIT_SHA.fullmatch(self.candidate_commit):
            raise ValueError("candidate_commit must be a 40-character hex commit SHA")

        normalized = normalize_policy_paths(self.changed_paths)
        object.__setattr__(self, "changed_paths", normalized)

        if self.scope_request_paths:
            normalized_scope = normalize_policy_paths(self.scope_request_paths)
            object.__setattr__(self, "scope_request_paths", normalized_scope)

        results = tuple(self.check_results)
        if len(results) > 128 or any(
            not isinstance(result, CheckResultEvidence) for result in results
        ):
            raise TypeError("check_results must contain at most 128 CheckResultEvidence instances")
        object.__setattr__(self, "check_results", results)
        object.__setattr__(
            self,
            "evidence_receipt_ids",
            _freeze_texts(self.evidence_receipt_ids, "evidence_receipt_id"),
        )
        object.__setattr__(
            self, "residual_concerns", _freeze_texts(self.residual_concerns, "residual_concern")
        )
        if self.summary:
            _validate_non_blank_text(self.summary, "summary", max_length=10_000)

        if self.status is HandoffStatus.COMPLETED:
            if not self.candidate_tree_digest:
                raise ValueError("completed handoff requires candidate_tree_digest")
            _validate_sha256_digest(self.candidate_tree_digest, "candidate_tree_digest")
            if not self.evidence_receipt_ids:
                raise ValueError("completed handoff requires evidence_receipt_ids")
        elif self.candidate_tree_digest:
            _validate_sha256_digest(self.candidate_tree_digest, "candidate_tree_digest")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedTaskHandoff(TaskHandoff):
    """A review's evidence handoff and typed report, never a human approval."""

    review_output: ReviewOutput

    def __post_init__(self) -> None:
        TaskHandoff.__post_init__(self)
        if not isinstance(self.review_output, ReviewOutput):
            raise TypeError("review_output must be a ReviewOutput")
        object.__setattr__(
            self,
            "review_output",
            ReviewOutput.model_validate(self.review_output.model_dump(mode="json")),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DelegateDecision:
    """Primary decision creating child tasks for specialist workers."""

    run_id: UUID
    parent_task_id: UUID
    child_tasks: tuple[LogicalTaskContract, ...]
    rationale: str

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.run_id, "run_id")
        _validate_non_nil_uuid(self.parent_task_id, "parent_task_id")
        _validate_non_blank_text(self.rationale, "rationale")
        child_tasks = tuple(self.child_tasks)
        if not child_tasks:
            raise ValueError("child_tasks must not be empty")
        for child in child_tasks:
            if not isinstance(child, LogicalTaskContract):
                raise TypeError("child_tasks must contain LogicalTaskContract instances")
            if child.parent_task_id != self.parent_task_id:
                raise ValueError("child task parent_task_id must match decision parent_task_id")
            if child.run_id != self.run_id:
                raise ValueError("child task run_id must match decision run_id")
        object.__setattr__(self, "child_tasks", child_tasks)


@dataclass(frozen=True, slots=True, kw_only=True)
class WaitDecision:
    """Primary decision yielding capacity while child tasks complete."""

    run_id: UUID
    task_id: UUID
    waiting_on_task_ids: tuple[UUID, ...]
    reason: str

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.run_id, "run_id")
        _validate_non_nil_uuid(self.task_id, "task_id")
        _validate_non_blank_text(self.reason, "reason")
        waiting_on = tuple(self.waiting_on_task_ids)
        if not waiting_on:
            raise ValueError("waiting_on_task_ids must not be empty")
        for tid in waiting_on:
            _validate_non_nil_uuid(tid, "waiting_on_task_id")
        if len(waiting_on) != len(set(waiting_on)):
            raise ValueError("waiting_on_task_ids must not contain duplicates")
        object.__setattr__(self, "waiting_on_task_ids", waiting_on)


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeRequestDecision:
    """Worker decision requesting additional path reservations."""

    run_id: UUID
    task_id: UUID
    requested_paths: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.run_id, "run_id")
        _validate_non_nil_uuid(self.task_id, "task_id")
        _validate_non_blank_text(self.reason, "reason")
        normalized = normalize_policy_paths(self.requested_paths)
        if not normalized:
            raise ValueError("requested_paths must not be empty")
        object.__setattr__(self, "requested_paths", normalized)


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeResponseDecision:
    """Primary decision granting or denying worker scope change requests."""

    run_id: UUID
    task_id: UUID
    granted_paths: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.run_id, "run_id")
        _validate_non_nil_uuid(self.task_id, "task_id")
        _validate_non_blank_text(self.reason, "reason")
        object.__setattr__(self, "granted_paths", normalize_policy_paths(self.granted_paths))
        object.__setattr__(self, "denied_paths", normalize_policy_paths(self.denied_paths))


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundScopeResponseDecision(ScopeResponseDecision):
    """A new response names the exact stopped worker request it answers."""

    request_attempt_id: UUID

    def __post_init__(self) -> None:
        ScopeResponseDecision.__post_init__(self)
        _validate_non_nil_uuid(self.request_attempt_id, "request_attempt_id")
        if not self.granted_paths and not self.denied_paths:
            raise ValueError("scope response must answer requested paths")
        if {policy_path_key(path) for path in self.granted_paths} & {
            policy_path_key(path) for path in self.denied_paths
        }:
            raise ValueError("scope response cannot grant and deny the same path")


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptDecision:
    """Primary acceptance of a child's handoff or its own integrated candidate.

    task_id names the accepted subject, not necessarily the producing primary.
    Child acceptance retains historical handoff claims; self acceptance requires
    current candidate evidence. Neither substitutes for final human gates.
    """

    run_id: UUID
    task_id: UUID
    candidate_commit: str | None
    candidate_tree_digest: str
    evidence_receipt_ids: tuple[str, ...]
    rationale: str

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.run_id, "run_id")
        _validate_non_nil_uuid(self.task_id, "task_id")
        _validate_sha256_digest(self.candidate_tree_digest, "candidate_tree_digest")
        _validate_non_blank_text(self.rationale, "rationale")
        if self.candidate_commit is not None and not _COMMIT_SHA.fullmatch(self.candidate_commit):
            raise ValueError("candidate_commit must be a 40-character hex commit SHA")
        if not self.evidence_receipt_ids:
            raise ValueError("acceptance requires evidence_receipt_ids (no fabricated approval)")
        for rid in self.evidence_receipt_ids:
            _validate_non_blank_text(rid, "evidence_receipt_id")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReassignDecision:
    """Primary decision reassigning a failed, blocked or exhausted task to a new route."""

    run_id: UUID
    task_id: UUID
    new_route: RouteSpec
    reason: str
    preserve_partial_work: bool = True

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.run_id, "run_id")
        _validate_non_nil_uuid(self.task_id, "task_id")
        if not isinstance(self.new_route, RouteSpec):
            raise TypeError("new_route must be a RouteSpec")
        _validate_non_blank_text(self.reason, "reason")
        _validate_bool(self.preserve_partial_work, "preserve_partial_work")


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundReassignDecision(ReassignDecision):
    """Reassignment names the stopped attempt and observed logical task version.

    The older record remains decodable but grants no new execution authority.
    """

    source_attempt_id: UUID
    expected_task_version: int

    def __post_init__(self) -> None:
        ReassignDecision.__post_init__(self)
        _validate_non_nil_uuid(self.source_attempt_id, "source_attempt_id")
        if type(self.expected_task_version) is not int or self.expected_task_version < 0:
            raise ValueError("expected_task_version must be a non-negative integer")


@dataclass(frozen=True, slots=True, kw_only=True)
class ForwardFeedbackDecision:
    """Primary acknowledgement forwarding one exact durable operator receipt."""

    run_id: UUID
    task_id: UUID
    feedback_receipt_id: UUID
    feedback_digest: str

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.run_id, "run_id")
        _validate_non_nil_uuid(self.task_id, "task_id")
        _validate_non_nil_uuid(self.feedback_receipt_id, "feedback_receipt_id")
        _validate_sha256_digest(self.feedback_digest, "feedback_digest")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewSelection:
    """Candidate-bound review selection decision including explicit no-review reason."""

    run_id: UUID
    candidate_commit: str | None
    candidate_tree_digest: str
    review_required: bool
    no_review_reason: str | None = None
    reviewer_route: RouteSpec | None = None
    review_task_id: UUID | None = None
    complexity_reason: str | None = None
    exceptional_maximum_effort: bool = False

    def __post_init__(self) -> None:
        _validate_non_nil_uuid(self.run_id, "run_id")
        _validate_sha256_digest(self.candidate_tree_digest, "candidate_tree_digest")
        _validate_bool(self.review_required, "review_required")
        _validate_bool(self.exceptional_maximum_effort, "exceptional_maximum_effort")
        if self.candidate_commit is not None and not _COMMIT_SHA.fullmatch(self.candidate_commit):
            raise ValueError("candidate_commit must be a 40-character hex commit SHA")

        if self.review_required:
            if self.reviewer_route is None:
                raise ValueError("review_required requires reviewer_route")
            if not isinstance(self.reviewer_route, RouteSpec):
                raise TypeError("reviewer_route must be a RouteSpec")
            if self.no_review_reason is not None:
                raise ValueError("review_required cannot specify no_review_reason")
            if self.review_task_id is not None:
                _validate_non_nil_uuid(self.review_task_id, "review_task_id")
            if self.reviewer_route.effort is ReasoningEffort.HIGH and not self.complexity_reason:
                raise ValueError("high-effort review requires complexity_reason")
            if (
                self.reviewer_route.effort is ReasoningEffort.MAXIMUM
                and not self.exceptional_maximum_effort
            ):
                raise ValueError("maximum-effort review requires explicit exceptional opt-in")
        else:
            if not self.no_review_reason or not self.no_review_reason.strip():
                raise ValueError("no-review selection requires an explicit no_review_reason")
            if self.reviewer_route is not None:
                raise ValueError("no-review selection cannot specify reviewer_route")
            if self.review_task_id is not None:
                raise ValueError("no-review selection cannot specify review_task_id")
            if self.complexity_reason is not None or self.exceptional_maximum_effort:
                raise ValueError("no-review selection cannot specify review effort details")


_SERIALIZATION_VERSION = 1
_MAX_SERIALIZATION_DEPTH = 64
_MAX_SERIALIZATION_ITEMS = 10_000
_RECORD_TYPES: Mapping[str, type[object]] = MappingProxyType(
    {
        cls.__name__: cls
        for cls in (
            RouteSpec,
            RouteBinding,
            ModelMapping,
            RouteMapping,
            RolePreference,
            OperatorProfile,
            ExecutionEnvelope,
            TaskLineage,
            AttemptTelemetry,
            UnknownTelemetryPolicy,
            TaskBudget,
            BudgetPool,
            AcceptanceCriterion,
            LogicalTaskContract,
            AttemptIdentity,
            ToolCallBinding,
            CheckResultEvidence,
            TaskHandoff,
            ReviewedTaskHandoff,
            DelegateDecision,
            WaitDecision,
            ScopeRequestDecision,
            ScopeResponseDecision,
            BoundScopeResponseDecision,
            AcceptDecision,
            ReassignDecision,
            BoundReassignDecision,
            ForwardFeedbackDecision,
            ReviewSelection,
        )
    }
)
_ENUM_TYPES: Mapping[str, type[StrEnum]] = MappingProxyType(
    {
        enum.__name__: enum
        for enum in (
            SpecialistPurpose,
            Capability,
            ReasoningEffort,
            AuthMode,
            BillingMode,
            QuotaStatus,
            FailureReason,
            HandoffStatus,
            DecisionKind,
            ToolName,
        )
    }
)


def _validate_serialization_shape(
    value: object, *, depth: int = 0, items: list[int] | None = None
) -> None:
    if depth > _MAX_SERIALIZATION_DEPTH:
        raise ValueError("subscription record encoding exceeds maximum nesting depth")
    counter = items if items is not None else [0]
    counter[0] += 1
    if counter[0] > _MAX_SERIALIZATION_ITEMS:
        raise ValueError("subscription record encoding exceeds maximum item count")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("subscription record encoding keys must be strings")
            _validate_serialization_shape(item, depth=depth + 1, items=counter)
    elif isinstance(value, list):
        for item in value:
            _validate_serialization_shape(item, depth=depth + 1, items=counter)
    elif value is not None and type(value) not in {str, int, bool}:
        raise ValueError("invalid subscription record encoding")


def _subscription_record_payload(record: object) -> dict[str, object]:
    """Build the closed encoding in memory; callers enforce their trust boundary."""
    if type(record).__name__ not in _RECORD_TYPES or not is_dataclass(record):
        raise TypeError("record must be a subscription domain dataclass")

    def encode(value: object) -> object:
        if isinstance(value, ReviewOutput):
            return {"$review_output": value.model_dump(mode="json")}
        if isinstance(value, StrEnum):
            return {"$enum": type(value).__name__, "value": value.value}
        if isinstance(value, UUID):
            return {"$uuid": str(value)}
        if isinstance(value, datetime):
            return {"$datetime": value.isoformat()}
        if type(value).__name__ in _RECORD_TYPES and is_dataclass(value):
            return {
                "$record": type(value).__name__,
                "fields": [
                    [field.name, encode(getattr(value, field.name))] for field in fields(value)
                ],
            }
        if isinstance(value, (tuple, frozenset)):
            return {"$tuple": [encode(item) for item in value]}
        if isinstance(value, Mapping):
            return {"$mapping": [[encode(key), encode(item)] for key, item in value.items()]}
        if value is None or type(value) in {str, int, bool}:
            return value
        raise TypeError("record contains an unsupported serialization value")

    payload = {"schema_version": _SERIALIZATION_VERSION, "record": encode(record)}
    _validate_serialization_shape(payload)
    return payload


def subscription_record_fingerprint(record: object) -> str:
    """Hash a typed response without persisting potentially unsafe provider text."""
    payload = _subscription_record_payload(record)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def encode_subscription_record(record: object) -> dict[str, object]:
    """Encode a versioned record only when its content is safe to persist."""
    payload = _subscription_record_payload(record)
    validate_durable_payload(payload)
    return payload


def decode_subscription_record(payload: Mapping[str, object]) -> object:
    """Decode only the current explicit schema and known domain record types."""
    _validate_serialization_shape(payload)
    validate_durable_payload(payload)
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"schema_version", "record"}
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != _SERIALIZATION_VERSION
    ):
        raise ValueError("unsupported subscription record schema version")
    raw = payload["record"]

    def decode(value: object) -> object:
        if not isinstance(value, Mapping):
            if value is None or type(value) in {str, int, bool}:
                return value
            raise ValueError("invalid subscription record encoding")
        if set(value) == {"$review_output"}:
            return ReviewOutput.model_validate(value["$review_output"])
        if "$enum" in value:
            enum_name = value.get("$enum")
            enum = _ENUM_TYPES.get(enum_name) if isinstance(enum_name, str) else None
            if enum is None or set(value) != {"$enum", "value"}:
                raise ValueError("invalid subscription enum encoding")
            return enum(value["value"])
        if "$uuid" in value and set(value) == {"$uuid"}:
            return UUID(str(value["$uuid"]))
        if "$datetime" in value and set(value) == {"$datetime"}:
            return datetime.fromisoformat(str(value["$datetime"]))
        if "$tuple" in value and set(value) == {"$tuple"} and isinstance(value["$tuple"], list):
            return tuple(decode(item) for item in value["$tuple"])
        if (
            "$mapping" in value
            and set(value) == {"$mapping"}
            and isinstance(value["$mapping"], list)
        ):
            decoded_mapping: dict[object, object] = {}
            for pair in value["$mapping"]:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ValueError("invalid subscription mapping encoding")
                key = decode(pair[0])
                try:
                    if key in decoded_mapping:
                        raise ValueError("subscription mapping encoding contains duplicate keys")
                    decoded_mapping[key] = decode(pair[1])
                except TypeError as error:
                    raise ValueError("invalid subscription mapping key") from error
            return MappingProxyType(decoded_mapping)
        if (
            "$record" in value
            and set(value) == {"$record", "fields"}
            and isinstance(value["fields"], list)
        ):
            record_name = value["$record"]
            cls = _RECORD_TYPES.get(record_name) if isinstance(record_name, str) else None
            if cls is None:
                raise ValueError("unknown subscription record type")
            encoded_fields: dict[str, object] = {}
            for pair in value["fields"]:
                if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
                    raise ValueError("invalid subscription record field shape")
                if pair[0] in encoded_fields:
                    raise ValueError("invalid subscription record field shape")
                encoded_fields[pair[0]] = pair[1]
            expected_fields = {field.name for field in fields(cls)}  # type: ignore[arg-type]
            if set(encoded_fields) != expected_fields:
                raise ValueError("invalid subscription record field shape")
            return cls(**{key: decode(item) for key, item in encoded_fields.items()})
        raise ValueError("invalid subscription record encoding")

    decoded = decode(raw)
    if type(decoded).__name__ not in _RECORD_TYPES or not is_dataclass(decoded):
        raise ValueError("subscription payload must decode to a record")
    return decoded


__all__ = [
    "CANDIDATE_READ_TOOLS",
    "SPECIALIST_ALLOWED_TOOLS",
    "SPECIALIST_CAPABILITIES",
    "AcceptDecision",
    "AcceptanceCriterion",
    "AttemptIdentity",
    "AttemptTelemetry",
    "AuthMode",
    "BillingMode",
    "BoundScopeResponseDecision",
    "BrokerAuthorizationBinding",
    "BudgetPool",
    "Capability",
    "CheckResultEvidence",
    "DecisionKind",
    "DelegateDecision",
    "ExecutionEnvelope",
    "FailureReason",
    "ForwardFeedbackDecision",
    "HandoffStatus",
    "LogicalTaskContract",
    "ModelMapping",
    "OperatorProfile",
    "QuotaStatus",
    "ReasoningEffort",
    "ReassignDecision",
    "ReviewSelection",
    "ReviewedTaskHandoff",
    "RolePreference",
    "RouteBinding",
    "RouteMapping",
    "RouteSpec",
    "ScopeRequestDecision",
    "ScopeResponseDecision",
    "SpecialistPurpose",
    "TaskBudget",
    "TaskHandoff",
    "TaskLineage",
    "ToolCallBinding",
    "UnknownTelemetryPolicy",
    "WaitDecision",
    "can_delegate",
    "decode_subscription_record",
    "encode_subscription_record",
    "is_read_only",
    "permits_fallback",
    "validate_task_dag",
    "validate_tool_permission",
]
