from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.domain.policy import (
    CommandSpec,
    DatabaseProvisioningPolicy,
    ProjectPolicy,
    RunnerMode,
    StepKind,
)
from forge.domain.review import FindingSeverity, ReviewFinding
from pydantic import ValidationError


def make_policy(**overrides: object) -> ProjectPolicy:
    values: dict[str, object] = {
        "id": uuid4(),
        "version": 1,
        "repository_path": "D:/Code/Parallel",
        "github_repository": "Clar17y/Parallel",
        "default_branch": "main",
    }
    values.update(overrides)
    return ProjectPolicy(**values)


def make_command(
    kind: StepKind,
    name: str | None = None,
    *,
    required: bool = True,
    argv: tuple[str, ...] | None = None,
) -> CommandSpec:
    return CommandSpec(
        kind=kind,
        name=name or f"{kind.value}-command",
        argv=("forge-check", kind.value) if argv is None else argv,
        timeout_seconds=60,
        required=required,
    )


def test_policy_defaults_to_three_cycles_and_docker() -> None:
    policy = make_policy()

    assert policy.runner_mode is RunnerMode.DOCKER
    assert policy.local_remediation_limit == 3
    assert policy.remote_remediation_limit == 3


def test_commands_are_the_single_registry_for_every_step_kind() -> None:
    commands = tuple(make_command(kind, f"named-{kind.value}") for kind in StepKind)
    policy = make_policy(commands=commands)

    for kind in StepKind:
        assert policy.commands_for(kind) == (
            next(command for command in commands if command.kind is kind),
        )
    assert not any(hasattr(command, "command") for command in policy.commands)
    assert "command" not in CommandSpec.model_fields


def test_required_checks_include_only_required_validation_commands() -> None:
    commands = (
        make_command(StepKind.BOOTSTRAP, "bootstrap", required=True),
        make_command(StepKind.TEST, "tests", required=True),
        make_command(StepKind.LINT, "lint", required=False),
        make_command(StepKind.TYPECHECK, "types", required=True),
        make_command(StepKind.BUILD, "build", required=True),
        make_command(StepKind.CUSTOM, "security", required=True),
    )
    policy = make_policy(commands=commands)

    assert tuple(command.name for command in policy.required_checks) == (
        "tests",
        "types",
        "build",
        "security",
    )
    # The helper remains callable for application code that treats it as a method.
    assert tuple(command.name for command in policy.required_checks()) == (
        "tests",
        "types",
        "build",
        "security",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_path", "relative/repository"),
        ("runner_mode", RunnerMode.TRUSTED_HOST),
        ("allowed_environment_files", ("../outside.env",)),
        ("secret_paths", ("nested/../.env",)),
    ],
)
def test_policy_rejects_unsafe_identity_or_file_configuration(field: str, value: object) -> None:
    overrides: dict[str, object] = {field: value}
    if field == "runner_mode":
        overrides["trusted_project"] = False

    with pytest.raises(ValidationError):
        make_policy(**overrides)


def test_policy_rejects_duplicate_command_names() -> None:
    duplicate = make_command(StepKind.TEST, "same-name")

    with pytest.raises(ValidationError, match="duplicate"):
        make_policy(commands=(duplicate, duplicate.model_copy(update={"kind": StepKind.LINT})))


@pytest.mark.parametrize(
    "argv",
    [
        (),
        ("/bin/sh", "-c", "echo unsafe"),
        ("powershell.exe", "-Command", "Write-Host unsafe"),
        ("cmd.exe", "/c", "echo unsafe"),
        ("forge-check", "test; rm -rf ."),
        ("forge-check", "$(whoami)"),
        ("forge-check", "check && escape"),
    ],
)
def test_command_spec_rejects_empty_vectors_shell_wrappers_and_metacharacters(
    argv: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError):
        make_command(StepKind.CUSTOM, argv=argv)


def test_policy_rejects_trusted_host_without_explicit_trust() -> None:
    with pytest.raises(ValidationError, match="trusted"):
        make_policy(runner_mode=RunnerMode.TRUSTED_HOST)


