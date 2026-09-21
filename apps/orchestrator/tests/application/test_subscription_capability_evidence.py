from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest
from forge.agents.capability_publication import (
    AccountAuthenticationObservation,
    CapabilityObservationSet,
    ClientIdentityObservation,
    RouteIdentityObservation,
    SubscriptionRouteBindingObservation,
    ToolIsolationObservation,
)
from forge.application.services.subscription_capability_evidence import (
    CapabilityPublicationError,
    SubscriptionCapabilityEvidenceService,
)
from forge.domain.capability_evidence import CapabilityEvidenceIdentity
from forge.domain.subscription import AuthMode, BillingMode, ReasoningEffort, SpecialistPurpose


class A:
    def __init__(s):
        s.d = {}

    async def put_bytes(s, b, *, media_type, max_bytes=None):
        x = sha256(b).hexdigest()
        s.d[x] = b
        return SimpleNamespace(digest=x, media_type=media_type, byte_count=len(b), truncated=False)

    async def verify(s, x):
        return x in s.d


class P:
    async def publish(s, m):
        return m


def i():
    return CapabilityEvidenceIdentity(
        provider="openai",
        client="codex_app_server",
        client_version="0.153.4",
        executable_digest="a" * 64,
        client_home_digest="b" * 64,
        account="account-digest",
        model="gpt-6-astra",
        effort=ReasoningEffort.LOW,
        role=SpecialistPurpose.PRIMARY,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )


def o(x):
    return CapabilityObservationSet(
        ClientIdentityObservation(
            x.client,
            x.client_version,
            x.executable_digest,
            x.client_home_digest,
            True,
            x.client_version,
        ),
        AccountAuthenticationObservation(x.account, "subscription", "chatgpt", True),
        RouteIdentityObservation(x.model, x.effort.value, True, True, "c" * 64),
        SubscriptionRouteBindingObservation("subscription", "allowance_only", True, True, True),
        ToolIsolationObservation(
            (), True, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945", 0, True
        ),
    )


@pytest.mark.asyncio
async def test_binds_and_rejects_mismatch_before_writes():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    a = A()
    s = SubscriptionCapabilityEvidenceService(a, P(), clock=lambda: now)
    m = await s.publish(
        identity=i(),
        observations=o(i()),
        observed_at=now,
    )
    assert len(m.proofs) == 5 and all(
        b'"identity_digest"' in a.d[p.artifact_digest] for p in m.proofs
    )
    bad = o(i())
    bad = CapabilityObservationSet(
        bad.client_identity,
        AccountAuthenticationObservation("other", "subscription"),
        bad.route_identity,
        bad.subscription_route_binding,
        bad.tool_isolation,
    )
    with pytest.raises(CapabilityPublicationError):
        await s.publish(
            identity=i(),
            observations=bad,
            observed_at=now,
        )
    assert len(a.d) == 5


@pytest.mark.asyncio
async def test_ttl_and_replay():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    s = SubscriptionCapabilityEvidenceService(A(), P(), clock=lambda: now)
    x = await s.publish(
        identity=i(),
        observations=o(i()),
        observed_at=now,
    )
    y = await s.publish(
        identity=i(),
        observations=o(i()),
        observed_at=now,
    )
    assert x.evidence_id == y.evidence_id
    with pytest.raises(CapabilityPublicationError):
        await s.publish(
            identity=i(),
            observations=o(i()),
            observed_at=now,
            ttl=timedelta(hours=25),
        )
