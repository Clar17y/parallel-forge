import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from forge.agents.claude_verification import (
    CLAUDE_VERIFIER_ID,
    CLAUDE_VERIFIER_VERSION,
)
from forge.agents.codex_verification import CODEX_VERIFIER_ID, CODEX_VERIFIER_VERSION
from forge.application.ports.capability_evidence import (
    CapabilityEvidenceInvalid,
    CapabilityEvidenceMissing,
)
from forge.domain.capability_diagnostics import CapabilityProbeDiagnostic
from forge.domain.subscription import ReasoningEffort, RouteSpec
from forge.domain.subscription_installations import (
    ClaudeInstallationSpec,
    CodexInstallationSpec,
    GeminiInstallationSpec,
    InstallationQuota,
)
from forge.domain.subscription_quota import PoolQuotaStatus, QuotaPoolKey
from forge.domain.subscription_readiness import (
    ReadinessQuota,
    ReadinessReason,
    SubscriptionRouteReadiness,
)
from forge.worker.subscription_readiness import SubscriptionReadinessEnricher

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)
ACCOUNT = "a" * 64
EXECUTABLE_DIGEST = "b" * 64


class Resolved:
    def __init__(
        self,
        identity,
        *,
        verifier_id: str = CODEX_VERIFIER_ID,
        verifier_version: str = CODEX_VERIFIER_VERSION,
        revision: int = 3,
    ) -> None:
        self.identity = identity
        self.revision = revision
        self.manifest = SimpleNamespace(
            evidence_id=UUID("11111111-1111-4111-8111-111111111111"),
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            observed_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )

    def matches(self, identity) -> bool:
        return identity == self.identity

    def permits(self, scope) -> bool:
        return (self.identity.route, self.identity.role, self.identity.tool_surface) == (
            scope.route,
            scope.role,
            scope.tool_surface,
        )


class Evidence:
    def __init__(self, outcomes=()) -> None:
        self.outcomes = list(outcomes)
        self.identities = []

    async def resolve(self, identity):
        self.identities.append(identity)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "wrong_verifier":
            return Resolved(identity, verifier_id="wrong-verifier")
        if outcome == "claude":
            return Resolved(
                identity,
                verifier_id=CLAUDE_VERIFIER_ID,
                verifier_version=CLAUDE_VERIFIER_VERSION,
            )
        return Resolved(identity)


class Diagnostics:
    def __init__(self, reasons=()) -> None:
        self.reasons = list(reasons)
        self.identities = []

    async def resolve(self, identity):
        self.identities.append(identity)
        reason = self.reasons.pop(0) if self.reasons else None
        if isinstance(reason, Exception):
            raise reason
        if reason is None:
            return None
        return CapabilityProbeDiagnostic(
            identity_digest=identity.digest,
            reason=reason,
            revision=2,
            observed_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )


def quota(status: str = "eligible", *, revision: int = 5):
    calls = []

    async def read(key: QuotaPoolKey) -> PoolQuotaStatus:
        calls.append(key)
        return PoolQuotaStatus(
            key=key,
            revision=revision,
            status=status,  # type: ignore[arg-type]
            observed_at=NOW,
            reason=None,
            reset_at=None,
            next_eligible_at=None,
            retry_basis=None,
        )

    read.calls = calls  # type: ignore[attr-defined]
    return read


def codex_spec(tmp_path: Path, *, effort: str = "low") -> CodexInstallationSpec:
    home = tmp_path / f"codex-{effort}"
    home.mkdir()
    return CodexInstallationSpec(
        client="codex_app_server",
        executable=str(tmp_path / "codex.exe"),
        cwd=str(tmp_path),
        home=str(home),
        model="gpt-6-astra",
        effort=effort,
        account=ACCOUNT,
        executable_digest=EXECUTABLE_DIGEST,
        client_version="0.154.0",
        quota=InstallationQuota(account="personal", pool="weekly"),
    )


