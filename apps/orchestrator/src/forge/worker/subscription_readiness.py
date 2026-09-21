"""Read-only enrichment of boot diagnostics from retained local evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable

from forge.agents.claude_verification import (
    CLAUDE_VERIFIER_ID,
    CLAUDE_VERIFIER_VERSION,
    required_claude_verification_scopes,
)
from forge.agents.codex_verification import (
    CODEX_VERIFIER_ID,
    CODEX_VERIFIER_VERSION,
    required_codex_verification_scopes,
)
from forge.application.ports.capability_diagnostics import CapabilityProbeDiagnosticSource
from forge.application.ports.capability_evidence import (
    CapabilityEvidenceInvalid,
    CapabilityEvidenceMissing,
    CapabilityEvidenceSource,
    CapabilityEvidenceUnavailable,
)
from forge.domain.capability_evidence import CapabilityEvidenceIdentity, capability_identity
from forge.domain.subscription_installations import (
    CodexInstallationSpec,
    GeminiInstallationSpec,
    SubscriptionInstallationSpec,
    quota_route_for,
)
from forge.domain.subscription_quota import PoolQuotaStatus, QuotaPoolKey
from forge.domain.subscription_readiness import (
    EvidenceReference,
    ReadinessQuota,
    ReadinessReason,
    SubscriptionRouteReadiness,
)


class SubscriptionReadinessEnricher:
    def __init__(
        self,
        evidence: CapabilityEvidenceSource,
        quota_status: Callable[[QuotaPoolKey], Awaitable[PoolQuotaStatus]],
        specs: Iterable[SubscriptionInstallationSpec],
        diagnostics: CapabilityProbeDiagnosticSource | None = None,
    ) -> None:
        self._evidence, self._quota = evidence, quota_status
        self._specs = tuple(specs)
        self._diagnostics = diagnostics

    async def enrich(
        self, base: Iterable[SubscriptionRouteReadiness]
    ) -> tuple[SubscriptionRouteReadiness, ...]:
        by_route: dict[tuple[str, str, str, str], SubscriptionInstallationSpec] = {
            (quota_route_for(item).provider, item.client, item.model, item.effort): item
            for item in self._specs
        }
        return tuple(
            [
                await self._one(
                    value,
                    by_route.get(
                        (
                            value.route.provider,
                            value.route.client,
                            value.route.model,
                            value.route.effort.value,
                        )
                    ),
                )
                for value in base
            ]
        )

    async def _one(
        self, value: SubscriptionRouteReadiness, spec: SubscriptionInstallationSpec | None
    ) -> SubscriptionRouteReadiness:
        if spec is None or value.reason not in {
            ReadinessReason.EVIDENCE_MISSING,
            ReadinessReason.READY,
        }:
            return value
        if isinstance(spec, GeminiInstallationSpec):
            return value
        scopes, verifier = (
            (required_codex_verification_scopes(), (CODEX_VERIFIER_ID, CODEX_VERIFIER_VERSION))
            if isinstance(spec, CodexInstallationSpec)
            else (
                required_claude_verification_scopes(),
                (CLAUDE_VERIFIER_ID, CLAUDE_VERIFIER_VERSION),
            )
        )
        applicable = [
            scope for scope in scopes if (scope.model, scope.effort) == (spec.model, spec.effort)
        ]
        references: list[EvidenceReference] = []
        missing = invalid = False
        diagnostic_reasons: list[ReadinessReason] = []
        for scope in applicable:
            identity = None
            try:
                identity = capability_identity(
                    scope=scope.evidence_scope(),
                    client_version=spec.client_version,
                    executable_digest=spec.executable_digest,
                    client_home=spec.home,
                    account=spec.account,
                )
                resolved = await self._evidence.resolve(identity)
                if (
                    not resolved.matches(identity)
                    or not resolved.permits(scope.evidence_scope())
                    or (resolved.manifest.verifier_id, resolved.manifest.verifier_version)
                    != verifier
                ):
                    invalid = True
                    continue
                references.append(
                    EvidenceReference(
                        scope.name,
                        str(resolved.manifest.evidence_id),
                        resolved.revision,
                        resolved.manifest.observed_at,
                        resolved.manifest.expires_at,
                    )
                )
            except CapabilityEvidenceMissing:
                diagnostic = await self._diagnostic(identity)
                if diagnostic is None or diagnostic is ReadinessReason.READY:
                    missing = True
                else:
                    diagnostic_reasons.append(diagnostic)
            except CapabilityEvidenceInvalid, CapabilityEvidenceUnavailable:
                invalid = True
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - readiness must fail closed on source errors
                invalid = True
        if not applicable:
            reason = ReadinessReason.UNSUPPORTED_MODEL_OR_EFFORT
        elif invalid:
            reason = ReadinessReason.EVIDENCE_STALE_OR_INVALID
        elif diagnostic_reasons:
            reason = min(diagnostic_reasons, key=_diagnostic_priority)
        elif missing:
            reason = ReadinessReason.EVIDENCE_MISSING
        else:
            reason = ReadinessReason.READY
        selector = quota_route_for(spec)
        key = QuotaPoolKey(selector.provider, selector.account, selector.pool)
        try:
            status = await self._quota(key)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - quota outages degrade to an explicit unknown state
            status = PoolQuotaStatus(
                key=key,
                revision=0,
                status="unknown",
                observed_at=None,
                reason=None,
                reset_at=None,
                next_eligible_at=None,
                retry_basis=None,
            )
        return SubscriptionRouteReadiness(
            value.route,
            value.configured,
            value.admitted,
            reason,
            ReadinessQuota(status.status),
            tuple(references),
            status.revision,
            status.reset_at,
            status.next_eligible_at,
        )

    async def _diagnostic(
        self, identity: CapabilityEvidenceIdentity | None
    ) -> ReadinessReason | None:
        if self._diagnostics is None or identity is None:
            return None
        try:
            value = await self._diagnostics.resolve(identity)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - optional diagnostics cannot block boot reporting
            return None
        return None if value is None else value.reason


_DIAGNOSTIC_PRIORITY = {
    ReadinessReason.MISSING_EXECUTABLE: 0,
    ReadinessReason.EXECUTABLE_DIGEST_MISMATCH: 1,
    ReadinessReason.VERSION_MISMATCH: 2,
    ReadinessReason.ISOLATION_UNPROVED: 3,
    ReadinessReason.SUBSCRIPTION_ROUTE_UNBOUND: 4,
    ReadinessReason.ACCOUNT_AUTHENTICATION_UNPROVED: 5,
    ReadinessReason.SIGNED_OUT: 6,
    ReadinessReason.UNSUPPORTED_MODEL_OR_EFFORT: 7,
    ReadinessReason.CONFIGURATION_INVALID: 8,
    ReadinessReason.EVIDENCE_STALE_OR_INVALID: 9,
    ReadinessReason.UNKNOWN: 10,
    ReadinessReason.EVIDENCE_MISSING: 11,
    ReadinessReason.READY: 12,
}


def _diagnostic_priority(reason: ReadinessReason) -> int:
    return _DIAGNOSTIC_PRIORITY.get(reason, 100)
