"""Shared strict measurement attribution for paginated groups and global scalars."""

from uuid import UUID

from forge.domain.subscription import (
    AttemptTelemetry,
    LogicalTaskContract,
    RouteBinding,
    RouteSpec,
    SpecialistPurpose,
    decode_subscription_record,
)
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption

type GroupKey = tuple[UUID, UUID, SpecialistPurpose, RouteSpec, str | None]


def source_key(
    attempt: SubscriptionAttempt,
    task: SubscriptionTask,
    consumption: SubscriptionAttemptConsumption | None,
    project_id: UUID,
) -> tuple[GroupKey, AttemptTelemetry | None]:
    try:
        contract = decode_subscription_record(task.payload)
        binding = decode_subscription_record(attempt.route_payload)
        payload = (
            consumption.telemetry_payload if consumption is not None else attempt.telemetry_payload
        )
        telemetry = decode_subscription_record(payload) if payload is not None else None
        if not isinstance(contract, LogicalTaskContract) or not isinstance(binding, RouteBinding):
            raise TypeError
        if telemetry is not None and not isinstance(telemetry, AttemptTelemetry):
            raise TypeError
        if (contract.run_id, contract.task_id) != (attempt.run_id, attempt.task_row_id):
            raise ValueError
        return (
            project_id,
            attempt.run_id,
            contract.purpose,
            binding.effective,
            telemetry.currency if telemetry else None,
        ), telemetry
    except TypeError, ValueError, KeyError:
        raise ValueError("invalid stored subscription usage") from None
