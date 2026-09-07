"""Stable, context-free errors exposed by the Forge agent gateway."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from forge.domain.agent import validate_usage_durable_metadata, validated_usage_attempts
from forge.observability.usage import UsageRecord


class AgentGatewayError(RuntimeError):
    """A gateway boundary failure without request, provider, or output details."""

    _MESSAGE = "agent gateway execution failed"

    def __init__(self, _detail: object = None) -> None:
        del _detail
        super().__init__(self._MESSAGE)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._MESSAGE!r})"


class _UsageCarrierError(AgentGatewayError):
    """Base for safe gateway failures that retain validated metering evidence."""

    def __init__(
        self,
        _detail: object = None,
        *,
        usage: UsageRecord | None = None,
        usage_attempts: Sequence[UsageRecord] = (),
    ) -> None:
        if usage is not None and type(usage) is not UsageRecord:
            raise TypeError("usage must be a UsageRecord or None")
        if usage is None and usage_attempts:
            raise ValueError("usage attempts require aggregate usage")
        super().__init__(_detail)
        if usage is not None:
            validate_usage_durable_metadata(usage)
        self.usage = replace(usage) if usage is not None else None
        self.usage_attempts = (
            validated_usage_attempts(self.usage, usage_attempts) if self.usage is not None else ()
        )


class AgentOutputInvalid(_UsageCarrierError):
    """The provider produced invalid output on both allowed attempts."""

    _MESSAGE = "agent output is invalid"


class AgentBudgetExceeded(_UsageCarrierError):
    """The gateway cannot safely represent, price, or enforce the request budget."""

    _MESSAGE = "agent budget cannot be safely enforced"


class AgentRepairFailure(_UsageCarrierError):
    """A repair failure after a measured first provider response."""

    _MESSAGE = "agent repair could not be safely completed"


class AgentPromptDrift(AgentRepairFailure):
    """The frozen prompt changed after the first measured response."""

    _MESSAGE = "agent prompt changed during repair"


__all__ = [
    "AgentBudgetExceeded",
    "AgentGatewayError",
    "AgentOutputInvalid",
    "AgentPromptDrift",
    "AgentRepairFailure",
]
