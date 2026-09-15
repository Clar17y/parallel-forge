"""Evidence-backed verification for the pinned official Claude client."""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from capability_support import fake_capability_evidence
from forge.agents.claude_gateway import CLAUDE_ISOLATION_POLICY_DIGEST, ClaudeInstallation
from forge.agents.claude_verification import (
    CLAUDE_VERIFIER_ID,
    CLAUDE_VERIFIER_VERSION,
    ClaudeEvidenceVerifier,
    required_claude_verification_scopes,
)
from forge.domain.capability_evidence import CapabilityEvidenceScope
from forge.domain.subscription import SPECIALIST_ALLOWED_TOOLS, RouteSpec, SpecialistPurpose
from forge.domain.tool import ToolName


def _installation(tmp_path) -> tuple[ClaudeInstallation, str]:
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"pinned official Claude fixture")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    home = tmp_path / "home"
    home.mkdir()
    scope = required_claude_verification_scopes()[0]
    return (
        ClaudeInstallation(
            executable=str(executable),
            cwd=str(tmp_path),
            model=scope.model,
            effort=scope.effort,
            client_home=str(home),
            account="test-account",
            executable_digest=digest,
        ),
        digest,
    )


def _evidence(installation, scope, digest):
    return fake_capability_evidence(
        scope=scope,
        client_version="2.1.263",
        executable_digest=digest,
        client_home=installation.client_home,
        account=installation.account,
        verifier_id=CLAUDE_VERIFIER_ID,
        verifier_version=CLAUDE_VERIFIER_VERSION,
    )


def _append_file(filename: str, payload: bytes) -> None:
    with open(filename, "ab") as stream:
        stream.write(payload)


def test_only_the_opus_independent_review_scope_is_eligible() -> None:
    scopes = required_claude_verification_scopes()

    assert len(scopes) == 1
    scope = scopes[0]
    assert (scope.name, scope.model, scope.effort, scope.role) == (
        "opus-independent-review",
        "claude-opus-5",
        "medium",
        SpecialistPurpose.INDEPENDENT_REVIEW,
    )
    assert frozenset(scope.tool_surface) == SPECIALIST_ALLOWED_TOOLS[scope.role]


async def test_concrete_verifier_hashes_the_executable_and_resolves_exact_evidence(
    tmp_path,
) -> None:
    installation, digest = _installation(tmp_path)
    scope = required_claude_verification_scopes()[0].evidence_scope()

    class Source:
        def __init__(self):
            self.identities = []

        async def resolve(self, identity, *, evidence_id=None):
            assert evidence_id is None
            self.identities.append(identity)
            return _evidence(installation, scope, digest)

    source = Source()
    report = await ClaudeEvidenceVerifier(source).verify(installation, scope)

    assert len(source.identities) == 1
    assert source.identities[0] == report.evidence.manifest.identity
    assert report.admits(installation, scope)


def test_verifier_version_is_bound_to_the_isolation_policy() -> None:
    assert CLAUDE_VERIFIER_VERSION == f"1-{CLAUDE_ISOLATION_POLICY_DIGEST[:16]}"


@pytest.mark.parametrize("change", ["script", "scope"])
async def test_unapproved_launch_or_scope_is_denied_before_evidence_lookup(
    tmp_path, change
) -> None:
    installation, _ = _installation(tmp_path)
    scope = required_claude_verification_scopes()[0].evidence_scope()
    if change == "script":
        installation = replace(installation, script=(*installation.script, "--help"))
    else:
        scope = CapabilityEvidenceScope(
            route=scope.route,
            role=scope.role,
            tool_surface=(ToolName.REPOSITORY_READ_FILE,),
        )

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            raise AssertionError("unapproved identities must not reach the evidence source")

    report = await ClaudeEvidenceVerifier(Source()).verify(installation, scope)

    assert not report.admits(installation, scope)


async def test_executable_drift_is_denied_before_evidence_lookup(tmp_path) -> None:
    installation, _ = _installation(tmp_path)
    scope = required_claude_verification_scopes()[0].evidence_scope()
    _append_file(installation.executable, b"changed")

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            raise AssertionError("changed executables must not reach the evidence source")

    report = await ClaudeEvidenceVerifier(Source()).verify(installation, scope)

    assert not report.admits(installation, scope)


async def test_managed_policy_drift_is_denied_before_evidence_lookup(tmp_path) -> None:
    installation, _ = _installation(tmp_path)
    scope = required_claude_verification_scopes()[0].evidence_scope()
    (Path(installation.client_home) / "managed-settings.json").write_text(
        '{"hooks":{"SessionStart":[]}}', encoding="utf-8"
    )

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            raise AssertionError("changed managed policy must not reach the evidence source")

    report = await ClaudeEvidenceVerifier(Source()).verify(installation, scope)

    assert not report.admits(installation, scope)


async def test_managed_policy_change_during_evidence_lookup_is_denied(tmp_path) -> None:
    installation, digest = _installation(tmp_path)
    scope = required_claude_verification_scopes()[0].evidence_scope()

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            (Path(installation.client_home) / "managed-settings.d").mkdir()
            return _evidence(installation, scope, digest)

    report = await ClaudeEvidenceVerifier(Source()).verify(installation, scope)

    assert not report.admits(installation, scope)


async def test_executable_change_during_evidence_lookup_is_denied(tmp_path) -> None:
    installation, digest = _installation(tmp_path)
    scope = required_claude_verification_scopes()[0].evidence_scope()

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            _append_file(installation.executable, b"changed")
            return _evidence(installation, scope, digest)

    report = await ClaudeEvidenceVerifier(Source()).verify(installation, scope)

    assert not report.admits(installation, scope)


@pytest.mark.parametrize("failure", ["account", "verifier", "version", "type"])
async def test_untrusted_source_result_never_becomes_a_capability_report(tmp_path, failure) -> None:
    installation, digest = _installation(tmp_path)
    scope = required_claude_verification_scopes()[0].evidence_scope()

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            if failure == "type":
                return object()
            if failure == "account":
                return _evidence(replace(installation, account="another-account"), scope, digest)
            if failure == "version":
                return fake_capability_evidence(
                    scope=scope,
                    client_version="2.1.263",
                    executable_digest=digest,
                    client_home=installation.client_home,
                    account=installation.account,
                    verifier_id=CLAUDE_VERIFIER_ID,
                    verifier_version="foreign-policy",
                )
            return fake_capability_evidence(
                scope=scope,
                client_version="2.1.263",
                executable_digest=digest,
                client_home=installation.client_home,
                account=installation.account,
                verifier_id="another-verifier",
                verifier_version=CLAUDE_VERIFIER_VERSION,
            )

    report = await ClaudeEvidenceVerifier(Source()).verify(installation, scope)

    assert not report.admits(installation, scope)


@pytest.mark.parametrize(
    "route",
    [
        RouteSpec(provider="another", client="claude_code", model="claude-opus-5"),
        RouteSpec(provider="anthropic", client="another", model="claude-opus-5"),
        RouteSpec(provider="anthropic", client="claude_code", model="another"),
    ],
)
async def test_route_identity_drift_is_denied_before_evidence_lookup(tmp_path, route) -> None:
    installation, _ = _installation(tmp_path)
    allowed = required_claude_verification_scopes()[0].evidence_scope()
    scope = CapabilityEvidenceScope(
        route=route,
        role=allowed.role,
        tool_surface=allowed.tool_surface,
    )

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            raise AssertionError("changed routes must not reach the evidence source")

    report = await ClaudeEvidenceVerifier(Source()).verify(installation, scope)

    assert not report.admits(installation, scope)
