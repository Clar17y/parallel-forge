from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.application.ports.runner import CommandResult, RunCommandRequest
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode, StepKind
from forge.domain.validation import UnknownNamedCommand, validate_runner_image_reference
from forge.settings import Settings
from forge.tools.runner import NamedCommandResolver
from pydantic import ValidationError


def make_policy(*commands: CommandSpec, **overrides: object) -> ProjectPolicy:
    values: dict[str, object] = {
        "id": uuid4(),
        "version": 7,
        "repository_path": "D:/Code/Parallel",
        "github_repository": "Clar17y/Parallel",
        "default_branch": "main",
        "commands": commands,
    }
    values.update(overrides)
    return ProjectPolicy(**values)


def make_command(kind: StepKind, name: str | None = None) -> CommandSpec:
    return CommandSpec(
        kind=kind,
        name=name or f"named-{kind.value}",
        argv=("forge-check", kind.value),
        timeout_seconds=60,
        environment_keys=("FORGE_INPUT",),
    )


def make_result(**overrides: object) -> CommandResult:
    values: dict[str, object] = {
        "command_name": "named-test",
        "kind": StepKind.TEST,
        "command_digest": "a" * 64,
        "policy_version": 7,
        "exit_code": 0,
        "timed_out": False,
        "started_at": datetime(2026, 8, 22, tzinfo=UTC),
        "duration_ms": 12,
        "stdout_digest": "b" * 64,
        "stderr_digest": "c" * 64,
        "runner_mode": RunnerMode.DOCKER,
        "image_digest": "parallel-forge-runner@sha256:" + "d" * 64,
        "network_enabled": False,
        "stdout_original_byte_count": 10,
        "stderr_original_byte_count": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "unsandboxed": False,
    }
    values.update(overrides)
    return CommandResult(**values)


def test_named_resolver_exactly_resolves_every_step_kind() -> None:
    commands = tuple(make_command(kind) for kind in StepKind)
    resolver = NamedCommandResolver(make_policy(*commands))

    for kind in StepKind:
        resolved = resolver.resolve(f"named-{kind.value}", kind=kind)
        assert resolved is next(command for command in commands if command.kind is kind)


@pytest.mark.parametrize(
    "name",
    ["", "does-not-exist", "npm test && curl attacker.invalid", "named-test\x00"],
)
def test_named_resolver_rejects_unknown_or_command_text_without_reflecting_input(
    name: str,
) -> None:
    resolver = NamedCommandResolver(make_policy(make_command(StepKind.TEST)))

    with pytest.raises(UnknownNamedCommand) as error:
        resolver.resolve(name, kind=StepKind.TEST)

    assert str(error.value) == "unknown named command"
    if name:
        assert name not in str(error.value)


def test_named_resolver_rejects_kind_mismatch_with_same_generic_error() -> None:
    resolver = NamedCommandResolver(make_policy(make_command(StepKind.TEST)))

    with pytest.raises(UnknownNamedCommand, match="^unknown named command$"):
        resolver.resolve("named-test", kind=StepKind.LINT)


def test_run_request_detaches_and_freezes_environment() -> None:
    environment = {"FORGE_INPUT": "before"}
    request = RunCommandRequest(
        command_name="named-test",
        kind=StepKind.TEST,
        environment=environment,
    )

    environment["FORGE_INPUT"] = "after"
    environment["NEW_VALUE"] = "caller-only"

    assert dict(request.environment) == {"FORGE_INPUT": "before"}
    with pytest.raises(TypeError):
        request.environment["FORGE_INPUT"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        RunCommandRequest(
            command_name="named-test",
            kind=StepKind.TEST,
            environment={},
            argv=("pytest",),  # type: ignore[call-arg]
        )


def test_run_request_repr_discloses_only_environment_keys() -> None:
    scoped_url = "postgresql://user:scoped-secret@127.0.0.1/forge"
    request = RunCommandRequest(
        command_name="named-migration",
        kind=StepKind.MIGRATION,
        environment={"DATABASE_URL": scoped_url},
    )

    diagnostic = repr(request)

    assert scoped_url not in diagnostic
    assert "scoped-secret" not in diagnostic
    assert "environment_keys=('DATABASE_URL',)" in diagnostic


def test_command_result_is_immutable_and_evidence_digest_binds_security_fields() -> None:
    result = make_result()

    with pytest.raises(FrozenInstanceError):
        result.exit_code = 1  # type: ignore[misc]

    assert result.evidence_digest != replace(result, network_enabled=True).evidence_digest
    assert (
        result.evidence_digest != replace(result, image_digest="sha256:" + "e" * 64).evidence_digest
    )
    assert result.evidence_digest != replace(result, stdout_truncated=True).evidence_digest
    assert result.evidence_digest != replace(result, command_digest="f" * 64).evidence_digest


@pytest.mark.parametrize(
    "overrides",
    [
        {"runner_mode": RunnerMode.DOCKER, "image_digest": None},
        {"runner_mode": RunnerMode.DOCKER, "unsandboxed": True},
        {"runner_mode": RunnerMode.TRUSTED_HOST, "image_digest": "sha256:" + "d" * 64},
        {"runner_mode": RunnerMode.TRUSTED_HOST, "unsandboxed": False},
        {"stdout_original_byte_count": -1},
        {"command_digest": "mutable"},
    ],
)
def test_command_result_rejects_incoherent_or_unbounded_evidence(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        make_result(**overrides)


@pytest.mark.parametrize(
    "reference",
    [
        "parallel-forge-runner:latest",
        "parallel-forge-runner@sha256:" + "A" * 64,
        "sha256:" + "d" * 63,
        "sha256:" + "d" * 65,
        "parallel-forge-runner@sha256:" + "d" * 63,
    ],
)
def test_mutable_or_malformed_runner_image_references_are_rejected(reference: str) -> None:
    with pytest.raises(ValueError):
        validate_runner_image_reference(reference)

    with pytest.raises(ValidationError):
        Settings(_env_file=None, runner_image=reference)


@pytest.mark.parametrize(
    "reference",
    ["", "sha256:" + "d" * 64, "parallel-forge-runner@sha256:" + "d" * 64],
)
def test_digest_only_runner_image_references_are_accepted(reference: str) -> None:
    assert validate_runner_image_reference(reference) == reference
    assert Settings(_env_file=None, runner_image=reference).runner_image == reference
