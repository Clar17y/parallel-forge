from pathlib import Path

from forge.domain.actor import AgentRole
from forge.domain.policy import CommandSpec, StepKind
from forge.evaluations.contracts import EvaluationCaseContract
from forge.evaluations.materializer import (
    _remove_readonly,
    calculate_fixture_identity,
    materialize_fixture,
    validate_repository_template,
)

FIXTURES_ROOT = Path(__file__).parent / "fixtures"


def test_preserved_fixture_survives_failed_durable_teardown() -> None:
    """The service can retain the exact temporary repository for intervention."""
    case = EvaluationCaseContract(
        fixture_version="fixture-lifecycle-v1",
        case_key="developer/basic-change",
        role=AgentRole.DEVELOPER,
        task="Change the greeting.",
        base_directory=FIXTURES_ROOT / "developer" / "basic-change",
    )
    with materialize_fixture(case) as fixture:
        path = fixture.path
        fixture.preserve()

    try:
        assert path.is_dir()
        assert (path / ".git").is_dir()
    finally:
        # This test owns the retained recovery fixture.
        import shutil

        shutil.rmtree(path, onerror=_remove_readonly)


def test_fixture_identity_binds_all_command_policy_fields() -> None:
    base = FIXTURES_ROOT / "developer" / "basic-change"
    snapshots = validate_repository_template(base / "repository")
    offline = EvaluationCaseContract(
        fixture_version="fixture-lifecycle-v1",
        case_key="developer/basic-change",
        role=AgentRole.DEVELOPER,
        task="Change the greeting.",
        base_directory=base,
        required_checks=("check",),
        check_commands=(
            CommandSpec(
                kind=StepKind.TEST,
                name="check",
                argv=("python", "check.py"),
                timeout_seconds=60,
                required=False,
                network_enabled=False,
            ),
        ),
    )
    online = offline.model_copy(
        update={
            "check_commands": (
                offline.check_commands[0].model_copy(update={"network_enabled": True}),
            )
        }
    )
    assert calculate_fixture_identity(offline, snapshots) != calculate_fixture_identity(
        online, snapshots
    )
