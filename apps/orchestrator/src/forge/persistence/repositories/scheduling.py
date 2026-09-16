"""Short-transaction scheduler; task renewal/terminalization lock runs first."""

from dataclasses import replace
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from forge.application.ports.scheduling import (
    SchedulingConflict,
    SchedulingLeaseLost,
    SchedulingLeaseRevoked,
)
from forge.domain.operation import canonical_digest
from forge.domain.paths import policy_path_key
from forge.domain.scheduling import (
    SchedulerCapacityPolicy,
    ScheduleTask,
    TaskEffectLease,
    TaskLease,
)
from forge.domain.subscription import (
    CANDIDATE_READ_TOOLS,
    BoundReassignDecision,
    DelegateDecision,
    ForwardFeedbackDecision,
    LogicalTaskContract,
    RouteBinding,
    RouteSpec,
    ScopeRequestDecision,
    SpecialistPurpose,
    TaskBudget,
    ToolCallBinding,
    WaitDecision,
    decode_subscription_record,
    encode_subscription_record,
    is_read_only,
)
from forge.domain.subscription_execution import run_allows_subscription_attempt
from forge.domain.tool import ToolName
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerCapacityPolicy,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionDecisionRecord,
    SubscriptionEnvelope,
    SubscriptionOperationBinding,
    SubscriptionTask,
)
from forge.persistence.models.subscription_handoff import SubscriptionHandoffFence
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.repositories.subscription_budget import PostgresSubscriptionBudgetRepository
from forge.persistence.repositories.subscription_quota import PostgresSubscriptionQuotaRepository


