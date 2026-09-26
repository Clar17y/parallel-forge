"""Shared delegation authority checks for decoding and durable application."""

from forge.domain.subscription import ExecutionEnvelope, LogicalTaskContract, SpecialistPurpose


def validate_child_authority(
    parent: LogicalTaskContract, child: LogicalTaskContract, envelope: ExecutionEnvelope
) -> None:
    if (
        parent.purpose is not SpecialistPurpose.PRIMARY
        or child.purpose is SpecialistPurpose.PRIMARY
        or child.run_id != parent.run_id
        or child.parent_task_id != parent.task_id
        or parent.task_id in child.dependency_task_ids
        or child.route != envelope.route_for(child.purpose)
        or child.budget.billing_mode is not child.route.effective.billing_mode
    ):
        raise ValueError("child authority differs from frozen primary contract")
    for name in (
        "max_duration_seconds",
        "max_tool_calls",
        "max_named_checks",
        "max_provider_attempts",
        "max_repairs",
        "max_input_tokens",
        "max_output_tokens",
        "max_cost_minor",
    ):
        bound, proposed = getattr(parent.budget, name), getattr(child.budget, name)
        if bound is not None and (proposed is None or proposed > bound):
            raise ValueError("child budget exceeds parent")
    if child.max_repairs > parent.max_repairs or child.max_repairs > child.budget.max_repairs:
        raise ValueError("child repair limit exceeds parent")
    if not set(child.named_checks) <= set(parent.named_checks):
        raise ValueError("child checks exceed parent")
    if any(
        not set(item.required_check_names) <= set(child.named_checks)
        for item in child.typed_acceptance
    ):
        raise ValueError("acceptance check is absent from child contract")
    for name in ("allow_unknown_tokens", "allow_unknown_cost", "allow_unknown_quota"):
        if not getattr(parent.budget.unknown_telemetry_policy, name) and getattr(
            child.budget.unknown_telemetry_policy, name
        ):
            raise ValueError("child telemetry policy exceeds parent")
    if (
        child.budget.unknown_telemetry_policy.max_uncertain_attempts
        > parent.budget.unknown_telemetry_policy.max_uncertain_attempts
    ):
        raise ValueError("child uncertainty limit exceeds parent")
