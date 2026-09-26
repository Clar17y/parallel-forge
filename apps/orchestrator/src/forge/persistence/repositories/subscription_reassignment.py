"""Reassign stopped children without changing their work, budgets or approvals."""

from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.domain.operation import canonical_digest
from forge.domain.paths import policy_path_key
from forge.domain.subscription import (
    BoundReassignDecision,
    ExecutionEnvelope,
    LogicalTaskContract,
    RouteBinding,
    RouteMapping,
    SpecialistPurpose,
    decode_subscription_record,
    encode_subscription_record,
    is_read_only,
)
from forge.domain.subscription_task_controls import TaskControlConflict
from forge.persistence.models.execution import ToolCall
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.subscription_idle_task_controls import settled_history_digest


async def reassignment_proof(
    session: AsyncSession,
    parent: LogicalTaskContract,
    envelope: ExecutionEnvelope,
    decision: BoundReassignDecision,
    response_digest: str,
) -> tuple[SubscriptionAttempt, LogicalTaskContract, dict[str, object]] | None:
    """Validate only immutable history, so replay remains valid after later work."""
    source = await session.get(SubscriptionAttempt, decision.source_attempt_id)
    if (
        source is None
        or source.run_id != parent.run_id
        or source.task_row_id != decision.task_id
        or not decision.preserve_partial_work
    ):
        return None
    result = await session.get(SubscriptionAttemptResult, source.id)
    if (
        result is None
        or not result.accepted
        or result.disposition not in {"failed", "handoff", "scope_requested", "quota_deferred"}
    ):
        return None
    try:
        history = await settled_history_digest(session, source)
        context = result.result_payload["proposal_context"]
        if not isinstance(context, dict):
            raise TypeError
        original = decode_subscription_record(context["task"])
        if (
            not isinstance(original, LogicalTaskContract)
            or original.run_id != parent.run_id
            or original.task_id != decision.task_id
            or original.parent_task_id != parent.task_id
            or original.purpose is SpecialistPurpose.PRIMARY
            or canonical_digest(context["task"]) != source.task_digest
            or context["envelope"] != encode_subscription_record(envelope)
            or canonical_digest(context["envelope"]) != source.envelope_digest
            or context["route"] != encode_subscription_record(original.route)
            or context["route"] != source.route_payload
            or context["task_version"] != source.task_version
            or context["candidate_epoch"] != source.candidate_epoch
            or not envelope.permits_route(original.purpose, original.route)
        ):
            raise ValueError
    except KeyError, TypeError, ValueError, TaskControlConflict:
        raise SubscriptionDecisionError("reassignment stopped source proof differs") from None
    preferred = envelope.route_for(original.purpose)
    route = decision.new_route
    if (
        route == original.route.effective
        or route not in (preferred.effective, *envelope.fallbacks_for(original.purpose))
        or route.auth_mode is not preferred.effective.auth_mode
        or route.billing_mode is not preferred.effective.billing_mode
    ):
        return None
    binding = (
        preferred
        if route == preferred.effective
        else replace(
            preferred,
            effective=route,
            mapping_applied=RouteMapping(
                requested=preferred.requested,
                effective=route,
                approved_by=f"profile:{envelope.profile_id}",
                approval_id=f"profile:{envelope.profile_id}:version:{envelope.profile_version}",
                reason="Primary reassignment through frozen specialist fallback",
            ),
        )
    )
    updated = replace(original, route=binding)
    receipt: dict[str, object] = {
        "schema_version": 1,
        "kind": "reassignment",
        "response_result_digest": response_digest,
        "source_attempt_id": str(source.id),
        "source_result_digest": result.result_digest,
        "source_history_digest": history,
        "child_task_id": str(original.task_id),
        "child_version": decision.expected_task_version,
        "prior_task_digest": source.task_digest,
        "updated_task": encode_subscription_record(updated),
    }
    return source, updated, receipt


async def requeue_reassigned_child(
    session: AsyncSession,
    parent_schedule: SubscriptionScheduledTask,
    decision: BoundReassignDecision,
    source: SubscriptionAttempt,
    updated: LogicalTaskContract,
    candidate_epoch: int,
) -> None:
    """The caller holds the run lock and the current primary decision authority."""
    child = await session.get(
        SubscriptionTask, decision.task_id, with_for_update=True, populate_existing=True
    )
    scheduled = await session.get(
        SubscriptionScheduledTask, decision.task_id, with_for_update=True, populate_existing=True
    )
    result = await session.get(SubscriptionAttemptResult, source.id)
    assert result is not None
    prior_route = decode_subscription_record(source.route_payload)
    assert isinstance(prior_route, RouteBinding)
    expected_state = {
        "failed": "terminal",
        "handoff": "terminal",
        "scope_requested": "blocked",
        "quota_deferred": "queued",
    }[result.disposition]
    latest = await session.scalar(
        select(SubscriptionAttempt.id)
        .where(SubscriptionAttempt.task_row_id == decision.task_id)
        .order_by(SubscriptionAttempt.attempt_number.desc())
        .limit(1)
    )
    effect = await session.scalar(
        select(SubscriptionScheduledEffect.id)
        .where(
            SubscriptionScheduledEffect.task_id == decision.task_id,
            SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
        )
        .limit(1)
    )
    tool = await session.scalar(
        select(ToolCall.id)
        .where(
            ToolCall.subscription_task_id == decision.task_id,
            ToolCall.status.in_(("PENDING", "RUNNING")),
        )
        .limit(1)
    )
    if (
        child is None
        or scheduled is None
        or child.run_id != parent_schedule.run_id
        or scheduled.run_id != parent_schedule.run_id
        or child.parent_task_id != parent_schedule.task_id
        or scheduled.parent_task_id != parent_schedule.task_id
        or child.state != expected_state
        or scheduled.state != expected_state
        or child.pause_requested
        or child.cancel_requested
        or scheduled.pause_requested
        or scheduled.cancel_requested
        or child.version != decision.expected_task_version
        or canonical_digest(child.payload) != source.task_digest
        or latest != source.id
        or source.status != "terminal"
        or scheduled.lease_owner is not None
        or scheduled.lease_expires_at is not None
        or scheduled.lease_generation != source.lease_generation
        or scheduled.worktree_id != parent_schedule.worktree_id
        or source.candidate_epoch != candidate_epoch
        or scheduled.provider != prior_route.effective.provider
        or tuple(scheduled.owned_paths) != tuple(policy_path_key(p) for p in updated.owned_paths)
        or tuple(scheduled.dependency_task_ids) != updated.dependency_task_ids
        or scheduled.read_only != is_read_only(updated.purpose)
        or scheduled.max_repairs != updated.max_repairs
        or effect is not None
        or tool is not None
    ):
        raise SubscriptionDecisionError("reassignment target is no longer current or stopped")
    child.payload = encode_subscription_record(updated)
    child.version += 1
    child.state = scheduled.state = "queued"
    scheduled.provider = updated.route.effective.provider
