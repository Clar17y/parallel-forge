"""Exact named-command resolution shared by every runner adapter."""

from __future__ import annotations

from forge.domain.policy import CommandSpec, ProjectPolicy, StepKind
from forge.domain.validation import UnknownNamedCommand


class NamedCommandResolver:
    """Resolve one exact policy name without parsing command text."""

    def __init__(self, policy: ProjectPolicy) -> None:
        if not isinstance(policy, ProjectPolicy):
            raise TypeError("named commands require a project policy")
        self._commands = {command.name: command for command in policy.commands}

    def resolve(self, name: str, *, kind: StepKind) -> CommandSpec:
        """Return only an exact name and expected-kind match."""

        if type(name) is not str or not isinstance(kind, StepKind):
            raise UnknownNamedCommand()
        command = self._commands.get(name)
        if command is None or command.kind is not kind:
            raise UnknownNamedCommand()
        return command


__all__ = ["NamedCommandResolver", "UnknownNamedCommand"]
