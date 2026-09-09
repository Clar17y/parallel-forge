import pytest
from forge.domain.actor import AgentRole
from forge.domain.policy import CommandSpec, StepKind
from forge.evaluations.contracts import EvaluationCaseContract


def test_programmatic_check_command_cannot_embed_a_credential() -> None:
    command = CommandSpec(
        kind=StepKind.TEST,
        name="check",
        argv=("python", "check.py", "ghp_" + "A" * 36),
        timeout_seconds=10,
    )
    with pytest.raises(ValueError, match="Credential detected"):
        EvaluationCaseContract(
            fixture_version="v1",
            case_key="developer/example",
            task="Check",
            role=AgentRole.DEVELOPER,
            required_checks=("check",),
            check_commands=(command,),
        )
