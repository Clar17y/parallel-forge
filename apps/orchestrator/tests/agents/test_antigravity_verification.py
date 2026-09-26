"""Exact scoped evidence resolver; synthetic sources never authorize a real client."""

import asyncio
from dataclasses import replace
from hashlib import sha256

import pytest
from capability_support import fake_capability_evidence
from forge.agents.antigravity_capability_probe import AntigravityProbeInstallation
from forge.agents.antigravity_verification import (
    ANTIGRAVITY_VERIFIER_ID,
    ANTIGRAVITY_VERIFIER_VERSION,
    AntigravityEvidenceVerifier,
    antigravity_writer_scope,
)
from forge.domain.subscription import SpecialistPurpose
from forge.domain.subscription_readiness import ReadinessWarning
from forge.domain.tool import ToolName


def installation(tmp_path):
    executable = tmp_path / "agy.exe"
    executable.write_bytes(b"fake, not an installed provider client")
    return AntigravityProbeInstallation(
        executable=str(executable),
        executable_digest=sha256(executable.read_bytes()).hexdigest(),
        client_version="1.2.7",
        home=str(tmp_path),
        model="gemini-3.8-flash-medium",
        effort="medium",
        account="b" * 64,
    )


class Source:
    def __init__(
        self, installed, *, verifier=ANTIGRAVITY_VERIFIER_ID, version=ANTIGRAVITY_VERIFIER_VERSION
    ):
        self.installed, self.verifier, self.version = installed, verifier, version
        self.identities = []

    async def resolve(self, identity):
        self.identities.append(identity)
        return fake_capability_evidence(
            scope=antigravity_writer_scope(),
            client_version=self.installed.client_version,
            executable_digest=self.installed.executable_digest,
            client_home=self.installed.home,
            account=self.installed.account,
            verifier_id=self.verifier,
            verifier_version=self.version,
        )


async def test_exact_verified_scope_keeps_degraded_tool_warning_when_ready(tmp_path):
    installed = installation(tmp_path)
    source = Source(installed)
    scope = antigravity_writer_scope()
    report = await AntigravityEvidenceVerifier(source).verify(installed, scope)
    assert report.admits(installed, scope)
    assert report.warnings == (ReadinessWarning.APPROVED_TOOLS_UNPROVED,)
    assert source.identities == [report.evidence.manifest.identity]


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "gemini-3.8-flash"),
        ("effort", "high"),
        ("account", "quota-label"),
        ("executable_digest", "a" * 64),
    ],
)
async def test_unverified_installation_does_not_even_query_evidence(tmp_path, field, value):
    installed = replace(installation(tmp_path), **{field: value})
    source = Source(installed)
    report = await AntigravityEvidenceVerifier(source).verify(installed, antigravity_writer_scope())
    assert not report.admits(installed, antigravity_writer_scope())
    assert not source.identities


@pytest.mark.parametrize(
    "verifier,version",
    [
        ("forge-codex-official", ANTIGRAVITY_VERIFIER_VERSION),
        (ANTIGRAVITY_VERIFIER_ID, "old"),
    ],
)
async def test_wrong_verifier_or_policy_version_cannot_admit(tmp_path, verifier, version):
    installed = installation(tmp_path)
    report = await AntigravityEvidenceVerifier(
        Source(installed, verifier=verifier, version=version)
    ).verify(
        installed,
        antigravity_writer_scope(),
    )
    assert not report.admits(installed, antigravity_writer_scope())


@pytest.mark.parametrize("change", ["role", "tools", "client"])
async def test_scope_cannot_expand_or_use_retired_acp_client(tmp_path, change):
    scope = antigravity_writer_scope()
    if change == "role":
        scope = replace(scope, role=SpecialistPurpose.COMPLEX_IMPLEMENTATION)
    elif change == "tools":
        scope = replace(scope, tool_surface=(*scope.tool_surface, ToolName.REPOSITORY_DELETE_FILE))
    else:
        scope = replace(scope, route=replace(scope.route, client="gemini_cli"))
    installed = installation(tmp_path)
    source = Source(installed)
    report = await AntigravityEvidenceVerifier(source).verify(installed, scope)
    assert not report.admits(installed, scope)
    assert not source.identities


async def test_executable_change_during_evidence_lookup_fails_closed(tmp_path):
    installed = installation(tmp_path)

    class ChangingSource(Source):
        async def resolve(self, identity):
            result = await super().resolve(identity)
            from pathlib import Path

            Path(installed.executable).write_bytes(b"replaced")
            return result

    report = await AntigravityEvidenceVerifier(ChangingSource(installed)).verify(
        installed,
        antigravity_writer_scope(),
    )
    assert not report.admits(installed, antigravity_writer_scope())


async def test_source_failure_denies_and_cancellation_propagates(tmp_path):
    installed = installation(tmp_path)

    class BrokenSource:
        async def resolve(self, identity):
            raise RuntimeError("sensitive source detail")

    report = await AntigravityEvidenceVerifier(BrokenSource()).verify(
        installed, antigravity_writer_scope()
    )
    assert not report.admits(installed, antigravity_writer_scope())
    assert "sensitive" not in repr(report)

    class CancelledSource:
        async def resolve(self, identity):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await AntigravityEvidenceVerifier(CancelledSource()).verify(
            installed, antigravity_writer_scope()
        )


@pytest.mark.parametrize("field", ["client_version", "account", "home"])
async def test_evidence_for_another_installation_binding_is_denied(tmp_path, field):
    installed = installation(tmp_path)
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    changed = replace(
        installed,
        **{
            field: {
                "client_version": "1.2.8",
                "account": "c" * 64,
                "home": str(other_home),
            }[field]
        },
    )
    report = await AntigravityEvidenceVerifier(Source(installed)).verify(
        changed, antigravity_writer_scope()
    )
    assert not report.admits(changed, antigravity_writer_scope())
