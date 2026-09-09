"""Closed identities for Forge-owned agent roles."""

from enum import StrEnum


class AgentRole(StrEnum):
    """The complete set of agent roles that may receive controlled tools."""

    PLANNER = "planner"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"


__all__ = ["AgentRole"]
