"""Durable, owner-enabled progression of one frozen epic execution at a time."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import or_, select

from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_eligibility import EpicEligibilityService
from forge.application.services.epic_run_bridge import (
    EpicRunBridgeService,
    latest_required_child_failed,
)
from forge.domain.epic_run_bridge import EpicLaunchConflict, LaunchRequest
from forge.persistence.models.epic_dispatch import EpicDispatchSetting
from forge.persistence.models.epic_run_bridge import (
    EpicChildBudgetHold,
    EpicExecution,
    EpicExecutionControl,
)
from forge.persistence.unit_of_work import PostgresUnitOfWork

_CLAIM_LIFETIME = timedelta(minutes=2)
_RECHECK_INTERVAL = timedelta(seconds=1)


class EpicDispatchWorker:
    def __init__(
        self,
        unit_of_work_factory: Callable[[], PostgresUnitOfWork],
        *,
        bridge: EpicRunBridgeService,
        eligibility: EpicEligibilityService,
    ) -> None:
        self._work = unit_of_work_factory
        self._bridge = bridge
        self._eligibility = eligibility

    async def run_once(self) -> UUID | None:
        now = datetime.now(UTC)
        async with self._work() as work:
            candidate = await work.session.scalar(
                select(EpicDispatchSetting)
                .where(
                    EpicDispatchSetting.enabled.is_(True),
                    or_(
                        EpicDispatchSetting.checked_at.is_(None),
                        EpicDispatchSetting.checked_at < now - _RECHECK_INTERVAL,
                    ),
                )
                .order_by(EpicDispatchSetting.checked_at.asc().nullsfirst(), EpicDispatchSetting.execution_id)
                .limit(1)
            )
            if candidate is None:
                await work.commit()
                return None
            epic_id, execution_id = candidate.epic_id, candidate.execution_id
            await work.commit()

        try:
            readiness = await self._eligibility.readiness(
                epic_id=epic_id, execution_id=execution_id
            )
        except Exception:  # noqa: BLE001 - untrusted proof failure must not monopolize polling
            async with self._work() as work:
                await work.epics.get(epic_id, for_update=True)
                setting = await work.session.get(EpicDispatchSetting, execution_id, with_for_update=True)
                if setting is not None and setting.enabled:
                    setting.checked_at = datetime.now(UTC)
                    setting.blocker_code = "readiness_unavailable"
                await work.commit()
            return execution_id
        required = [item for item in readiness if item.disposition == "required"]
        ready = next((item for item in required if item.status == "ready"), None)
        now = datetime.now(UTC)
        async with self._work() as work:
            epic = await work.epics.get(epic_id, for_update=True)
            setting = await work.session.get(EpicDispatchSetting, execution_id, with_for_update=True)
            control = await work.session.get(EpicExecutionControl, execution_id, with_for_update=True)
            if setting is None or not setting.enabled:
                await work.commit()
                return execution_id
            setting.checked_at = now
            if control is None or control.state != "ACTIVE":
                setting.blocker_code = "execution_not_active"
                if control is not None and control.state in {"SUCCEEDED", "CANCELLED"}:
                    setting.enabled = False
                await work.commit()
                return execution_id
            attempts = await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution_id)
            if await latest_required_child_failed(attempts, execution_id, work.runs.get):
                control.state = "BLOCKED"
                control.version += 1
                control.blocker_code = "predecessor_failed"
                setting.enabled = False
                setting.blocker_code = "predecessor_failed"
                setting.claim_item_id = setting.claim_token = setting.claim_expires_at = None
                await work.commit()
                return execution_id
            if setting.claim_item_id is not None:
                if setting.claim_expires_at is not None and setting.claim_expires_at > now:
                    await work.commit()
                    return execution_id
                # A crash may have occurred after the bridge committed. Its durable
                # attempt wins; never create a second child for that item.
                if any(attempt.item_id == setting.claim_item_id for attempt in attempts):
                    setting.claim_item_id = setting.claim_token = setting.claim_expires_at = None
                    setting.blocker_code = None
                    await work.commit()
                    return execution_id
                setting.claim_item_id = setting.claim_token = setting.claim_expires_at = None
            if not required or all(item.status == "verified" for item in required):
                settled = True
                for attempt in attempts:
                    run = await work.runs.get(attempt.run_id)
                    hold = await work.session.get(EpicChildBudgetHold, attempt.attempt_id)
                    if (
                        run.state not in {"COMPLETED", "FAILED", "CANCELLED"}
                        or not (await work.runs.prove_quiescent(run.id)).is_quiescent
                        or hold is None or not hold.effects_settled
                    ):
                        settled = False
                if settled:
                    control.state = "SUCCEEDED"
                    control.version += 1
                    control.blocker_code = None
                    setting.blocker_code = None
                    setting.enabled = False
                else:
                    setting.blocker_code = "child_effects_unsettled"
                await work.commit()
                return execution_id
            if ready is None:
                setting.blocker_code = next((item.blocker_code for item in required if item.status == "blocked"), "child_active")
                await work.commit()
                return execution_id
            if any(attempt.item_id == ready.item_id for attempt in attempts):
                setting.blocker_code = "predecessor_integration_unverified"
                await work.commit()
                return execution_id
            # The bridge's default admission fence is epic-wide. An independent
            # ready sibling still waits for an older/manual child, including a
            # child from another execution, before consuming a dispatch claim.
            waiting = None
            for attempt in await work.epic_run_bridge.list_attempts(epic_id):
                run = await work.runs.get_for_update(attempt.run_id)
                hold = await work.session.get(EpicChildBudgetHold, attempt.attempt_id)
                if run.state not in {"COMPLETED", "FAILED", "CANCELLED"}:
                    waiting = "active_child"
                    break
                if not (await work.runs.prove_quiescent(run.id)).is_quiescent or hold is None or not hold.effects_settled:
                    waiting = "child_effects_unsettled"
                    break
            if waiting is not None:
                setting.blocker_code = waiting
                await work.commit()
                return execution_id
            execution = await work.session.get(EpicExecution, execution_id)
            assert execution is not None
            token = uuid4()
            setting.claim_item_id = ready.item_id
            setting.claim_token = token
            setting.claim_expires_at = now + _CLAIM_LIFETIME
            setting.blocker_code = None
            actor = AuthenticatedActor(actor_id=setting.actor_id, actor_class="operator", session_id=setting.session_id)
            request = LaunchRequest(
                expected_epic_version=epic.version,
                execution_id=execution_id,
                brief_revision_id=execution.brief_revision_id,
                brief_digest=execution.brief_digest,
                graph_revision_id=execution.graph_revision_id,
                graph_digest=execution.graph_digest,
                item_id=ready.item_id,
                profile_id=setting.profile_id,
                profile_version=setting.profile_version,
            )
            await work.commit()
        try:
            await self._bridge.launch(
                actor=actor, epic_id=epic_id,
                idempotency_key=f"epic.dispatch:{execution_id}:{ready.item_id}",
                request=request, dispatch_claim_token=token,
            )
            blocker = None
        except EpicLaunchConflict as conflict:
            waiting = next(
                (code for code in conflict.blocker_codes if code in {"active_child", "child_effects_unsettled"}),
                None,
            )
            permanent = next(
                (
                    code for code in conflict.blocker_codes
                    if code == "predecessor_failed"
                    or (code.startswith("epic_budget_") and code.endswith("_exhausted"))
                ),
                None,
            )
            blocker = permanent or waiting or (
                conflict.blocker_codes[0] if conflict.blocker_codes else "dispatch_blocked"
            )
        except Exception:  # noqa: BLE001 - retained claim is settled with a safe blocker
            blocker = "dispatch_error"
        async with self._work() as work:
            await work.epics.get(epic_id, for_update=True)
            setting = await work.session.get(EpicDispatchSetting, execution_id, with_for_update=True)
            if setting is not None and setting.claim_token == token:
                if blocker == "predecessor_failed":
                    attempts = await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution_id)
                    if not await latest_required_child_failed(attempts, execution_id, work.runs.get):
                        blocker = "active_child"
                setting.claim_item_id = setting.claim_token = setting.claim_expires_at = None
                setting.blocker_code = blocker
                setting.checked_at = datetime.now(UTC)
                if blocker == "predecessor_failed" or (
                    blocker is not None
                    and blocker.startswith("epic_budget_")
                    and blocker.endswith("_exhausted")
                ):
                    control = await work.session.get(EpicExecutionControl, execution_id, with_for_update=True)
                    if control is not None and control.state == "ACTIVE":
                        control.state = "BLOCKED"
                        control.version += 1
                        control.blocker_code = blocker
                if blocker is not None and blocker not in {"active_child", "child_effects_unsettled"}:
                    # Admission has a zero automatic retry budget. The owner
                    # may re-enable a refusal or directly override a blocked control.
                    setting.enabled = False
            await work.commit()
        return execution_id
