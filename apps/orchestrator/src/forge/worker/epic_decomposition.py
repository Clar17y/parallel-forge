"""Validation boundary for decomposition output from the configured subject gateway."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from forge.application.ports.epic_brainstorm import BrainstormProcessLifecycle
from forge.application.ports.epic_decomposition import (
    DecompositionGateway,
    DecompositionGatewayResult,
)
from forge.domain.epic_brainstorm import AuthoringJobSnapshot, BrainstormTurn
from forge.domain.epic_decomposition import DecompositionProposal, validate_decomposition_proposal
from forge.tools.epic_brainstorm import BrainstormReadOnlyTools


class ValidatedDecompositionGateway:
    """Publish only closed, brief-bound proposals; retain gateway telemetry on failure."""

    def __init__(self, gateway: DecompositionGateway) -> None:
        self._gateway = gateway

    async def execute(
        self,
        job: AuthoringJobSnapshot,
        turns: tuple[BrainstormTurn, ...],
        reader: BrainstormReadOnlyTools,
        *,
        cancelled: Callable[[], Awaitable[bool]],
        lifecycle: BrainstormProcessLifecycle,
    ) -> DecompositionGatewayResult:
        result = await self._gateway.execute(
            job, turns, reader, cancelled=cancelled, lifecycle=lifecycle
        )
        if result.failure is not None or result.proposal is None:
            return DecompositionGatewayResult(
                proposal=None,
                telemetry=result.telemetry,
                failure=result.failure or "invalid_output",
                quota_reset_at=result.quota_reset_at,
            )
        try:
            proposal = DecompositionProposal.model_validate(
                result.proposal.model_dump(mode="json")
            )
            if (
                str(job.kind) != "decomposition"
                or job.accepted_content is None
                or job.input_brief_revision_id is None
                or job.input_brief_digest is None
                or proposal.epic_id != job.epic_id
                or proposal.project_id != job.project_id
                or proposal.brief_revision_id != job.input_brief_revision_id
                or proposal.brief_digest != job.input_brief_digest
            ):
                raise ValueError("decomposition binding conflicts")
            validate_decomposition_proposal(proposal, job.accepted_content)
        except (TypeError, ValueError):
            return DecompositionGatewayResult(
                proposal=None, telemetry=result.telemetry, failure="invalid_output"
            )
        return DecompositionGatewayResult(proposal=proposal, telemetry=result.telemetry)


__all__ = ["ValidatedDecompositionGateway"]
