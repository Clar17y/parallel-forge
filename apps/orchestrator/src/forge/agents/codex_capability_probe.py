"""Explicitly-authorized, bounded Codex subscription capability observation.

This module deliberately has no fallback route.  It is a production probe, not
an invocation adapter: a failure produces no observations and retains no raw
account, prompt, output, thread, or provider error data.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from forge.agents.capability_publication import (
    AccountAuthenticationObservation,
    CapabilityObservationSet,
    ClientIdentityObservation,
    RouteIdentityObservation,
    SubscriptionRouteBindingObservation,
    ToolIsolationObservation,
)
from forge.agents.client_process import (
    ClientLaunchSpec,
    ClientProcessError,
    ClientProcessSession,
    ClientProcessSupervisor,
    ClientProcessTimeout,
    ClientSettlementUncertain,
    terminal_launch_proof,
)
from forge.agents.codex_conformance import (
    CodexConformanceResult,
    CodexConformanceScope,
    required_codex_live_scopes,
)
from forge.agents.codex_gateway import (
    CODEX_MODEL_CATALOG_ARGUMENT,
    CodexInstallation,
    codex_account_identity,
    codex_configuration_arguments,
    codex_configuration_matches,
    codex_dynamic_tools,
    codex_isolation_configuration,
    codex_model_catalog_pin,
)
from forge.agents.codex_verification import CODEX_VERIFIER_ID, CODEX_VERIFIER_VERSION
from forge.agents.subscription_protocol import ProtocolError
from forge.domain.capability_evidence import CapabilityEvidenceScope, capability_home_digest
from forge.domain.subscription import AuthMode, BillingMode


class CodexCapabilityProbeError(RuntimeError):
    """A sanitized, non-publishable probe outcome."""


class CodexOfflineConformance(Protocol):
    async def run(
        self, installation: CodexInstallation, scope: object
    ) -> CodexConformanceResult: ...


def codex_offline_scope(scope: CapabilityEvidenceScope) -> CodexConformanceScope:
    """Resolve the exact code-owned offline scope for a durable route scope."""

    matches = [item for item in required_codex_live_scopes() if item.evidence_scope() == scope]
    if len(matches) != 1:
        raise CodexCapabilityProbeError("offline_conformance_failed")
    return matches[0]


def validate_codex_offline_conformance(
    result: object,
    installation: CodexInstallation,
    scope: CapabilityEvidenceScope,
) -> CodexConformanceResult:
    """Validate a providerless observation without granting publish authority."""

    if not isinstance(result, CodexConformanceResult):
        raise CodexCapabilityProbeError("offline_conformance_failed")
    expected_home = capability_home_digest(installation.client_home)
    proof = result.terminal_proof
    if result.scope.evidence_scope() != scope:
        raise CodexCapabilityProbeError("isolation_configuration_failed")
    if result.client_version != installation.client_version:
        raise CodexCapabilityProbeError("version_mismatch")
    if not hmac.compare_digest(result.executable_digest, installation.executable_digest):
        raise CodexCapabilityProbeError("executable_digest_mismatch")
    if not hmac.compare_digest(result.client_home_digest, expected_home):
        raise CodexCapabilityProbeError("isolation_configuration_failed")
    if result.account != installation.account:
        raise CodexCapabilityProbeError("account_identity_unbound")
    if not (
        result.environmentless
        and result.configuration_isolated
        and result.only_forge_tools_advertised
        and result.callback_identity_bound
        and result.callback_result_forwarded
        and result.side_effects_absent
        and not result.credentials_sent
        and proof.permits_decision
    ):
        raise CodexCapabilityProbeError("isolation_configuration_failed")
    return result


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


@dataclass(slots=True)
class CodexCapabilityProbe:
    """Observe one exact ChatGPT subscription route after an explicit opt-in."""

    installation: CodexInstallation
    scope: CapabilityEvidenceScope
    conformance: CodexOfflineConformance
    supervisor: ClientProcessSupervisor = field(default_factory=ClientProcessSupervisor, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.installation, CodexInstallation) or not isinstance(
            self.scope, CapabilityEvidenceScope
        ):
            raise TypeError("Codex probe requires an installation and capability scope")
        route = self.scope.route
        if (
            route.provider != "openai"
            or route.client != "codex_app_server"
            or route.model != self.installation.model
            or route.effort.value != self.installation.effort
            or route.auth_mode is not AuthMode.SUBSCRIPTION
            or route.billing_mode is not BillingMode.ALLOWANCE_ONLY
        ):
            raise ValueError("Codex probe scope differs from installation")
        if not callable(getattr(self.conformance, "run", None)) or not callable(
            getattr(self.supervisor, "start", None)
        ):
            raise TypeError("Codex probe dependencies are invalid")

    async def observe(
        self, *, authorize_provider_contact: bool = False
    ) -> CapabilityObservationSet:
        """Return observations only after a complete, terminally-confirmed turn.

        The authorization check intentionally precedes even offline conformance
        and construction of a launch specification so accidental callers cannot
        cause process activity.
        """
        if authorize_provider_contact is not True:
            raise CodexCapabilityProbeError("provider_contact_not_authorized")
        try:
            offline = await self.conformance.run(self.installation, self._conformance_scope())
            validate_codex_offline_conformance(offline, self.installation, self.scope)
            return await self._live_observation()
        except asyncio.CancelledError:
            raise
        except CodexCapabilityProbeError:
            raise
        except ClientProcessTimeout, TimeoutError:
            raise CodexCapabilityProbeError("probe_timeout") from None
        except ClientSettlementUncertain:
            raise CodexCapabilityProbeError("probe_stop_uncertain") from None
        except ClientProcessError, ProtocolError, ValueError, TypeError, KeyError:
            raise CodexCapabilityProbeError("probe_protocol_failed") from None
        except Exception:  # noqa: BLE001 - provider failures must not leak into evidence
            raise CodexCapabilityProbeError("probe_failed") from None

    def _conformance_scope(self) -> CodexConformanceScope:
        """Map the durable capability scope to its named offline harness scope."""
        return codex_offline_scope(self.scope)

    async def _live_observation(self) -> CapabilityObservationSet:
        expected_digest = self.installation.executable_digest
        # ClientLaunchSpec verifies the executable at start; this explicit
        # before/after check closes the window around a completed observation.
        from forge.agents.codex_verification import codex_executable_digest

        actual_digest = await asyncio.to_thread(
            codex_executable_digest, self.installation.executable
        )
        if not hmac.compare_digest(actual_digest or "", expected_digest):
            raise CodexCapabilityProbeError("executable_digest_mismatch")
        config = codex_isolation_configuration(self.installation)
        session: ClientProcessSession | None = None
        completed = False
        try:
            spec = ClientLaunchSpec(
                argv=(
                    self.installation.executable,
                    *self.installation.script,
                    *codex_configuration_arguments(config),
                ),
                cwd=self.installation.cwd,
                environment={"CODEX_HOME": self.installation.client_home},
                allowed_environment=frozenset({"CODEX_HOME"}),
                executable_digest=expected_digest,
                pinned_files=(codex_model_catalog_pin(),),
                duration_seconds=self.installation.duration_seconds,
            )
            async with asyncio.timeout(self.installation.duration_seconds):
                session = await self.supervisor.start(spec)
                pinned = session.pinned_path(CODEX_MODEL_CATALOG_ARGUMENT)
                config["model_catalog_json"] = pinned
                initialize = await self._rpc(
                    session,
                    1,
                    "initialize",
                    {
                        "clientInfo": {"name": "forge-capability-probe", "version": "0.2"},
                        "capabilities": {"experimentalApi": True},
                    },
                )
                if not self._reported_version_matches(initialize.get("userAgent")):
                    raise CodexCapabilityProbeError("version_mismatch")
                await session.send({"method": "initialized", "params": {}})
                account = await self._rpc(session, 2, "account/read", {"refreshToken": False})
                account_value = account.get("account")
                if not isinstance(account_value, Mapping) or account_value.get("type") != "chatgpt":
                    raise CodexCapabilityProbeError("subscription_authentication_failed")
                identity = account_value.get("email") or account_value.get("id")
                if type(identity) is not str or not hmac.compare_digest(
                    codex_account_identity(identity), self.installation.account
                ):
                    raise CodexCapabilityProbeError("subscription_authentication_failed")
                models = await self._rpc(
                    session, 3, "model/list", {"includeHidden": True, "limit": 200}
                )
                if not self._catalog_supports(models):
                    raise CodexCapabilityProbeError("model_or_effort_unavailable")
                configured = await self._rpc(
                    session,
                    4,
                    "config/read",
                    {"cwd": self.installation.cwd, "includeLayers": False},
                )
                if not codex_configuration_matches(
                    configured, config, allow_unreported_request_user_input=True
                ):
                    raise CodexCapabilityProbeError("isolation_configuration_failed")
                thread = await self._rpc(
                    session,
                    5,
                    "thread/start",
                    {
                        "model": self.installation.model,
                        "allowProviderModelFallback": False,
                        "environments": [],
                        "ephemeral": True,
                        "cwd": self.installation.cwd,
                        "config": config,
                        "baseInstructions": "Return the required fixed JSON only.",
                        "developerInstructions": "Do not call tools.",
                        "dynamicTools": codex_dynamic_tools(self.scope.tool_surface),
                    },
                )
                thread_id = self._id(thread.get("thread"))
                if thread.get("model") != self.installation.model:
                    raise CodexCapabilityProbeError("model_or_effort_unavailable")
                turn = await self._rpc(
                    session,
                    6,
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": 'Return {\\"ok\\":true}.'}],
                        "model": self.installation.model,
                        "effort": self.installation.effort,
                        "environments": [],
                        "outputSchema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                    },
                )
                turn_id = self._id(turn.get("turn"))
                observation = await self._complete(session, thread_id, turn_id)
                completed = True
        finally:
            # Never replace the original launch/RPC/cancellation error merely
            # because no session was acquired.  A session that did start must
            # always be settled before its result can influence publication.
            if session is not None:
                result = await session.close(completed=completed)
                if not terminal_launch_proof(result).permits_decision:
                    raise CodexCapabilityProbeError("probe_stop_uncertain")
        actual = await asyncio.to_thread(codex_executable_digest, self.installation.executable)
        if actual is None or not hmac.compare_digest(actual, expected_digest):
            raise CodexCapabilityProbeError("executable_digest_mismatch")
        tools = tuple(tool.value for tool in self.scope.tool_surface)
        return CapabilityObservationSet(
            client_identity=ClientIdentityObservation(
                "codex_app_server",
                self.installation.client_version,
                expected_digest,
                capability_home_digest(self.installation.client_home),
                reported_client_version=self.installation.client_version,
            ),
            account_authentication=AccountAuthenticationObservation(
                self.installation.account, "subscription", "chatgpt"
            ),
            route_identity=RouteIdentityObservation(
                self.installation.model,
                self.installation.effort,
                turn_observation_digest=_digest(observation),
            ),
            subscription_route_binding=SubscriptionRouteBindingObservation(
                "subscription", "allowance_only", True, True
            ),
            tool_isolation=ToolIsolationObservation(
                tools, True, advertised_tool_surface_digest=_digest(list(tools))
            ),
            verifier_id=CODEX_VERIFIER_ID,
            verifier_version=CODEX_VERIFIER_VERSION,
        )

    async def _rpc(
        self, session: ClientProcessSession, ident: int, method: str, params: Mapping[str, object]
    ) -> Mapping[str, Any]:
        await session.send({"id": ident, "method": method, "params": dict(params)})
        for _ in range(64):
            frame = await session.receive()
            if not isinstance(frame, Mapping):
                break
            if "id" not in frame:
                continue
            if (
                frame.get("id") == ident
                and "error" not in frame
                and isinstance(frame.get("result"), Mapping)
            ):
                return cast(Mapping[str, Any], frame["result"])
            break
        raise ProtocolError("probe RPC response differs")

    def _catalog_supports(self, models: Mapping[str, Any]) -> bool:
        data = models.get("data")
        matches = (
            [
                item
                for item in data
                if isinstance(item, Mapping)
                and item.get("model", item.get("id")) == self.installation.model
            ]
            if isinstance(data, list)
            else []
        )
        return len(matches) == 1 and any(
            isinstance(item, Mapping) and item.get("reasoningEffort") == self.installation.effort
            for item in matches[0].get("supportedReasoningEfforts", [])
        )

    async def _complete(
        self, session: ClientProcessSession, thread_id: str, turn_id: str
    ) -> dict[str, object]:
        result_marker = False
        for _ in range(256):
            frame = await session.receive()
            if not isinstance(frame, Mapping):
                break
            if frame.get("method") == "account/rateLimits/updated":
                continue
            params = frame.get("params")
            if not isinstance(params, Mapping) or params.get("threadId") != thread_id:
                raise ProtocolError("foreign probe thread")
            if "id" in frame or frame.get("method") == "item/tool/call":
                raise CodexCapabilityProbeError("unexpected_tool_callback")
            if params.get("turnId") not in (None, turn_id):
                raise ProtocolError("foreign probe turn")
            item = params.get("item")
            if frame.get("method") == "item/completed" and isinstance(item, Mapping):
                text = item.get("text")
                if item.get("type") == "agentMessage" and type(text) is str:
                    try:
                        result_marker = json.loads(text) == {"ok": True}
                    except json.JSONDecodeError:
                        raise CodexCapabilityProbeError("offline_conformance_failed") from None
            if frame.get("method") == "turn/completed":
                turn = params.get("turn")
                if (
                    not isinstance(turn, Mapping)
                    or self._id(turn) != turn_id
                    or turn.get("status") != "completed"
                    or turn.get("error") is not None
                    or not result_marker
                ):
                    raise CodexCapabilityProbeError("offline_conformance_failed")
                return {
                    "status": "completed",
                    "model": self.installation.model,
                    "effort": self.installation.effort,
                }
        raise ProtocolError("probe turn completion missing")

    @staticmethod
    def _id(value: object) -> str:
        ident = value.get("id") if isinstance(value, Mapping) else None
        if type(ident) is not str or not ident or len(ident.encode("utf-8")) > 255:
            raise ProtocolError("invalid probe identity")
        return ident

    def _reported_version_matches(self, user_agent: object) -> bool:
        return (
            type(user_agent) is str
            and re.search(
                rf"(?<![0-9.]){re.escape(self.installation.client_version)}(?![0-9.])",
                user_agent,
            )
            is not None
        )


__all__ = [
    "CodexCapabilityProbe",
    "CodexCapabilityProbeError",
    "CodexOfflineConformance",
    "codex_offline_scope",
    "validate_codex_offline_conformance",
]