def readiness(spec, reason: ReadinessReason = ReadinessReason.EVIDENCE_MISSING):
    provider = {
        "codex_app_server": "openai",
        "claude_code": "anthropic",
        "gemini_cli": "google",
    }[spec.client]
    return SubscriptionRouteReadiness(
        RouteSpec(
            provider=provider,
            client=spec.client,
            model=spec.model,
            effort=ReasoningEffort(spec.effort),
        ),
        configured=True,
        admitted=spec.client != "gemini_cli",
        reason=reason,
    )


@pytest.mark.asyncio
async def test_codex_requires_every_scope_and_publishes_only_safe_bounded_refs(
    tmp_path: Path,
) -> None:
    spec = codex_spec(tmp_path)
    evidence = Evidence()
    quota_status = quota()

    result = (
        await SubscriptionReadinessEnricher(evidence, quota_status, [spec]).enrich(
            [readiness(spec)]
        )
    )[0]

    assert result.reason is ReadinessReason.READY
    assert result.quota is ReadinessQuota.ELIGIBLE and result.quota_revision == 5
    assert len(result.evidence) == len(evidence.identities) == 2
    assert {item.scope for item in result.evidence} == {
        "astra-primary",
        "astra-direct-repair",
    }
    assert all(item.revision == 3 for item in result.evidence)
    assert all(identity.account == ACCOUNT for identity in evidence.identities)
    assert all(identity.executable_digest == EXECUTABLE_DIGEST for identity in evidence.identities)
    assert all(identity.client_version == "0.154.0" for identity in evidence.identities)
    wire = result.wire()
    rendered = repr(wire)
    assert str(spec.home) not in rendered and ACCOUNT not in rendered
    assert len(wire["evidence"]) == 2
    assert quota_status.calls == [QuotaPoolKey("openai", "personal", "weekly")]  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcomes",
    [
        [CapabilityEvidenceMissing("missing"), CapabilityEvidenceInvalid("invalid")],
        [CapabilityEvidenceInvalid("invalid"), CapabilityEvidenceMissing("missing")],
        [OSError("database unavailable"), CapabilityEvidenceMissing("missing")],
    ],
)
async def test_invalid_scope_dominates_missing_regardless_of_order(
    tmp_path: Path, outcomes: list[Exception]
) -> None:
    spec = codex_spec(tmp_path)
    result = (
        await SubscriptionReadinessEnricher(Evidence(outcomes), quota(), [spec]).enrich(
            [readiness(spec)]
        )
    )[0]

    assert result.reason is ReadinessReason.EVIDENCE_STALE_OR_INVALID
    assert result.evidence == ()


@pytest.mark.asyncio
async def test_missing_scope_and_wrong_verifier_never_become_ready(tmp_path: Path) -> None:
    spec = codex_spec(tmp_path)
    missing = (
        await SubscriptionReadinessEnricher(
            Evidence([None, CapabilityEvidenceMissing("missing")]), quota(), [spec]
        ).enrich([readiness(spec)])
    )[0]
    wrong = (
        await SubscriptionReadinessEnricher(
            Evidence([None, "wrong_verifier"]), quota(), [spec]
        ).enrich([readiness(spec)])
    )[0]

    assert missing.reason is ReadinessReason.EVIDENCE_MISSING
    assert len(missing.evidence) == 1
    assert wrong.reason is ReadinessReason.EVIDENCE_STALE_OR_INVALID
    assert len(wrong.evidence) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reasons",
    [
        [ReadinessReason.SIGNED_OUT, ReadinessReason.ISOLATION_UNPROVED],
        [ReadinessReason.ISOLATION_UNPROVED, ReadinessReason.SIGNED_OUT],
    ],
)
async def test_current_probe_diagnostics_refine_missing_evidence_deterministically(
    tmp_path: Path, reasons: list[ReadinessReason]
) -> None:
    spec = codex_spec(tmp_path)
    diagnostics = Diagnostics(reasons)
    result = (
        await SubscriptionReadinessEnricher(
            Evidence(
                [
                    CapabilityEvidenceMissing("missing"),
                    CapabilityEvidenceMissing("missing"),
                ]
            ),
            quota(),
            [spec],
            diagnostics,
        ).enrich([readiness(spec)])
    )[0]

    assert result.reason is ReadinessReason.ISOLATION_UNPROVED
    assert len(diagnostics.identities) == 2


