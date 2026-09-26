"""Evidence-backed verification for the pinned official Codex client."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace

import pytest
from capability_support import fake_capability_evidence
from forge.agents.codex_gateway import (
    CODEX_MODEL_CATALOG_DIGEST,
    CodexInstallation,
    codex_account_identity,
)
from forge.agents.codex_verification import (
    CODEX_VERIFIER_ID,
    CODEX_VERIFIER_VERSION,
    CodexEvidenceVerifier,
    required_codex_verification_scopes,
)
from forge.domain.capability_evidence import CapabilityEvidenceScope
from forge.domain.subscription import RouteSpec
from forge.domain.tool import ToolName


def _installation(tmp_path) -> tuple[CodexInstallation, str]:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"pinned official codex fixture")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    home = tmp_path / "home"
    home.mkdir()
    scope = required_codex_verification_scopes()[1]
    return (
        CodexInstallation(
            executable=str(executable),
            cwd=str(tmp_path),
            model=scope.model,
            effort=scope.effort,
            client_home=str(home),
            account=codex_account_identity("codex@example.invalid"),
            executable_digest=digest,
        ),
        digest,
    )


def _evidence(
    installation,
    scope,
    digest,
    *,
    verifier_id=CODEX_VERIFIER_ID,
    verifier_version=CODEX_VERIFIER_VERSION,
):
    return fake_capability_evidence(
        scope=scope,
        client_version="0.153.4",
        executable_digest=digest,
        client_home=installation.client_home,
        account=installation.account,
        verifier_id=verifier_id,
        verifier_version=verifier_version,
    )


def _append_file(filename: str, payload: bytes) -> None:
    with open(filename, "ab") as stream:
        stream.write(payload)


def test_verifier_version_is_mechanically_bound_to_the_isolation_catalog() -> None:
    assert CODEX_VERIFIER_VERSION != "1"
    assert CODEX_VERIFIER_VERSION.endswith(CODEX_MODEL_CATALOG_DIGEST[:16])


async def test_concrete_verifier_hashes_the_executable_and_resolves_exact_evidence(tmp_path):
    installation, digest = _installation(tmp_path)
    scope = required_codex_verification_scopes()[1].evidence_scope()

    class Source:
        def __init__(self):
            self.identities = []

        async def resolve(self, identity, *, evidence_id=None):
            assert evidence_id is None
            self.identities.append(identity)
            return _evidence(installation, scope, digest)

    source = Source()
    report = await CodexEvidenceVerifier(source).verify(installation, scope)

    assert len(source.identities) == 1
    assert source.identities[0] == report.evidence.manifest.identity
    assert report.admits(installation, scope)


@pytest.mark.parametrize("change", ["script", "scope"])
async def test_unapproved_launch_or_scope_is_denied_before_evidence_lookup(tmp_path, change):
    installation, _ = _installation(tmp_path)
    scope = required_codex_verification_scopes()[1].evidence_scope()
    if change == "script":
        installation = replace(installation, script=("app-server", "--stdio", "--help"))
    else:
        scope = CapabilityEvidenceScope(
            route=scope.route,
            role=scope.role,
            tool_surface=(ToolName.REPOSITORY_READ_FILE,),
        )

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            raise AssertionError("unapproved identities must not reach the evidence source")

    report = await CodexEvidenceVerifier(Source()).verify(installation, scope)

    assert not report.supported and not report.admits(installation, scope)


async def test_executable_drift_is_denied_before_evidence_lookup(tmp_path):
    installation, _ = _installation(tmp_path)
    scope = required_codex_verification_scopes()[1].evidence_scope()
    await asyncio.to_thread(_append_file, installation.executable, b"changed")

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            raise AssertionError("changed executables must not reach the evidence source")

    report = await CodexEvidenceVerifier(Source()).verify(installation, scope)

    assert not report.supported and not report.admits(installation, scope)


async def test_executable_change_during_evidence_lookup_is_denied(tmp_path):
    installation, digest = _installation(tmp_path)
    scope = required_codex_verification_scopes()[1].evidence_scope()

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            await asyncio.to_thread(_append_file, installation.executable, b"changed")
            return _evidence(installation, scope, digest)

    report = await CodexEvidenceVerifier(Source()).verify(installation, scope)

    assert not report.supported and not report.admits(installation, scope)


@pytest.mark.parametrize("failure", ["account", "verifier", "version", "type"])
async def test_untrusted_source_result_never_becomes_a_capability_report(tmp_path, failure):
    installation, digest = _installation(tmp_path)
    scope = required_codex_verification_scopes()[1].evidence_scope()

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            if failure == "type":
                return object()
            if failure == "account":
                changed = replace(
                    installation,
                    account=codex_account_identity("another@example.invalid"),
                )
                return _evidence(changed, scope, digest)
            if failure == "version":
                return _evidence(
                    installation,
                    scope,
                    digest,
                    verifier_version="foreign-policy",
                )
            return _evidence(installation, scope, digest, verifier_id="another-verifier")

    report = await CodexEvidenceVerifier(Source()).verify(installation, scope)

    assert not report.supported and not report.admits(installation, scope)


@pytest.mark.parametrize(
    "route",
    [
        RouteSpec(provider="another", client="codex_app_server", model="gpt-6-astra"),
        RouteSpec(provider="openai", client="another", model="gpt-6-astra"),
        RouteSpec(provider="openai", client="codex_app_server", model="another"),
    ],
)
async def test_route_identity_drift_is_denied_before_evidence_lookup(tmp_path, route):
    installation, _ = _installation(tmp_path)
    allowed = required_codex_verification_scopes()[1].evidence_scope()
    scope = CapabilityEvidenceScope(
        route=route,
        role=allowed.role,
        tool_surface=allowed.tool_surface,
    )

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            raise AssertionError("changed routes must not reach the evidence source")

    report = await CodexEvidenceVerifier(Source()).verify(installation, scope)

    assert not report.supported and not report.admits(installation, scope)
