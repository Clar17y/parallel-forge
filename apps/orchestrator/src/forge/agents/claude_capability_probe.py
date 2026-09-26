"""Sanitized Claude subscription-auth status classification.

This deliberately small boundary is shared by the future bounded route probe
and the operator CLI.  It does not inspect a credential file or infer billing:
the only accepted fact is the official client's supported JSON status response.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from uuid import uuid4

from forge.agents.capability_publication import (
    AccountAuthenticationObservation,
    CapabilityObservationSet,
    ClientIdentityObservation,
    RouteIdentityObservation,
    SubscriptionRouteBindingObservation,
    ToolIsolationObservation,
)
from forge.agents.capability_verification import stable_executable_digest
from forge.agents.claude_conformance import ClaudeConformanceResult, ClaudeConformanceScope
from forge.agents.claude_gateway import (
    ClaudeCapabilityVerifier,
    ClaudeGateway,
    ClaudeInstallation,
    _QuotaState,
    _Usage,
)
from forge.agents.claude_verification import (
    CLAUDE_VERIFIER_ID,
    CLAUDE_VERIFIER_VERSION,
    required_claude_verification_scopes,
)
from forge.agents.client_process import (
    ClientLaunchSpec,
    ClientProcessError,
    ClientProcessResult,
    ClientProcessSupervisor,
    ClientProcessTimeout,
    ClientSettlementUncertain,
    terminal_launch_proof,
)
from forge.application.ports.subscription_gateway import SubscriptionInvocationRequest
from forge.domain.capability_evidence import CapabilityEvidenceScope, capability_home_digest
from forge.domain.subscription import (
    AttemptIdentity,
    BillingMode,
    BrokerAuthorizationBinding,
    ExecutionEnvelope,
    LogicalTaskContract,
    ReasoningEffort,
    RouteBinding,
    RouteSpec,
    TaskBudget,
)
from forge.domain.subscription_installations import is_account_identity_digest

_MAX_AUTH_STATUS_BYTES = 16 * 1024
_SUBSCRIPTION_TYPES = frozenset({"pro", "max", "team", "enterprise"})


class ClaudeCapabilityProbeError(RuntimeError):
    """A sanitized, non-publishable subscription-probe outcome."""


@dataclass(frozen=True, slots=True)
class ClaudeSubscriptionAuthentication:
    """The non-secret facts which can be carried into capability evidence."""

    account_digest: str
    subscription_type: str


class ClaudeAuthStatusRunner(Protocol):
    """Runs the supported metadata command in a caller-provided clean env."""

    async def run(self, installation: ClaudeInstallation, environment: Mapping[str, str]) -> str:
        """Return one JSON status document, without retaining raw output."""


class ClaudeOfflineConformance(Protocol):
    async def run(
        self, installation: ClaudeInstallation, scope: object
    ) -> ClaudeConformanceResult: ...


def claude_offline_scope(scope: CapabilityEvidenceScope) -> ClaudeConformanceScope:
    """Resolve the exact code-owned offline scope for a durable route scope."""

    matches = [
        item for item in required_claude_verification_scopes() if item.evidence_scope() == scope
    ]
    if len(matches) != 1:
        raise ClaudeCapabilityProbeError("offline_conformance_failed")
    return matches[0]


def validate_claude_offline_conformance(
    result: object,
    installation: ClaudeInstallation,
    scope: CapabilityEvidenceScope,
) -> ClaudeConformanceResult:
    """Validate a providerless observation without granting publish authority."""

    if not isinstance(result, ClaudeConformanceResult):
        raise ClaudeCapabilityProbeError("offline_conformance_failed")
    if result.scope.evidence_scope() != scope:
        raise ClaudeCapabilityProbeError("isolation_configuration_failed")
    if result.client_version != installation.client_version:
        raise ClaudeCapabilityProbeError("version_mismatch")
    if not hmac.compare_digest(result.executable_digest, installation.executable_digest):
        raise ClaudeCapabilityProbeError("executable_digest_mismatch")
    if not hmac.compare_digest(
        result.client_home_digest, capability_home_digest(installation.client_home)
    ):
        raise ClaudeCapabilityProbeError("isolation_configuration_failed")
    if result.account != installation.account:
        raise ClaudeCapabilityProbeError("account_identity_unbound")
    if not (
        result.configuration_isolated
        and result.only_forge_tools_advertised
        and result.callback_identity_bound
        and result.callback_result_forwarded
        and result.side_effects_absent
        and result.alternate_auth_isolated
        and result.terminal_proof.permits_decision
    ):
        raise ClaudeCapabilityProbeError("isolation_configuration_failed")
    return result


@dataclass(frozen=True, slots=True)
class ClaudeLiveRouteObservation:
    """Closed facts returned by the strict stream-json route executor."""

    reported_client_version: str
    model: str
    effort: str
    turn_digest: str
    tool_surface_digest: str


class ClaudeLiveRouteRunner(Protocol):
    async def run(
        self, installation: ClaudeInstallation, scope: CapabilityEvidenceScope
    ) -> ClaudeLiveRouteObservation: ...


@dataclass(slots=True)
class SupervisedClaudeLiveRouteRunner:
    """Execute the gateway's exact stream protocol once, with no fallback."""

    supervisor: ClientProcessSupervisor = field(default_factory=ClientProcessSupervisor, repr=False)

    async def run(
        self, installation: ClaudeInstallation, scope: CapabilityEvidenceScope
    ) -> ClaudeLiveRouteObservation:
        request = _probe_request(installation, scope)
        broker = _ProbeBroker()
        gateway = ClaudeGateway(
            installation,
            verifier=cast(ClaudeCapabilityVerifier, object()),
            broker=cast(Any, broker),
            supervisor=self.supervisor,
        )
        session = None
        completed = False
        cancelled = False
        try:
            spec = ClientLaunchSpec(
                argv=(installation.executable, *gateway._command(request)),
                cwd=installation.cwd,
                environment=claude_subscription_environment(installation),
                allowed_environment=frozenset(claude_subscription_environment(installation)),
                executable_digest=installation.executable_digest,
                duration_seconds=installation.duration_seconds,
            )
            async with asyncio.timeout(installation.duration_seconds):
                session = await self.supervisor.start(spec)
                result = await gateway._exchange(
                    session, request, _Usage(), _QuotaState(installation.quota_limit_types)
                )
                if result.failure is not None or result.decision is None or broker.calls:
                    raise ClaudeCapabilityProbeError("probe_protocol_failed")
                completed = True
                return ClaudeLiveRouteObservation(
                    installation.client_version,
                    installation.model,
                    installation.effort,
                    hashlib.sha256(repr(result.decision).encode("utf-8")).hexdigest(),
                    hashlib.sha256(
                        json.dumps(
                            [tool.value for tool in scope.tool_surface],
                            ensure_ascii=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                )
        except asyncio.CancelledError:
            cancelled = True
            raise
        except ClaudeCapabilityProbeError:
            raise
        except ClientProcessTimeout, TimeoutError:
            raise ClaudeCapabilityProbeError("probe_timeout") from None
        except ClientSettlementUncertain:
            raise ClaudeCapabilityProbeError("probe_stop_uncertain") from None
        except Exception:  # noqa: BLE001 - provider output is never exposed
            raise ClaudeCapabilityProbeError("probe_protocol_failed") from None
        finally:
            if session is not None:
                receipt = await _close_probe_session(
                    session, completed=completed, cancellation_pending=cancelled
                )
                if cancelled:
                    raise asyncio.CancelledError
                proof = terminal_launch_proof(receipt)
                if not proof.stop_confirmed or (completed and not proof.permits_decision):
                    raise ClaudeCapabilityProbeError("probe_stop_uncertain")


def _probe_request(
    installation: ClaudeInstallation, scope: CapabilityEvidenceScope
) -> SubscriptionInvocationRequest:
    """Create the fixed, no-tool route contract consumed by the gateway codec."""

    run_id, task_id, attempt_id = uuid4(), uuid4(), uuid4()
    route = RouteSpec(
        provider="anthropic",
        client="claude_code",
        model=installation.model,
        effort=ReasoningEffort(installation.effort),
    )
    binding = RouteBinding(requested=route, effective=route)
    budget = TaskBudget(
        max_duration_seconds=max(1, int(installation.duration_seconds)),
        max_tool_calls=0,
        max_named_checks=0,
        max_provider_attempts=1,
        max_repairs=0,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    task = LogicalTaskContract(
        run_id=run_id, task_id=task_id, purpose=scope.role, route=binding, budget=budget
    )
    attempt = AttemptIdentity(run_id=run_id, task_id=task_id, attempt_id=attempt_id)
    authorization = BrokerAuthorizationBinding(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worktree_id="forge-probe",
        role=scope.role,
        policy_version=1,
        permitted_tools=frozenset(scope.tool_surface),
        broker_token="probe",
    )
    return SubscriptionInvocationRequest(
        task=task,
        attempt=attempt,
        authorization=authorization,
        envelope=ExecutionEnvelope(
            run_id=run_id,
            profile_id=uuid4(),
            profile_version=1,
            safety_policy_version=1,
            routes=((scope.role, binding),),
        ),
        prompt_version="probe-v1",
        trusted_system_prompt="Return only a valid structured decision; do not call tools.",
        untrusted_context={"capability_probe": True},
    )


@dataclass(slots=True)
class _ProbeBroker:
    calls: int = 0

    async def __call__(self, _value: Any) -> Mapping[str, object]:
        self.calls += 1
        raise ClaudeCapabilityProbeError("unexpected_tool_callback")

    async def revoke(self) -> None:
        return None


@dataclass(slots=True)
class SupervisedClaudeAuthStatusRunner:
    """Run exactly the documented metadata command under the process supervisor."""

    supervisor: ClientProcessSupervisor = field(default_factory=ClientProcessSupervisor, repr=False)

    async def run(self, installation: ClaudeInstallation, environment: Mapping[str, str]) -> str:
        session = None
        completed = False
        cancelled = False
        try:
            spec = ClientLaunchSpec(
                argv=(installation.executable, "--setting-sources=", "auth", "status"),
                cwd=installation.cwd,
                environment=dict(environment),
                allowed_environment=frozenset(environment),
                executable_digest=installation.executable_digest,
                duration_seconds=min(installation.duration_seconds, 15.0),
                stdout_max_bytes=_MAX_AUTH_STATUS_BYTES,
                protocol="json_document",
            )
            async with asyncio.timeout(spec.duration_seconds):
                session = await self.supervisor.start(spec)
                await session.close_stdin()
                frame = await session.receive()
                if not isinstance(frame, Mapping):
                    raise ClaudeCapabilityProbeError("auth_status_invalid")
                payload = json.dumps(frame, sort_keys=True, separators=(",", ":"))
                if len(payload.encode("utf-8")) > _MAX_AUTH_STATUS_BYTES:
                    raise ClaudeCapabilityProbeError("auth_status_invalid")
                if await session.receive() is not None:
                    raise ClaudeCapabilityProbeError("auth_status_invalid")
                completed = True
                return payload
        except asyncio.CancelledError:
            cancelled = True
            raise
        except ClaudeCapabilityProbeError:
            raise
        except ClientProcessTimeout, TimeoutError:
            raise ClaudeCapabilityProbeError("probe_timeout") from None
        except ClientSettlementUncertain:
            raise ClaudeCapabilityProbeError("probe_stop_uncertain") from None
        except ClientProcessError, OSError, TypeError, ValueError:
            raise ClaudeCapabilityProbeError("auth_status_invalid") from None
        finally:
            if session is not None:
                receipt = await _close_probe_session(
                    session, completed=completed, cancellation_pending=cancelled
                )
                if cancelled:
                    raise asyncio.CancelledError
                proof = terminal_launch_proof(receipt)
                if not proof.stop_confirmed or (completed and not proof.permits_decision):
                    raise ClaudeCapabilityProbeError("probe_stop_uncertain")


async def _close_probe_session(
    session: Any, *, completed: bool, cancellation_pending: bool = False
) -> ClientProcessResult:
    """Await one close to completion without allowing cleanup to hide cancellation."""

    close_task = asyncio.create_task(session.close(completed=completed))
    wait_task = asyncio.create_task(asyncio.wait((close_task,)))
    interrupted = cancellation_pending
    while not wait_task.done():
        try:
            await asyncio.shield(wait_task)
        except asyncio.CancelledError:
            interrupted = True
    wait_task.result()
    try:
        receipt = close_task.result()
    except BaseException:
        if interrupted:
            raise asyncio.CancelledError from None
        raise
    if interrupted:
        raise asyncio.CancelledError
    return cast(ClientProcessResult, receipt)


@dataclass(slots=True)
class ClaudeCapabilityProbe:
    """Gate, offline proof, auth metadata and one exact subscription route."""

    installation: ClaudeInstallation
    scope: CapabilityEvidenceScope
    conformance: ClaudeOfflineConformance
    auth_status: ClaudeAuthStatusRunner
    live_route: ClaudeLiveRouteRunner

    async def observe(
        self, *, authorize_provider_contact: bool = False
    ) -> CapabilityObservationSet:
        if authorize_provider_contact is not True:
            raise ClaudeCapabilityProbeError("provider_contact_not_authorized")
        try:
            offline = await self.conformance.run(self.installation, self._conformance_scope())
            validate_claude_offline_conformance(offline, self.installation, self.scope)
            before = await asyncio.to_thread(stable_executable_digest, self.installation.executable)
            if not hmac.compare_digest(before or "", self.installation.executable_digest):
                raise ClaudeCapabilityProbeError("executable_digest_mismatch")
            auth = parse_claude_subscription_auth_status(
                await self.auth_status.run(
                    self.installation, claude_subscription_environment(self.installation)
                ),
                expected_account_digest=self.installation.account,
            )
            route = await self.live_route.run(self.installation, self.scope)
            if route.reported_client_version != self.installation.client_version:
                raise ClaudeCapabilityProbeError("version_mismatch")
            if route.model != self.installation.model or route.effort != self.installation.effort:
                raise ClaudeCapabilityProbeError("model_or_effort_unavailable")
            if not _digest_is_valid(route.turn_digest) or not _digest_is_valid(
                route.tool_surface_digest
            ):
                raise ClaudeCapabilityProbeError("probe_protocol_failed")
            after = await asyncio.to_thread(stable_executable_digest, self.installation.executable)
            if not hmac.compare_digest(after or "", self.installation.executable_digest):
                raise ClaudeCapabilityProbeError("executable_digest_mismatch")
            tools = tuple(tool.value for tool in self.scope.tool_surface)
            return CapabilityObservationSet(
                ClientIdentityObservation(
                    "claude_code",
                    self.installation.client_version,
                    before or "",
                    capability_home_digest(self.installation.client_home),
                    reported_client_version=route.reported_client_version,
                ),
                AccountAuthenticationObservation(
                    auth.account_digest, "subscription", "subscription"
                ),
                RouteIdentityObservation(
                    route.model, route.effort, turn_observation_digest=route.turn_digest
                ),
                SubscriptionRouteBindingObservation("subscription", "allowance_only", True, True),
                ToolIsolationObservation(
                    tools, True, advertised_tool_surface_digest=route.tool_surface_digest
                ),
                CLAUDE_VERIFIER_ID,
                CLAUDE_VERIFIER_VERSION,
            )
        except asyncio.CancelledError:
            raise
        except ClaudeCapabilityProbeError:
            raise
        except ClientProcessTimeout, TimeoutError:
            raise ClaudeCapabilityProbeError("probe_timeout") from None
        except ClientSettlementUncertain:
            raise ClaudeCapabilityProbeError("probe_stop_uncertain") from None
        except Exception:  # noqa: BLE001 - never expose provider/process details
            raise ClaudeCapabilityProbeError("probe_protocol_failed") from None

    def _conformance_scope(self) -> ClaudeConformanceScope:
        return claude_offline_scope(self.scope)


def _digest_is_valid(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def claude_subscription_environment(installation: ClaudeInstallation) -> dict[str, str]:
    """Construct the only environment permitted for stored claude.ai OAuth.

    The mapping is built from scratch.  It intentionally excludes API, cloud,
    gateway and host-managed-provider selectors; the latter suppresses the
    selected official config directory's stored OAuth state.
    """

    if not isinstance(installation, ClaudeInstallation):
        raise TypeError("Claude installation is required")
    return {
        "CLAUDE_CONFIG_DIR": installation.client_home,
        "CLAUDE_CODE_ENTRYPOINT": "local-agent",
    }


def parse_claude_subscription_auth_status(
    payload: str | bytes, *, expected_account_digest: str
) -> ClaudeSubscriptionAuthentication:
    """Validate supported status JSON without exposing a raw account identifier."""

    if not is_account_identity_digest(expected_account_digest):
        raise ClaudeCapabilityProbeError("account_identity_unbound")
    if isinstance(payload, bytes):
        if len(payload) > _MAX_AUTH_STATUS_BYTES:
            raise ClaudeCapabilityProbeError("auth_status_invalid")
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise ClaudeCapabilityProbeError("auth_status_invalid") from None
    if (
        type(payload) is not str
        or not payload
        or len(payload.encode("utf-8")) > _MAX_AUTH_STATUS_BYTES
    ):
        raise ClaudeCapabilityProbeError("auth_status_invalid")
    try:
        value = json.loads(payload)
    except TypeError, json.JSONDecodeError:
        raise ClaudeCapabilityProbeError("auth_status_invalid") from None
    if not isinstance(value, Mapping):
        raise ClaudeCapabilityProbeError("auth_status_invalid")
    if value.get("loggedIn") is not True:
        raise ClaudeCapabilityProbeError("subscription_signed_out")
    if value.get("authMethod") != "claude.ai" or value.get("apiProvider") != "firstParty":
        raise ClaudeCapabilityProbeError("subscription_authentication_failed")
    subscription_type = value.get("subscriptionType")
    if type(subscription_type) is not str or subscription_type not in _SUBSCRIPTION_TYPES:
        raise ClaudeCapabilityProbeError("subscription_type_unrecognized")
    identity = _status_identity(value)
    if identity is None:
        raise ClaudeCapabilityProbeError("account_identity_missing")
    actual = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual, expected_account_digest):
        raise ClaudeCapabilityProbeError("account_identity_unbound")
    return ClaudeSubscriptionAuthentication(actual, subscription_type)


def _status_identity(value: Mapping[str, object]) -> str | None:
    """Pick exactly one bounded documented account identifier, never a label."""

    candidates: list[str] = []
    for key in ("accountId", "email", "id"):
        item = value.get(key)
        if type(item) is str and 1 <= len(item.encode("utf-8")) <= 512:
            candidates.append(item)
    account = value.get("account")
    if isinstance(account, Mapping):
        for key in ("id", "email"):
            item = account.get(key)
            if type(item) is str and 1 <= len(item.encode("utf-8")) <= 512:
                candidates.append(item)
    unique = tuple(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else None


__all__ = [
    "ClaudeAuthStatusRunner",
    "ClaudeCapabilityProbe",
    "ClaudeCapabilityProbeError",
    "ClaudeLiveRouteObservation",
    "ClaudeLiveRouteRunner",
    "ClaudeOfflineConformance",
    "ClaudeSubscriptionAuthentication",
    "SupervisedClaudeAuthStatusRunner",
    "claude_offline_scope",
    "claude_subscription_environment",
    "parse_claude_subscription_auth_status",
    "validate_claude_offline_conformance",
]