@pytest.mark.asyncio
async def test_diagnostic_ready_or_unavailable_never_substitutes_for_evidence(
    tmp_path: Path,
) -> None:
    spec = codex_spec(tmp_path)
    outcomes = [
        CapabilityEvidenceMissing("missing"),
        CapabilityEvidenceMissing("missing"),
    ]
    result = (
        await SubscriptionReadinessEnricher(
            Evidence(outcomes),
            quota(),
            [spec],
            Diagnostics([ReadinessReason.READY, OSError("database unavailable")]),
        ).enrich([readiness(spec)])
    )[0]

    assert result.reason is ReadinessReason.EVIDENCE_MISSING


@pytest.mark.asyncio
async def test_claude_uses_its_exact_verifier_and_quota_identity(tmp_path: Path) -> None:
    home = tmp_path / "claude-home"
    home.mkdir()
    spec = ClaudeInstallationSpec(
        client="claude_code",
        executable=str(tmp_path / "claude.exe"),
        cwd=str(tmp_path),
        home=str(home),
        model="claude-opus-5",
        effort="medium",
        account=ACCOUNT,
        executable_digest=EXECUTABLE_DIGEST,
        client_version="2.1.263",
        quota=InstallationQuota(account="review", pool="seven-day"),
    )
    evidence = Evidence(["claude"])
    quota_status = quota()

    result = (
        await SubscriptionReadinessEnricher(evidence, quota_status, [spec]).enrich(
            [readiness(spec)]
        )
    )[0]

    assert result.reason is ReadinessReason.READY and len(result.evidence) == 1
    assert quota_status.calls == [QuotaPoolKey("anthropic", "review", "seven-day")]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_quota_failure_is_unknown_and_cancellation_propagates(tmp_path: Path) -> None:
    spec = codex_spec(tmp_path)

    async def unavailable(_key):
        raise OSError("database unavailable")

    result = (
        await SubscriptionReadinessEnricher(Evidence(), unavailable, [spec]).enrich(
            [readiness(spec)]
        )
    )[0]
    assert result.reason is ReadinessReason.READY
    assert result.quota is ReadinessQuota.UNKNOWN and result.quota_revision == 0

    async def cancelled(_key):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await SubscriptionReadinessEnricher(Evidence(), cancelled, [spec]).enrich([readiness(spec)])


@pytest.mark.asyncio
async def test_effort_is_part_of_spec_matching_and_unsupported_scope_is_closed(
    tmp_path: Path,
) -> None:
    low = codex_spec(tmp_path, effort="low")
    medium = codex_spec(tmp_path, effort="medium")
    evidence = Evidence()

    result = (
        await SubscriptionReadinessEnricher(evidence, quota(), [low, medium]).enrich(
            [readiness(medium)]
        )
    )[0]

    assert result.reason is ReadinessReason.UNSUPPORTED_MODEL_OR_EFFORT
    assert evidence.identities == []


@pytest.mark.asyncio
async def test_gemini_readiness_remains_provider_unsupported_without_reads(
    tmp_path: Path,
) -> None:
    home = tmp_path / "gemini-home"
    home.mkdir()
    spec = GeminiInstallationSpec(
        client="gemini_cli",
        executable=str(tmp_path / "agy.exe"),
        cwd=str(tmp_path),
        home=str(home),
        model="gemini-3.8-flash",
        effort="medium",
        account="google",
        executable_digest=EXECUTABLE_DIGEST,
        client_version="1.2.7",
        quota=InstallationQuota(account="google", pool="allowance"),
    )
    evidence = Evidence()
    quota_status = quota()
    base = readiness(spec, ReadinessReason.PROVIDER_UNSUPPORTED)

    result = (await SubscriptionReadinessEnricher(evidence, quota_status, [spec]).enrich([base]))[0]

    assert result is base
    assert evidence.identities == [] and quota_status.calls == []  # type: ignore[attr-defined]
