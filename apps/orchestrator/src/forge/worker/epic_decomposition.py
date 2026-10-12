"""Validation boundary for decomposition output from the configured subject gateway."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from forge.application.ports.epic_brainstorm import (
    BrainstormProcessLifecycle,
    ObservableBrainstormGateway,
)
from forge.application.ports.epic_decomposition import (
    DecompositionGateway,
    DecompositionGatewayResult,
)
from forge.application.ports.task_usage import TaskUsageObserver
from forge.domain.epic_brainstorm import AuthoringJobSnapshot, BrainstormTurn
from forge.domain.epic_decomposition import (
    DecompositionProposal,
    validate_decomposition_proposal,
)
from forge.domain.task_usage_contract import WorkUnitBinding
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
        return self._validate(job, cast(DecompositionGatewayResult, result))

    async def execute_observed(
        self,
        job: AuthoringJobSnapshot,
        turns: tuple[BrainstormTurn, ...],
        reader: BrainstormReadOnlyTools,
        *,
        cancelled: Callable[[], Awaitable[bool]],
        lifecycle: BrainstormProcessLifecycle,
        usage_binding: WorkUnitBinding,
        usage_sink: TaskUsageObserver,
    ) -> DecompositionGatewayResult:
        if callable(getattr(self._gateway, "execute_observed", None)):
            result = await cast(ObservableBrainstormGateway, self._gateway).execute_observed(
                job,
                turns,
                reader,
                cancelled=cancelled,
                lifecycle=lifecycle,
                usage_binding=usage_binding,
                usage_sink=usage_sink,
            )
        else:
            result = await self._gateway.execute(
                job, turns, reader, cancelled=cancelled, lifecycle=lifecycle
            )
        return self._validate(job, cast(DecompositionGatewayResult, result))

    @staticmethod
    def _validate(job: AuthoringJobSnapshot, result: DecompositionGatewayResult) -> DecompositionGatewayResult:
        if result.failure not in {None, "interrupted"} or result.proposal is None:
            return DecompositionGatewayResult(
                proposal=None,
                telemetry=result.telemetry,
                failure=result.failure or "invalid_output",
                quota_reset_at=result.quota_reset_at,
            )
        try:
            if (
                str(job.kind) != "decomposition"
                or job.accepted_content is None
                or job.input_brief_revision_id is None
                or job.input_brief_digest is None
                or not isinstance(result.proposal, DecompositionProposal)
            ):
                raise ValueError("decomposition binding conflicts")
            proposal = validate_decomposition_proposal(result.proposal, job.accepted_content)
            if (
                proposal.epic_id != job.epic_id
                or proposal.project_id != job.project_id
                or proposal.brief_revision_id != job.input_brief_revision_id
                or proposal.brief_digest != job.input_brief_digest
            ):
                raise ValueError("decomposition binding conflicts")
        except TypeError, ValueError:
            return DecompositionGatewayResult(
                proposal=None, telemetry=result.telemetry, failure="invalid_output"
            )
        return DecompositionGatewayResult(
            proposal=proposal,
            telemetry=result.telemetry,
            failure=result.failure,
            quota_reset_at=result.quota_reset_at,
        )


__all__ = ["ValidatedDecompositionGateway"]