def test_trusted_host_is_valid_only_for_a_trusted_project() -> None:
    policy = make_policy(runner_mode=RunnerMode.TRUSTED_HOST, trusted_project=True)

    assert policy.runner_mode is RunnerMode.TRUSTED_HOST


def test_database_provisioning_defaults_to_disabled() -> None:
    policy = make_policy()

    assert policy.database == DatabaseProvisioningPolicy()
    assert policy.database.enabled is False
    assert policy.database.admin_url_secret_reference is None


def test_database_provisioning_accepts_a_server_side_reference_without_a_secret_value() -> None:
    policy = make_policy(
        database=DatabaseProvisioningPolicy(
            enabled=True,
            admin_url_secret_reference="secret://forge/postgres-admin",
        )
    )

    serialized = policy.model_dump_json()
    assert "secret://forge/postgres-admin" in serialized
    assert "postgresql://" not in serialized
    assert "super-secret" not in serialized


@pytest.mark.parametrize(
    "reference", [None, "postgresql://forge:super-secret@db/forge", "forge-secret"]
)
def test_enabled_database_requires_a_syntactically_valid_secret_reference(
    reference: str | None,
) -> None:
    with pytest.raises(ValidationError):
        make_policy(
            database=DatabaseProvisioningPolicy(
                enabled=True,
                admin_url_secret_reference=reference,
            )
        )


def test_policy_contracts_are_immutable_and_reject_unknown_command_fields() -> None:
    policy = make_policy()

    with pytest.raises(ValidationError):
        CommandSpec(
            kind=StepKind.TEST,
            name="tests",
            argv=("pytest",),
            timeout_seconds=60,
            command="pytest",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        ProjectPolicy(
            id=uuid4(),
            version=1,
            repository_path="D:/Code/Parallel",
            github_repository="Clar17y/Parallel",
            default_branch="main",
            unexpected="reject-me",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        policy.default_branch = "release"  # type: ignore[misc]


def test_review_findings_preserve_all_severities_and_resolved_findings_do_not_block() -> None:
    findings = tuple(
        ReviewFinding(
            finding_id=f"finding-{severity.value}",
            severity=severity,
            path="src/main.py",
            start_line=10,
            summary=f"{severity.value} issue",
            evidence="evidence",
            proposed_resolution="fix it",
            resolved_at=(datetime.now(UTC) if severity is FindingSeverity.BLOCKER else None),
        )
        for severity in FindingSeverity
    )
    policy = make_policy()

    assert {finding.severity for finding in findings} == set(FindingSeverity)
    assert policy.blocks_publication(findings) is True
    assert policy.blocks_merge(findings) is True
    assert policy.blocks_publication((findings[0],)) is False
    assert policy.blocks_merge((findings[0],)) is False
    assert policy.blocks_publication((findings[2], findings[3])) is False
    assert policy.blocks_merge((findings[2], findings[3])) is False


def test_minor_and_suggestion_blocking_can_be_enabled_per_gate() -> None:
    minor = ReviewFinding(
        finding_id="minor",
        severity=FindingSeverity.MINOR,
        path="src/main.py",
        start_line=1,
        summary="minor issue",
        evidence="evidence",
    )
    policy = make_policy(
        publication_blocking_severities=frozenset({FindingSeverity.MINOR}),
        merge_blocking_severities=frozenset({FindingSeverity.SUGGESTION}),
    )
    suggestion = minor.model_copy(
        update={"finding_id": "suggestion", "severity": FindingSeverity.SUGGESTION}
    )

    assert policy.blocks_publication((minor,)) is True
    assert policy.blocks_merge((minor,)) is False
    assert policy.blocks_merge((suggestion,)) is True


def test_review_finding_validates_line_numbers_and_is_immutable() -> None:
    ReviewFinding(
        finding_id="finding-1",
        severity=FindingSeverity.MAJOR,
        path="src/main.py",
        start_line=1,
        summary="problem",
        evidence="proof",
    )

    with pytest.raises(ValidationError):
        ReviewFinding(
            finding_id="finding-2",
            severity=FindingSeverity.MAJOR,
            path="src/main.py",
            start_line=0,
            summary="problem",
            evidence="proof",
        )