class PostgresSchedulingRepository:
    def __init__(
        self, session: AsyncSession, *, quota: PostgresSubscriptionQuotaRepository | None = None
    ) -> None:
        self._session = session
        self._quota = quota or PostgresSubscriptionQuotaRepository(session)

    async def _lock_run(self, run_id: UUID) -> None:
        if (
            await self._session.scalar(select(Run.id).where(Run.id == run_id).with_for_update())
            is None
        ):
            raise SchedulingConflict("scheduled run not found")

    async def enqueue(self, task: ScheduleTask) -> ScheduleTask:
        await self._lock_run(task.run_id)
        run = await self._session.get(
            SubscriptionSchedulerRun, task.run_id, with_for_update=True, populate_existing=True
        )
        contract_row = await self._session.get(SubscriptionTask, task.task_id)
        if contract_row is None or contract_row.run_id != task.run_id:
            raise SchedulingConflict("scheduled task requires persisted logical contract")
        contract = decode_subscription_record(contract_row.payload)
        if not isinstance(contract, LogicalTaskContract) or (
            task.parent_task_id != contract.parent_task_id
            or task.dependency_task_ids != contract.dependency_task_ids
            or task.owned_paths != tuple(policy_path_key(path) for path in contract.owned_paths)
            or task.max_repairs != contract.max_repairs
            or task.read_only != is_read_only(contract.purpose)
        ):
            raise SchedulingConflict("scheduled task authority differs from logical contract")
        if (
            run is not None
            and run.candidate_state != "open"
            and not (run.candidate_state == "closed" and task.read_only)
        ):
            raise SchedulingConflict("candidate barrier denies new task admission")
        row = await self._session.get(SubscriptionScheduledTask, task.task_id, with_for_update=True)
        if row is not None:
            if row.run_id != task.run_id:
                raise SchedulingConflict("task identity belongs to another run")
            if not self._matches(row, task, contract.route.effective.provider):
                raise SchedulingConflict("scheduled task contract is immutable")
            return task
        self._session.add(
            SubscriptionScheduledTask(
                id=task.task_id,
                run_id=task.run_id,
                task_id=task.task_id,
                parent_task_id=task.parent_task_id,
                worktree_id=task.worktree_id,
                provider=contract.route.effective.provider,
                owned_paths=list(task.owned_paths),
                dependency_task_ids=list(task.dependency_task_ids),
                read_only=task.read_only,
                max_repairs=task.max_repairs,
            )
        )
        await self._session.flush()
        return task

    async def admit_run(self, run_id: UUID) -> None:
        if await self._session.get(SubscriptionEnvelope, run_id) is None:
            return
        policy = await self._current_policy()
        stmt = (
            insert(SubscriptionSchedulerRun)
            .values(
                run_id=run_id,
                admitted=True,
                capacity_policy_version=policy.version,
                effective_run_limit=policy.run_limit,
            )
            .on_conflict_do_update(index_elements=["run_id"], set_={"admitted": True})
        )
        await self._session.execute(stmt)

    async def configure_capacity(self, policy: SchedulerCapacityPolicy) -> None:
        """Trusted operator/deployment configuration; tasks never supply limits."""
        existing = await self._session.get(
            SubscriptionSchedulerCapacityPolicy, policy.version, with_for_update=True
        )
        if existing is not None:
            if (existing.global_limit, existing.run_limit, existing.provider_limit) != (
                policy.global_limit,
                policy.run_limit,
                policy.provider_limit,
            ):
                raise SchedulingConflict("capacity policy version is immutable")
            return
        self._session.add(
            SubscriptionSchedulerCapacityPolicy(
                version=policy.version,
                global_limit=policy.global_limit,
                run_limit=policy.run_limit,
                provider_limit=policy.provider_limit,
            )
        )
        await self._session.flush()

    async def claim_ready(self, owner: str, lease_for: timedelta) -> TaskLease | None:
        return await self._claim_ready(owner, lease_for, fresh_attempt=False)

    async def claim_execution_ready(
        self,
        owner: str,
        lease_for: timedelta,
        *,
        eligible_routes: frozenset[RouteSpec] | None = None,
        reservation_ceiling: TaskBudget | None = None,
    ) -> TaskLease | None:
        """Claim only tasks eligible for a new accounted provider attempt."""
        return await self._claim_ready(
            owner,
            lease_for,
            fresh_attempt=True,
            eligible_routes=eligible_routes,
            reservation_ceiling=reservation_ceiling,
        )

    async def _claim_ready(
        self,
        owner: str,
        lease_for: timedelta,
        *,
        fresh_attempt: bool,
        eligible_routes: frozenset[RouteSpec] | None = None,
        reservation_ceiling: TaskBudget | None = None,
    ) -> TaskLease | None:
        if not owner.strip() or lease_for.total_seconds() < 1:
            raise ValueError("valid owner and lease required")
        now = self._quota.now()
        expiry = now + lease_for
        # Serializes only the brief admission decision.  The provider call happens
        # after the UoW closes, so this cannot turn into a provider-wide lock.
        await self._session.execute(text("SELECT pg_advisory_xact_lock(91827364)"))
        expired_runs = (
            await self._session.scalars(
                select(SubscriptionScheduledTask.run_id)
                .where(
                    SubscriptionScheduledTask.state == "leased",
                    SubscriptionScheduledTask.lease_expires_at < now,
                )
                .distinct()
            )
        ).all()
        for run_id in expired_runs:
            locked = await self._session.scalar(
                select(Run.id).where(Run.id == run_id).with_for_update(skip_locked=True)
            )
            if locked is not None:
                await self._session.execute(
                    update(SubscriptionScheduledTask)
                    .where(
                        SubscriptionScheduledTask.run_id == run_id,
                        SubscriptionScheduledTask.state == "leased",
                        SubscriptionScheduledTask.lease_expires_at < now,
                    )
                    .values(state="reconciling")
                )
        # Parents wait durably: dependencies must be terminal before admission.
        candidates = (
            (
                await self._session.execute(
                    select(SubscriptionScheduledTask)
                    .join(
                        SubscriptionSchedulerRun,
                        SubscriptionSchedulerRun.run_id == SubscriptionScheduledTask.run_id,
                    )
                    .where(
                        SubscriptionSchedulerRun.admitted.is_(True),
                        SubscriptionSchedulerRun.candidate_state.in_(("open", "closed")),
                        SubscriptionScheduledTask.state == "queued",
                        SubscriptionScheduledTask.pause_requested.is_(False),
                        SubscriptionScheduledTask.cancel_requested.is_(False),
                    )
                    .order_by(
                        SubscriptionSchedulerRun.last_claimed_at.nullsfirst(),
                        SubscriptionScheduledTask.created_at,
                    )
                )
            )
            .scalars()
            .all()
        )
        row = None
        policy = await self._current_policy()
        global_active = await self._active_count()
        selected_route: RouteBinding | None = None
        selected_logical: SubscriptionTask | None = None
        for candidate in candidates:
            # Atomic attempt admission continues by locking this run. Acquire it
            # before scheduler rows, matching broker/usage lock order, and skip
            # occupied runs so a different worktree can still progress.
            locked = await self._session.scalar(
                select(Run.id).where(Run.id == candidate.run_id).with_for_update(skip_locked=True)
            )
            if locked is None:
                continue
            run = await self._session.get(
                SubscriptionSchedulerRun, candidate.run_id, with_for_update=True
            )
            current = await self._session.scalar(
                select(SubscriptionScheduledTask)
                .where(
                    SubscriptionScheduledTask.id == candidate.id,
                    SubscriptionScheduledTask.state == "queued",
                    SubscriptionScheduledTask.pause_requested.is_(False),
                    SubscriptionScheduledTask.cancel_requested.is_(False),
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if current is None or run is None or not run.admitted:
                continue
            if run.candidate_state != "open" and not (
                run.candidate_state == "closed" and await self._can_read_candidate(current)
            ):
                continue
            candidate = current
            route: RouteBinding | None = None
            logical: SubscriptionTask | None = None
            if fresh_attempt:
                source_run = await self._session.get(Run, candidate.run_id, populate_existing=True)
                if source_run is None or not run_allows_subscription_attempt(
                    source_run.state, source_run.pending_gate
                ):
                    continue
                # Do not repeatedly select a task that execution admission must
                # reject: rolling that lease back would starve unrelated worktrees.
                unsettled = await self._session.scalar(
                    select(SubscriptionAttempt.id)
                    .outerjoin(
                        SubscriptionAttemptConsumption,
                        SubscriptionAttemptConsumption.attempt_id == SubscriptionAttempt.id,
                    )
                    .where(
                        SubscriptionAttempt.task_row_id == candidate.task_id,
                        or_(
                            SubscriptionAttempt.status != "terminal",
                            SubscriptionAttempt.lease_owner.is_(None),
                            SubscriptionAttemptConsumption.attempt_id.is_(None),
                        ),
                    )
                    .limit(1)
                )
                pending_effect = await self._session.scalar(
                    select(SubscriptionScheduledEffect.id)
                    .where(
                        SubscriptionScheduledEffect.run_id == candidate.run_id,
                        SubscriptionScheduledEffect.task_id == candidate.task_id,
                        SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
                    )
                    .limit(1)
                )
                logical = await self._session.get(SubscriptionTask, candidate.task_id)
                if (
                    unsettled is not None
                    or pending_effect is not None
                    or logical is None
                    or logical.state != "queued"
                    or logical.pause_requested
                    or logical.cancel_requested
                ):
                    continue
                route = await self._quota.route_for_task(logical, eligible_routes=eligible_routes)
                if route is None:
                    continue
                if (
                    reservation_ceiling is not None
                    and await PostgresSubscriptionBudgetRepository(self._session).fit_reservation(
                        candidate.run_id, candidate.task_id, reservation_ceiling
                    )
                    is None
                ):
                    continue
            if run is None or global_active >= policy.global_limit:
                continue
            if await self._active_count(run_id=candidate.run_id) >= run.effective_run_limit:
                continue
            if (
                await self._active_count(
                    provider=route.effective.provider if route else candidate.provider
                )
                >= policy.provider_limit
            ):
                continue
            if (
                not candidate.read_only
                and not await self._is_coordinator(candidate)
                and await self._has_exclusive_effect_barrier(candidate)
            ):
                continue
            if not candidate.dependency_task_ids and not await self._has_worktree_conflict(
                candidate
            ):
                row = candidate
                selected_route, selected_logical = route, logical
                break
            pending = (
                await self._session.execute(
                    select(SubscriptionScheduledTask.id).where(
                        SubscriptionScheduledTask.run_id == candidate.run_id,
                        SubscriptionScheduledTask.task_id.in_(candidate.dependency_task_ids),
                        SubscriptionScheduledTask.state != "terminal",
                    )
                )
            ).first()
            if pending is None and not await self._has_worktree_conflict(candidate):
                row = candidate
                selected_route, selected_logical = route, logical
                break
        if row is None:
            return None
        if selected_route is not None and selected_logical is not None:
            contract = decode_subscription_record(selected_logical.payload)
            if not isinstance(contract, LogicalTaskContract):
                raise SchedulingConflict("invalid quota task contract")
            if contract.route != selected_route:
                selected_logical.payload = encode_subscription_record(
                    replace(contract, route=selected_route)
                )
                selected_logical.version += 1
                row.provider = selected_route.effective.provider
        row.state, row.lease_owner, row.lease_generation, row.lease_expires_at = (
            "leased",
            owner,
            row.lease_generation + 1,
            expiry,
        )
        run = await self._session.get(SubscriptionSchedulerRun, row.run_id, with_for_update=True)
        if run is not None:
            run.last_claimed_at = now
        await self._session.flush()
        return TaskLease(
            run_id=row.run_id,
            task_id=row.task_id,
            owner=owner,
            generation=row.lease_generation,
            expires_at=expiry,
        )

    async def renew(self, lease: TaskLease, lease_for: timedelta) -> TaskLease:
        if lease_for.total_seconds() < 1:
            raise ValueError("valid renewal duration required")
        source_run = await self._session.scalar(
            select(Run)
            .where(Run.id == lease.run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if source_run is None:
            raise SchedulingLeaseLost("scheduled run is absent")
        if not run_allows_subscription_attempt(source_run.state, source_run.pending_gate):
            raise SchedulingLeaseRevoked("task lease is fenced by run control")
        row = await self._task(lease, lock=True)
        logical = await self._session.get(
            SubscriptionTask, lease.task_id, populate_existing=True, with_for_update=True
        )
        if (
            row.pause_requested
            or row.cancel_requested
            or (logical is not None and (logical.pause_requested or logical.cancel_requested))
        ):
            raise SchedulingLeaseRevoked("task lease is revoked")
        expiry = self._quota.now() + lease_for
        row.lease_expires_at = expiry
        await self._session.flush()
        return TaskLease(
            run_id=lease.run_id,
            task_id=lease.task_id,
            owner=lease.owner,
            generation=lease.generation,
            expires_at=expiry,
        )

    async def finish(
        self, lease: TaskLease, *, successful: bool, allow_repair: bool = True
    ) -> None:
        await self._lock_run(lease.run_id)
        row = await self._task(lease, lock=True)
        # A stop is a durable revocation of result acceptance: a late provider
        # completion can settle its fence but never revive or accept the task.
        if (
            row.pause_requested
            or row.cancel_requested
            or successful
            or not allow_repair
            or row.repairs >= row.max_repairs
        ):
            terminal = True
        else:
            row.repairs += 1
            terminal = False
        await self._release_task(row, terminal=terminal)

    async def defer_quota(self, lease: TaskLease) -> None:
        """Release execution capacity without spending a repair or waking dependencies."""
        await self._lock_run(lease.run_id)
        row = await self._task(lease, lock=True)
        if row.pause_requested or row.cancel_requested:
            raise SchedulingLeaseRevoked("quota deferral lease is revoked")
        await self._release_task(row, terminal=False)

    async def admit_effect(
        self,
        lease: TaskLease,
        effect_id: UUID,
        *,
        owned_paths: tuple[str, ...] = (),
        whole_worktree_exclusive: bool = False,
        expected_candidate_epoch: int | None = None,
    ) -> TaskEffectLease:
        await self._lock_run(lease.run_id)
        row = await self._task(lease, lock=True)
        run = await self._session.get(SubscriptionSchedulerRun, lease.run_id, with_for_update=True)
        if run is None or row.pause_requested or row.cancel_requested:
            raise SchedulingConflict("task effect admission is revoked")
        if expected_candidate_epoch is not None and expected_candidate_epoch != run.candidate_epoch:
            raise SchedulingConflict("candidate epoch differs")
        paths = tuple(policy_path_key(path) for path in owned_paths)
        if any(
            not any(
                path == permitted or path.startswith(permitted.rstrip("/") + "/")
                for permitted in row.owned_paths
            )
            for path in paths
        ):
            raise SchedulingConflict("effect paths exceed persisted task ownership")
        existing = await self._session.get(
            SubscriptionScheduledEffect, effect_id, with_for_update=True
        )
        coordinator = await self._is_coordinator(row)
        read_effect = (coordinator or row.read_only) and await self._is_bound_read(
            effect_id, row, lease.owner, lease.generation
        )
        if existing is not None and read_effect and existing.whole_worktree_exclusive:
            # Earlier primary Git reads were conservatively exclusive. Their
            # replay retains that stored mode even after read classification.
            whole_worktree_exclusive = True
        if (
            coordinator
            and not read_effect
            and (existing is None or existing.whole_worktree_exclusive)
        ):
            # New primary mutations reserve the worktree only for their effect.
            # Older nonexclusive effects retain their exact replay identity;
            # _has_exclusive_effect_barrier still fences their uncertain writes.
            whole_worktree_exclusive = True
        if existing is not None and (
            existing.run_id,
            existing.task_id,
            existing.lease_owner,
            existing.lease_generation,
            existing.whole_worktree_exclusive,
        ) != (
            lease.run_id,
            lease.task_id,
            lease.owner,
            lease.generation,
            whole_worktree_exclusive,
        ):
            raise SchedulingConflict("effect identity conflicts")
        if (
            ((not row.read_only and not read_effect) or whole_worktree_exclusive)
            and existing is None
            and await self._has_exclusive_effect_barrier(row)
        ):
            raise SchedulingConflict("worktree exclusive effect barrier is active")
        snapshot_read = whole_worktree_exclusive and await self._is_bound_read(
            effect_id, row, lease.owner, lease.generation, snapshot_only=True
        )
        if (
            whole_worktree_exclusive
            and existing is None
            and await self._has_effect_conflict(row, snapshot_read=snapshot_read)
        ):
            raise SchedulingConflict("worktree is not exclusively available")
        if run.candidate_state != "open" and not (run.candidate_state == "closed" and read_effect):
            raise SchedulingConflict("candidate barrier denies task effect admission")
        if existing is None:
            self._session.add(
                SubscriptionScheduledEffect(
                    id=effect_id,
                    run_id=lease.run_id,
                    task_id=lease.task_id,
                    lease_owner=lease.owner,
                    lease_generation=lease.generation,
                    candidate_epoch=run.candidate_epoch,
                    whole_worktree_exclusive=whole_worktree_exclusive,
                )
            )
        await self._session.flush()
        return TaskEffectLease(
            effect_id=effect_id, task_lease=lease, candidate_epoch=run.candidate_epoch
        )

    async def settle_effect(self, effect: TaskEffectLease, *, accepted: bool) -> bool:
        lease = effect.task_lease
        task = (
            await self._session.execute(
                select(SubscriptionScheduledTask)
                .where(
                    SubscriptionScheduledTask.run_id == lease.run_id,
                    SubscriptionScheduledTask.task_id == lease.task_id,
                    SubscriptionScheduledTask.lease_owner == lease.owner,
                    SubscriptionScheduledTask.lease_generation == lease.generation,
                    SubscriptionScheduledTask.state == "leased",
                    SubscriptionScheduledTask.lease_expires_at > self._quota.now(),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        row = await self._effect(effect)
        if task is None or task.pause_requested or task.cancel_requested:
            accepted = False
        row.state = "settled" if accepted else "rejected"
        await self._session.flush()
        return accepted

    async def reconcile_effect(self, effect: TaskEffectLease) -> None:
        row = await self._effect(effect, admitted_only=False)
        if row.state == "admitted":
            row.state = "reconciling"
            await self._session.flush()

    async def yield_to_children(self, lease: TaskLease, children: tuple[ScheduleTask, ...]) -> None:
        parent = await self._task(lease, lock=True)
        if not children:
            raise SchedulingConflict("parent yield requires children")
        for child in children:
            if child.run_id != lease.run_id or child.parent_task_id != lease.task_id:
                raise SchedulingConflict("child lineage differs from yielding parent")
            await self.enqueue(child)
        parent.state = "blocked"
        parent.lease_owner = None
        parent.lease_expires_at = None
        await self._session.flush()

    async def reconcile_expired(self, run_id: UUID, task_id: UUID, *, retry: bool) -> None:
        await self._lock_run(run_id)
        row = await self._session.get(SubscriptionScheduledTask, task_id, with_for_update=True)
        if row is None or row.run_id != run_id:
            raise SchedulingConflict("scheduled task not found")
        if row.state != "reconciling":
            raise SchedulingConflict("task is not awaiting reconciliation")
        if retry:
            unresolved_effect = (
                await self._session.execute(
                    select(SubscriptionScheduledEffect.id).where(
                        SubscriptionScheduledEffect.run_id == run_id,
                        SubscriptionScheduledEffect.task_id == task_id,
                        SubscriptionScheduledEffect.lease_generation == row.lease_generation,
                        SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
                    )
                )
            ).first()
            if unresolved_effect is not None:
                raise SchedulingConflict("task effect requires reconciliation before retry")
        await self._release_task(row, terminal=not retry or row.cancel_requested)

    async def begin_candidate(self, run_id: UUID) -> int:
        await self._lock_run(run_id)
        row = await self._session.get(SubscriptionSchedulerRun, run_id, with_for_update=True)
        if row is None or not row.admitted:
            raise SchedulingConflict("run is not admitted")
        if row.candidate_state != "open":
            raise SchedulingConflict("candidate barrier already active")
        row.candidate_state = "draining"
        await self._session.flush()
        return row.candidate_epoch

    async def close_candidate(self, run_id: UUID, epoch: int) -> None:
        await self._lock_run(run_id)
        run = await self._session.get(SubscriptionSchedulerRun, run_id, with_for_update=True)
        if run is None or run.candidate_state != "draining" or run.candidate_epoch != epoch:
            raise SchedulingConflict("candidate epoch differs")
        active = (
            await self._session.execute(
                select(SubscriptionScheduledTask.id).where(
                    SubscriptionScheduledTask.run_id == run_id,
                    SubscriptionScheduledTask.state.in_(("leased", "reconciling")),
                )
            )
        ).first()
        if active is not None:
            raise SchedulingConflict("candidate barrier still draining effects")
        unsettled_effect = (
            await self._session.execute(
                select(SubscriptionScheduledEffect.id).where(
                    SubscriptionScheduledEffect.run_id == run_id,
                    SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
                )
            )
        ).first()
        if unsettled_effect is not None:
            raise SchedulingConflict("candidate barrier still draining effects")
        run.candidate_state = "closed"
        run.candidate_epoch += 1
        await self._session.flush()

    async def request_stop(self, run_id: UUID, task_id: UUID, *, cancel: bool) -> None:
        await self._lock_run(run_id)
        row = await self._session.get(SubscriptionScheduledTask, task_id, with_for_update=True)
        if row is None or row.run_id != run_id:
            raise SchedulingConflict("scheduled task not found")
        if cancel:
            row.cancel_requested = True
        else:
            row.pause_requested = True
        if row.state in ("queued", "blocked"):
            await self._release_task(row, terminal=True)
        else:
            await self._session.flush()

    async def _release_task(self, row: SubscriptionScheduledTask, *, terminal: bool) -> None:
        row.state = "terminal" if terminal else "queued"
        row.lease_owner = None
        row.lease_expires_at = None
        await self._session.flush()
        if terminal:
            await self._wake_ready_parents(row)

    async def _task(self, lease: TaskLease, *, lock: bool) -> SubscriptionScheduledTask:
        stmt = select(SubscriptionScheduledTask).where(
            SubscriptionScheduledTask.run_id == lease.run_id,
            SubscriptionScheduledTask.task_id == lease.task_id,
        )
        if lock:
            stmt = stmt.with_for_update()
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if (
            row is None
            or row.state != "leased"
            or row.lease_owner != lease.owner
            or row.lease_generation != lease.generation
            or row.lease_expires_at is None
        ):
            raise SchedulingLeaseLost("stale task lease")
        if row.lease_expires_at <= self._quota.now():
            row.state = "reconciling"
            raise SchedulingLeaseLost("task lease expired and requires reconciliation")
        return row

    async def _effect(
        self, effect: TaskEffectLease, *, admitted_only: bool = True
    ) -> SubscriptionScheduledEffect:
        row = await self._session.get(
            SubscriptionScheduledEffect, effect.effect_id, with_for_update=True
        )
        lease = effect.task_lease
        if (
            row is None
            or (row.run_id, row.task_id, row.lease_owner, row.lease_generation, row.candidate_epoch)
            != (lease.run_id, lease.task_id, lease.owner, lease.generation, effect.candidate_epoch)
            or row.state not in ("admitted", "reconciling", "settled", "rejected")
            or (admitted_only and row.state != "admitted")
        ):
            raise SchedulingConflict("stale or settled effect lease")
        return row

    async def _can_read_candidate(self, row: SubscriptionScheduledTask) -> bool:
        if await self._is_coordinator(row):
            return True
        logical = await self._session.get(SubscriptionTask, row.task_id, populate_existing=True)
        if logical is None or not row.read_only:
            return False
        try:
            contract = decode_subscription_record(logical.payload)
        except KeyError, TypeError, ValueError:
            return False
        return (
            isinstance(contract, LogicalTaskContract)
            and is_read_only(contract.purpose)
            and contract.run_id == logical.run_id == row.run_id
            and contract.task_id == logical.id == logical.task_id == row.task_id
            and contract.parent_task_id == logical.parent_task_id == row.parent_task_id
            and tuple(policy_path_key(path) for path in contract.owned_paths)
            == tuple(row.owned_paths)
        )

    async def _is_coordinator(self, row: SubscriptionScheduledTask) -> bool:
        logical = await self._session.get(SubscriptionTask, row.task_id, populate_existing=True)
        if logical is None or logical.parent_task_id is not None or row.parent_task_id is not None:
            return False
        try:
            contract = decode_subscription_record(logical.payload)
        except KeyError, TypeError, ValueError:
            return False
        return (
            isinstance(contract, LogicalTaskContract)
            and contract.purpose is SpecialistPurpose.PRIMARY
            and contract.parent_task_id is None
            and contract.route.is_primary
            and contract.run_id == logical.run_id == row.run_id
            and contract.task_id == logical.id == logical.task_id == row.task_id
            and tuple(policy_path_key(path) for path in contract.owned_paths)
            == tuple(row.owned_paths)
            and not row.read_only
        )

    async def _is_bound_read(
        self,
        effect_id: UUID,
        owner: SubscriptionScheduledTask,
        lease_owner: str,
        generation: int,
        *,
        snapshot_only: bool = False,
    ) -> bool:
        rows = (
            await self._session.execute(
                select(SubscriptionOperationBinding, SubscriptionAttempt)
                .join(
                    SubscriptionAttempt,
                    SubscriptionAttempt.id == SubscriptionOperationBinding.attempt_id,
                )
                .where(SubscriptionOperationBinding.durable_operation_id == effect_id)
                .limit(2)
                .execution_options(populate_existing=True)
            )
        ).all()
        if len(rows) != 1:
            return False
        record, attempt = rows[0]
        try:
            binding = decode_subscription_record(record.payload)
        except KeyError, TypeError, ValueError:
            return False
        return (
            isinstance(binding, ToolCallBinding)
            and binding.attempt_id == record.attempt_id == attempt.id
            and binding.durable_operation_id == effect_id
            and binding.provider_call_key == record.provider_call_key
            and (attempt.run_id, attempt.task_row_id, attempt.lease_owner, attempt.lease_generation)
            == (owner.run_id, owner.task_id, lease_owner, generation)
            and binding.tool_name in CANDIDATE_READ_TOOLS
            and (
                not snapshot_only
                or binding.tool_name is ToolName.GIT_DIFF
                and binding.arguments_digest == canonical_digest({"scope": "snapshot"})
            )
        )

    async def _has_worktree_conflict(self, candidate: SubscriptionScheduledTask) -> bool:
        if (
            candidate.read_only
            or not candidate.owned_paths
            or await self._is_coordinator(candidate)
        ):
            return False
        active = (
            await self._session.execute(
                select(SubscriptionScheduledTask).where(
                    SubscriptionScheduledTask.worktree_id == candidate.worktree_id,
                    SubscriptionScheduledTask.state.in_(("leased", "reconciling")),
                    SubscriptionScheduledTask.read_only.is_(False),
                )
            )
        ).scalars()
        for existing in active:
            if await self._is_coordinator(existing):
                continue
            if any(
                _paths_overlap(candidate_path, active_path)
                for candidate_path in candidate.owned_paths
                for active_path in existing.owned_paths
            ):
                return True
        return False

    async def _has_effect_conflict(
        self, candidate: SubscriptionScheduledTask, *, snapshot_read: bool = False
    ) -> bool:
        active = (
            await self._session.execute(
                select(SubscriptionScheduledTask).where(
                    SubscriptionScheduledTask.worktree_id == candidate.worktree_id,
                    SubscriptionScheduledTask.id != candidate.id,
                    SubscriptionScheduledTask.state.in_(("leased", "reconciling")),
                    SubscriptionScheduledTask.read_only.is_(False),
                )
            )
        ).scalars()
        for existing in active:
            if not await self._is_coordinator(existing) and (
                not snapshot_read or existing.state == "reconciling"
            ):
                return True
        pending = select(SubscriptionScheduledEffect.id)
        if snapshot_read:
            # A read snapshot freezes new mutations through its durable exclusive
            # effect. Other live, idle writers need not exit, but every admitted
            # or uncertain effect in the worktree must settle before capture.
            pending = pending.outerjoin(
                SubscriptionScheduledTask,
                (SubscriptionScheduledTask.task_id == SubscriptionScheduledEffect.task_id)
                & (SubscriptionScheduledTask.run_id == SubscriptionScheduledEffect.run_id),
            ).where(
                or_(
                    SubscriptionScheduledTask.worktree_id == candidate.worktree_id,
                    SubscriptionScheduledTask.id.is_(None)
                    & (SubscriptionScheduledEffect.run_id == candidate.run_id),
                )
            )
        else:
            pending = pending.where(
                SubscriptionScheduledEffect.run_id == candidate.run_id,
                SubscriptionScheduledEffect.task_id == candidate.task_id,
            )
        return (
            await self._session.scalar(
                pending.where(
                    SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
                ).limit(1)
            )
            is not None
        )

    async def _has_exclusive_effect_barrier(self, candidate: SubscriptionScheduledTask) -> bool:
        """Return whether an unsettled exclusive effect fences this worktree."""
        observation = await self._session.scalar(
            select(SubscriptionHandoffFence.token).where(
                SubscriptionHandoffFence.worktree_id == candidate.worktree_id,
                SubscriptionHandoffFence.expires_at > func.clock_timestamp(),
            )
        )
        if observation is not None:
            return True
        active = (
            await self._session.execute(
                select(SubscriptionScheduledEffect, SubscriptionScheduledTask)
                .select_from(SubscriptionScheduledEffect)
                .join(
                    SubscriptionScheduledTask,
                    (SubscriptionScheduledTask.run_id == SubscriptionScheduledEffect.run_id)
                    & (SubscriptionScheduledTask.task_id == SubscriptionScheduledEffect.task_id),
                )
                .where(
                    SubscriptionScheduledTask.worktree_id == candidate.worktree_id,
                    SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
                )
            )
        ).all()
        for effect, owner in active:
            if effect.whole_worktree_exclusive or (
                await self._is_coordinator(owner)
                and not await self._is_bound_read(
                    effect.id, owner, effect.lease_owner, effect.lease_generation
                )
            ):
                return True
        return False

    async def _current_policy(self) -> SchedulerCapacityPolicy:
        row = (
            await self._session.execute(
                select(SubscriptionSchedulerCapacityPolicy)
                .order_by(SubscriptionSchedulerCapacityPolicy.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return SchedulerCapacityPolicy(version=1)
        return SchedulerCapacityPolicy(
            version=row.version,
            global_limit=row.global_limit,
            run_limit=row.run_limit,
            provider_limit=row.provider_limit,
        )

    async def _active_count(
        self, *, run_id: UUID | None = None, provider: str | None = None
    ) -> int:
        from forge.persistence.repositories.subscription_stopped_capacity import (
            paused_task_is_stopped,
        )

        # Stopped pauses retain their path reservation. They no longer own a
        # provider execution slot; requested/unproved stops remain counted.
        predicates: list[ColumnElement[bool]] = [
            SubscriptionScheduledTask.state.in_(("leased", "reconciling")),
        ]
        if run_id is not None:
            predicates.append(SubscriptionScheduledTask.run_id == run_id)
        if provider is not None:
            predicates.append(SubscriptionScheduledTask.provider == provider)
        rows = list(
            await self._session.scalars(
                select(SubscriptionScheduledTask)
                .where(*predicates)
                .execution_options(populate_existing=True)
            )
        )
        count = len(rows)
        for row in rows:
            if await paused_task_is_stopped(self._session, row):
                count -= 1
        return count

    @staticmethod
    def _matches(row: SubscriptionScheduledTask, task: ScheduleTask, provider: str) -> bool:
        return (
            row.task_id == task.task_id
            and row.parent_task_id == task.parent_task_id
            and tuple(row.dependency_task_ids) == task.dependency_task_ids
            and row.worktree_id == task.worktree_id
            and tuple(row.owned_paths) == task.owned_paths
            and row.read_only == task.read_only
            and row.max_repairs == task.max_repairs
            and row.provider == provider
        )

    async def has_scope_request(self, run_id: UUID, task_ids: tuple[UUID, ...]) -> bool:
        children = (
            await self._session.scalars(
                select(SubscriptionScheduledTask).where(
                    SubscriptionScheduledTask.run_id == run_id,
                    SubscriptionScheduledTask.task_id.in_(task_ids),
                )
            )
        ).all()
        for child in children:
            if await self._scope_request_pending(child):
                return True
        return False

    async def pending_scope_request(
        self, run_id: UUID, task_id: UUID
    ) -> tuple[UUID, ScopeRequestDecision] | None:
        child = await self._session.get(SubscriptionScheduledTask, task_id)
        if child is None or child.run_id != run_id:
            return None
        return await self._scope_request_pending(child)

    async def _scope_request_pending(
        self, child: SubscriptionScheduledTask
    ) -> tuple[UUID, ScopeRequestDecision] | None:
        if child.state != "blocked":
            return None
        attempt = await self._session.scalar(
            select(SubscriptionAttempt)
            .where(
                SubscriptionAttempt.run_id == child.run_id,
                SubscriptionAttempt.task_row_id == child.task_id,
            )
            .order_by(SubscriptionAttempt.attempt_number.desc())
            .limit(1)
        )
        if attempt is None or attempt.status != "terminal":
            return None
        result = await self._session.get(SubscriptionAttemptResult, attempt.id)
        record = await self._session.scalar(
            select(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.run_id == child.run_id,
                SubscriptionDecisionRecord.task_row_id == child.task_id,
                SubscriptionDecisionRecord.attempt_id == attempt.id,
                SubscriptionDecisionRecord.idempotency_key == f"scope-request:{attempt.id}",
            )
        )
        if (
            result is None
            or not result.accepted
            or result.disposition != "scope_requested"
            or record is None
            or canonical_digest(result.result_payload) != result.result_digest
            or record.payload != result.result_payload.get("decision")
        ):
            return None
        try:
            decision = decode_subscription_record(record.payload)
        except TypeError, ValueError:
            return None
        if (
            isinstance(decision, ScopeRequestDecision)
            and record.record_type == "ScopeRequestDecision"
            and decision.run_id == child.run_id
            and decision.task_id == child.task_id
        ):
            return attempt.id, decision
        return None

    async def wake_parent_for_scope_request(self, run_id: UUID, task_id: UUID) -> None:
        await self._lock_run(run_id)
        child = await self._session.get(SubscriptionScheduledTask, task_id, with_for_update=True)
        if child is None or child.run_id != run_id or not await self._scope_request_pending(child):
            raise SchedulingConflict("scope request wake source differs")
        await self._wake_ready_parents(child, scope_request=True)

    async def _wake_ready_parents(
        self, child: SubscriptionScheduledTask, *, scope_request: bool = False
    ) -> None:
        if child.parent_task_id is None:
            return
        parents = (
            await self._session.execute(
                select(SubscriptionScheduledTask)
                .where(
                    SubscriptionScheduledTask.run_id == child.run_id,
                    SubscriptionScheduledTask.task_id == child.parent_task_id,
                    SubscriptionScheduledTask.state == "blocked",
                )
                .with_for_update()
            )
        ).scalars()
        for parent in parents:
            latest = await self._session.scalar(
                select(SubscriptionAttempt.id)
                .where(SubscriptionAttempt.task_row_id == parent.task_id)
                .order_by(SubscriptionAttempt.attempt_number.desc())
                .limit(1)
            )
            result = (
                None
                if latest is None
                else await self._session.get(SubscriptionAttemptResult, latest)
            )
            if result is not None and not (
                result.accepted
                and result.disposition
                in {"delegated", "waiting", "reassigned", "feedback_forwarded"}
            ):
                continue
            selected: tuple[UUID, ...] | None = None
            if result is not None:
                prefix = {
                    "waiting": "wait",
                    "delegated": "delegation",
                    "reassigned": "reassignment",
                    "feedback_forwarded": "feedback-forward",
                }[result.disposition]
                record = await self._session.scalar(
                    select(SubscriptionDecisionRecord).where(
                        SubscriptionDecisionRecord.attempt_id == latest,
                        SubscriptionDecisionRecord.task_row_id == parent.task_id,
                        SubscriptionDecisionRecord.run_id == parent.run_id,
                        SubscriptionDecisionRecord.idempotency_key == f"{prefix}:{latest}",
                    )
                )
                try:
                    if (
                        record is None
                        or canonical_digest(result.result_payload) != result.result_digest
                    ):
                        raise ValueError
                    decision = decode_subscription_record(record.payload)
                    if record.payload != result.result_payload.get("decision"):
                        raise ValueError
                    if result.disposition == "reassigned":
                        receipt = result.application_payload
                        if (
                            not isinstance(decision, BoundReassignDecision)
                            or decision.run_id != parent.run_id
                            or record.record_type != "BoundReassignDecision"
                            or receipt is None
                            or result.application_digest != canonical_digest(receipt)
                            or receipt.get("kind") != "reassignment"
                            or receipt.get("response_result_digest") != result.result_digest
                            or receipt.get("child_task_id") != str(decision.task_id)
                            or receipt.get("source_attempt_id") != str(decision.source_attempt_id)
                            or receipt.get("child_version") != decision.expected_task_version
                        ):
                            raise ValueError
                        selected = (decision.task_id,)
                    elif result.disposition == "feedback_forwarded":
                        receipt = result.application_payload
                        if (
                            not isinstance(decision, ForwardFeedbackDecision)
                            or decision.run_id != parent.run_id
                            or record.record_type != "ForwardFeedbackDecision"
                            or receipt is None
                            or result.application_digest != canonical_digest(receipt)
                            or receipt.get("kind") != "feedback_forwarded"
                            or receipt.get("result_digest") != result.result_digest
                            or receipt.get("target_task_id") != str(decision.task_id)
                            or receipt.get("feedback_receipt_id")
                            != str(decision.feedback_receipt_id)
                            or receipt.get("feedback_digest") != decision.feedback_digest
                        ):
                            raise ValueError
                        selected = (decision.task_id,)
                    elif result.disposition == "waiting":
                        if (
                            not isinstance(decision, WaitDecision)
                            or decision.task_id != parent.task_id
                            or decision.run_id != parent.run_id
                            or record.record_type != "WaitDecision"
                            or len(decision.waiting_on_task_ids) > 64
                        ):
                            raise ValueError
                        selected = decision.waiting_on_task_ids
                    elif (
                        not isinstance(decision, DelegateDecision)
                        or decision.parent_task_id != parent.task_id
                        or decision.run_id != parent.run_id
                        or record.record_type != "DelegateDecision"
                    ):
                        raise ValueError
                except TypeError, ValueError:
                    # A broken wake record must not roll back a child's own
                    # valid settlement or fabricate authority for its parent.
                    continue
            wake_on_completion = scope_request or (
                result is not None and result.disposition == "delegated"
            )
            if selected is not None:
                selected_rows = (
                    await self._session.scalars(
                        select(SubscriptionScheduledTask).where(
                            SubscriptionScheduledTask.run_id == parent.run_id,
                            SubscriptionScheduledTask.parent_task_id == parent.task_id,
                            SubscriptionScheduledTask.task_id.in_(selected),
                        )
                    )
                ).all()
                if len(selected_rows) != len(selected):
                    continue
                wake_on_completion = (
                    child.task_id in selected
                    if scope_request
                    else all(row.state == "terminal" for row in selected_rows)
                )
                if not wake_on_completion:
                    continue
            remaining = (
                None
                if wake_on_completion
                else (
                    await self._session.execute(
                        select(SubscriptionScheduledTask.id).where(
                            SubscriptionScheduledTask.run_id == parent.run_id,
                            SubscriptionScheduledTask.parent_task_id == parent.task_id,
                            SubscriptionScheduledTask.state != "terminal",
                        )
                    )
                ).first()
            )
            if remaining is None:
                parent.state = "queued"
                logical = await self._session.get(
                    SubscriptionTask, parent.task_id, with_for_update=True
                )
                if logical is not None and logical.state == "blocked":
                    logical.state = "queued"
                    logical.version += 1


def _paths_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")
