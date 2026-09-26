"""Validate durable client rows against exact supervisor terminal evidence."""

from collections.abc import Sequence

from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.persistence.models.subscription import SubscriptionClientLaunch


def launches_confirmed(
    rows: Sequence[SubscriptionClientLaunch],
    expected: SubscriptionLaunchTerminalProof | None,
    *,
    require_decision: bool,
    worker_identity: str | None,
) -> bool:
    if not worker_identity:
        return False
    if require_decision and (expected is None or not expected.permits_decision):
        return False
    matched = expected is None
    for row in rows:
        try:
            proof = SubscriptionLaunchTerminalProof.model_validate(row.terminal_payload)
        except ValueError, TypeError:
            return False
        if (
            row.state != "terminal"
            or row.worker_identity != worker_identity
            or not proof.stop_confirmed
            or (row.launch_id, row.pid, row.process_start_token)
            != (proof.launch_id, proof.pid, proof.process_identity)
        ):
            return False
        if expected is not None and row.launch_id == expected.launch_id:
            if proof != expected:
                return False
            matched = True
    return matched
