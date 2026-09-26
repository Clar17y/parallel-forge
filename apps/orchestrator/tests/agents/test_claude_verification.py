"""Evidence-backed verification for the pinned official Claude client."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from capability_support import fake_capability_evidence
from forge.agents.claude_gateway import (
    CLAUDE_ISOLATION_POLICY_DIGEST,
    ClaudeCapabilityReport,
    ClaudeInstallation,
)
from forge.agents.claude_verification import (
    CLAUDE_VERIFIER_ID,
    CLAUDE_VERIFIER_VERSION,
    ClaudeEvidenceVerifier,
    required_claude_verification_scopes,
)
from forge.domain.capability_evidence import CapabilityEvidenceScope
from forge.domain.subscription import SPECIALIST_ALLOWED_TOOLS, RouteSpec, SpecialistPurpose
from forge.domain.tool import ToolName


@pytest.fixture(autouse=True)
def _supported_isolation_platform(monkeypatch):
    monkeypatch.setattr(
        "forge.agents.claude_gateway.claude_isolation_platform_supported", lambda: True
    )


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


async def test_unsupported_platform_does_not_resolve_evidence(tmp_path, monkeypatch) -> None:
    installation, _ = _installation(tmp_path)
    scope = required_claude_verification_scopes()[0].evidence_scope()
    monkeypatch.setattr(
        "forge.agents.claude_gateway.claude_isolation_platform_supported", lambda: False
    )

    class Source:
        async def resolve(self, *_args, **_kwargs):
            raise AssertionError("unsupported platform must not resolve evidence")

    report = await ClaudeEvidenceVerifier(Source()).verify(installation, scope)

    assert report == ClaudeCapabilityReport()


def test_verifier_version_is_bound_to_the_isolation_policy() -> None:
    assert CLAUDE_VERIFIER_VERSION == f"1-{CLAUDE_ISOLATION_POLICY_DIGEST[:16]}"


def test_claude_isolation_policy_digest_binds_exact_launch_environment() -> None:
    from forge.agents import claude_gateway

    assert claude_gateway.CLAUDE_ISOLATION_LAUNCH_ENVIRONMENT == {
        "CLAUDE_CODE_ENTRYPOINT": "local-agent",
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1",
    }
    payload = claude_gateway._isolation_policy_digest_payload()
    assert payload["launch_environment"] == [
        ("CLAUDE_CODE_ENTRYPOINT", "local-agent"),
        ("CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "1"),
    ]
    expected_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert claude_gateway.CLAUDE_ISOLATION_POLICY_DIGEST == expected_digest

    forbidden_credential_keys = {
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CCR_OAUTH_TOKEN_FILE",
        "CLAUDE_CODE_USE_GATEWAY",
        "CLAUDE_CODE_MANAGED_SETTINGS_PATH",
    }
    assert not (set(claude_gateway.CLAUDE_ISOLATION_LAUNCH_ENVIRONMENT) & forbidden_credential_keys)
    production_keys = {"CLAUDE_CONFIG_DIR", *claude_gateway.CLAUDE_ISOLATION_LAUNCH_ENVIRONMENT}
    assert not (production_keys & forbidden_credential_keys)


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


async def test_managed_policy_drift_does_not_gate_evidence_for_the_pinned_launch_root(
    tmp_path,
) -> None:
    installation, digest = _installation(tmp_path)
    scope = required_claude_verification_scopes()[0].evidence_scope()
    (Path(installation.client_home) / "managed-settings.json").write_text(
        '{"hooks":{"SessionStart":[]}}', encoding="utf-8"
    )

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            return _evidence(installation, scope, digest)

    report = await ClaudeEvidenceVerifier(Source()).verify(installation, scope)

    assert report.admits(installation, scope)


async def test_managed_policy_change_during_evidence_lookup_does_not_invalidate_evidence(
    tmp_path,
) -> None:
    installation, digest = _installation(tmp_path)
    scope = required_claude_verification_scopes()[0].evidence_scope()

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            (Path(installation.client_home) / "managed-settings.d").mkdir()
            return _evidence(installation, scope, digest)

    report = await ClaudeEvidenceVerifier(Source()).verify(installation, scope)

    assert report.admits(installation, scope)


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


async def test_verify_offloads_platform_probe_from_event_loop(tmp_path, monkeypatch) -> None:
    import threading

    probe_threads: list[threading.Thread] = []

    def _probe() -> bool:
        probe_threads.append(threading.current_thread())
        return False

    monkeypatch.setattr("forge.agents.claude_gateway.claude_isolation_platform_supported", _probe)
    installation, _ = _installation(tmp_path)
    scope = required_claude_verification_scopes()[0].evidence_scope()

    class Source:
        async def resolve(self, identity, *, evidence_id=None):
            raise AssertionError("unsupported platform must not reach evidence lookup")

    report = await ClaudeEvidenceVerifier(Source()).verify(installation, scope)
    assert report == ClaudeCapabilityReport()
    assert len(probe_threads) == 1
    assert probe_threads[0] is not threading.main_thread()


def test_claude_isolation_policy_digest_derives_from_canonical_initialization_request() -> None:
    from forge.agents import claude_gateway

    payload = claude_gateway._isolation_policy_digest_payload()
    canonical_init = claude_gateway.claude_initialize_request()
    assert payload["initialize_controls"] == canonical_init


def test_claude_isolation_policy_digest_binds_canonical_fixed_launch_arguments() -> None:
    from forge.agents import claude_gateway

    payload = claude_gateway._isolation_policy_digest_payload()
    assert "fixed_launch_arguments" in payload
    fixed_args = payload["fixed_launch_arguments"]
    assert "--restricted" in fixed_args
    assert "--safe-mode" in fixed_args
    assert "--disable-slash-commands" in fixed_args
    assert "--no-chrome" in fixed_args
    assert "--strict-mcp-config" in fixed_args
    assert "--no-session-persistence" in fixed_args
    assert "--permission-prompts" in fixed_args


def test_claude_launch_arguments_correspond_to_digest_fixed_controls(tmp_path) -> None:
    from forge.agents import claude_gateway

    installation, _ = _installation(tmp_path)
    actual = list(
        claude_gateway.claude_launch_arguments(
            installation,
            session_id="actual-session",
            system_prompt="actual-system-prompt",
            permitted_tools=frozenset({ToolName.REPOSITORY_READ_FILE}),
            schema={"type": "object"},
        )
    )
    for flag, placeholder in (
        ("--model", "<model>"),
        ("--effort", "<effort>"),
        ("--session-id", "<session_id>"),
        ("--system-prompt", "<system_prompt>"),
        ("--json-schema", "<schema>"),
    ):
        actual[actual.index(flag) + 1] = placeholder
    actual[-1] = "--allowed-tools=<allowed_tools>"

    assert tuple(actual) == claude_gateway.claude_canonical_fixed_launch_arguments()
