"""Transactional discovery storage. Caller owns commit and brief transaction."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.epic_brainstorm import (
    _MAX_USAGE,
    AuthoringJobSnapshot,
    AuthoringOutcome,
    AuthoringReceipt,
    BrainstormAmounts,
    BrainstormConflict,
    BrainstormHeldReasons,
    BrainstormMeasuredUsage,
    BrainstormNotFound,
    BrainstormProposal,
    BrainstormReservation,
    BrainstormThread,
    BrainstormTurn,
    duration_floor,
    validate_brainstorm_budget,
    validate_invocation_context,
)
from forge.domain.epic_decomposition import DecompositionProposal
from forge.domain.operation import canonical_digest
from forge.domain.payload import redact_durable_text
from forge.domain.subscription import (
    RouteBinding,
    RouteSpec,
    TaskBudget,
    decode_subscription_record,
    encode_subscription_record,
)
from forge.domain.subscription_quota import QuotaPolicy, QuotaPoolKey
from forge.persistence.models.epic_brainstorm import (
    BrainstormAttemptRow,
    BrainstormAuditRow,
    BrainstormBudgetLedger,
    BrainstormConversation,
    BrainstormJobRow,
    BrainstormQuotaAdmission,
    BrainstormReceiptRow,
    BrainstormTurnRow,
)
from forge.persistence.models.subscription_quota import (
    SubscriptionQuotaObservation,
    SubscriptionQuotaPool,
)


def _parse_proposal(
    payload: object, kind: str
) -> BrainstormProposal | DecompositionProposal | None:
    if payload is None:
        return None
    if kind == "decomposition":
        return DecompositionProposal.model_validate(payload)
    if kind == "brainstorm":
        return BrainstormProposal.model_validate(payload)
    raise BrainstormConflict("unknown authoring job kind")


class PostgresBrainstormRepository:
    def __init__(
        self,
        session: AsyncSession,
        *,
        quota_policy: QuotaPolicy | None = None,
        epic_ceiling: TaskBudget | None = None,
    ) -> None:
        self.session = session
        self.quota_policy = quota_policy or QuotaPolicy()
        self.epic_ceiling = epic_ceiling

    async def quota_pool(self, snapshot: AuthoringJobSnapshot) -> SubscriptionQuotaPool:
        key = self.quota_policy.key_for(snapshot.route.effective)
        await self.session.execute(
            insert(SubscriptionQuotaPool)
            .values(
                provider=key.provider, account=key.account, pool=key.pool, revision=0, blocked=False
            )
            .on_conflict_do_nothing()
        )
        row = await self.session.get(
            SubscriptionQuotaPool,
            (key.provider, key.account, key.pool),
            with_for_update=True,
            populate_existing=True,
        )
        assert row is not None
        return row

    async def quota_settle(
        self,
        row: BrainstormJobRow,
        attempt: BrainstormAttemptRow,
        *,
        exhausted: bool,
        reset_at: datetime | None,
        succeeded: bool = False,
    ) -> None:
        if row.current_attempt_id != attempt.id:
            raise BrainstormConflict("quota result attempt authority revoked")
        snapshot = self.decode_snapshot(row)
        admission = await self.session.get(BrainstormQuotaAdmission, attempt.id)
        if admission is None:
            raise BrainstormConflict("quota admission identity conflicts")
        try:
            key = QuotaPoolKey(admission.provider, admission.account, admission.pool)
        except ValueError as error:
            raise BrainstormConflict("quota admission identity conflicts") from error
        if key.provider != snapshot.route.effective.provider:
            raise BrainstormConflict("quota admission identity conflicts")
        # Pool precedes admission in the lock order used by claim. The admission
        # identity is immutable; recheck it under lock before applying settlement.
        pool = await self.session.get(
            SubscriptionQuotaPool,
            (key.provider, key.account, key.pool),
            with_for_update=True,
            populate_existing=True,
        )
        admission = await self.session.get(
            BrainstormQuotaAdmission,
            attempt.id,
            with_for_update=True,
            populate_existing=True,
        )
        if (
            pool is None
            or admission is None
            or (admission.provider, admission.account, admission.pool)
            != (key.provider, key.account, key.pool)
        ):
            raise BrainstormConflict("quota admission identity conflicts")
        now = datetime.now(UTC)
        existing_observation = (
            await self.session.scalar(
                select(SubscriptionQuotaObservation.id).where(
                    SubscriptionQuotaObservation.source_attempt_id == attempt.id
                )
            )
            if exhausted
            else None
        )
        if exhausted and existing_observation is None and admission.finished_at is None:
            eligible = reset_at or now + timedelta(
                seconds=self.quota_policy.unknown_reset_cooldown_seconds
            )
            basis = "known_reset" if reset_at else "probe_cooldown"
            evidence = {
                "schema_version": 1,
                "attempt_id": str(attempt.id),
                "reset_at": reset_at.isoformat() if reset_at else None,
            }
            digest = canonical_digest(evidence)
            self.session.add(
                SubscriptionQuotaObservation(
                    id=uuid4(),
                    provider=pool.provider,
                    account=pool.account,
                    pool=pool.pool,
                    source_key=digest,
                    source_attempt_id=attempt.id,
                    actor_id=None,
                    observed_at=now,
                    reason="usage_exhausted",
                    reset_at=reset_at,
                    next_eligible_at=eligible,
                    retry_basis=basis,
                    evidence_digest=digest,
                )
            )
            if pool.observed_at is None or pool.observed_at <= now:
                if (
                    not pool.blocked
                    or pool.next_eligible_at is None
                    or eligible > pool.next_eligible_at
                ):
                    pool.next_eligible_at, pool.reset_at, pool.retry_basis = (
                        eligible,
                        reset_at,
                        basis,
                    )
                pool.blocked, pool.observed_at, pool.reason, pool.recovered_at = (
                    True,
                    now,
                    "usage_exhausted",
                    None,
                )
                pool.revision += 1
        if not attempt.process_settled or admission.finished_at is not None:
            return
        admission.finished_at = now
        if (
            not exhausted
            and admission.probe
            and pool.probe_attempt_id == attempt.id
            and pool.revision == admission.revision
        ):
            if succeeded:
                pool.blocked, pool.recovered_at, pool.reason = False, now, None
                pool.next_eligible_at, pool.reset_at, pool.retry_basis = None, None, None
            else:
                pool.blocked, pool.next_eligible_at, pool.reset_at, pool.retry_basis = (
                    True,
                    now + timedelta(seconds=self.quota_policy.unknown_reset_cooldown_seconds),
                    None,
                    "probe_cooldown",
                )
            pool.revision += 1
        if pool.probe_attempt_id == attempt.id:
            pool.probe_attempt_id = None
        waiting = (
            await self.session.scalars(
                select(BrainstormJobRow.id)
                .where(
                    BrainstormJobRow.epic_id == row.epic_id,
                    BrainstormJobRow.state == "capacity_wait",
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
        if waiting:
            await self.session.execute(
                update(BrainstormJobRow)
                .where(BrainstormJobRow.id.in_(waiting))
                .values(
                    state="queued",
                    next_eligible_at=None,
                    failure=None,
                    version=BrainstormJobRow.version + 1,
                )
            )
        await self.session.flush()

    async def _epic_attempts(self, epic_id: UUID) -> list[BrainstormAttemptRow]:
        return list(
            (
                await self.session.scalars(
                    select(BrainstormAttemptRow)
                    .join(BrainstormJobRow, BrainstormAttemptRow.job_id == BrainstormJobRow.id)
                    .where(BrainstormJobRow.epic_id == epic_id)
                )
            ).all()
        )

    @staticmethod
    def _bounded_measurement(value: object) -> int | None:
        return value if type(value) is int and 0 <= value <= _MAX_USAGE else None

    @staticmethod
    def _unsafe_usage(usage: Mapping[str, object]) -> bool:
        return bool(usage.get("tool_call_count_unknown")) or any(
            PostgresBrainstormRepository._bounded_measurement(value) is None
            for name in (
                "duration_ms",
                "duration_lower_bound_ms",
                "tool_call_count",
                "input_tokens",
                "output_tokens",
                "estimated_api_cost_minor",
            )
            if (value := usage.get(name)) is not None
        )

    @staticmethod
    def _charges(
        attempts: list[BrainstormAttemptRow],
    ) -> tuple[dict[str, int], dict[str, int], int]:
        charged = {
            key: 0
            for key in (
                "duration_ms",
                "tool_call_count",
                "input_tokens",
                "output_tokens",
                "estimated_api_cost_minor",
            )
        }
        held = dict.fromkeys(charged, 0)
        unknown = 0
        for attempt in attempts:
            usage = attempt.usage or {}
            reservation = attempt.reservation if isinstance(attempt.reservation, Mapping) else {}
            if attempt.usage_known is False or PostgresBrainstormRepository._unsafe_usage(usage):
                unknown += 1
            for key in charged:
                value = PostgresBrainstormRepository._bounded_measurement(
                    usage.get(key) if attempt.process_settled else None
                )
                reserve = reservation.get(key)
                if key == "duration_ms":
                    lower = duration_floor(usage)
                    if type(value) is int and value >= lower:
                        charged[key] += value
                    else:
                        charged[key] += lower
                        if type(reserve) is int:
                            held[key] += max(reserve - lower, 0)
                    continue
                if key == "tool_call_count" and type(value) is not int:
                    value = attempt.tool_calls_used
                if type(value) is int and value >= 0:
                    charged[key] += value
                    if (
                        not attempt.process_settled
                        or (key == "tool_call_count" and usage.get("tool_call_count_unknown"))
                        or (
                            key == "estimated_api_cost_minor"
                            and (
                                attempt.usage_known is not True
                                or (usage.get("currency") is None and value > 0)
                            )
                        )
                    ) and type(reserve) is int:
                        held[key] += max(reserve - value, 0)
                elif type(reserve) is int:
                    held[key] += reserve
        return charged, held, unknown

    @staticmethod
    def _currency(value: object) -> str | None:
        if (
            isinstance(value, str)
            and len(value) == 3
            and value.isascii()
            and value.isupper()
            and value.isalpha()
        ):
            return value
        return None

    async def _attempt_routes(self, attempts: list[BrainstormAttemptRow]) -> dict[UUID, RouteSpec]:
        job_ids = {attempt.job_id for attempt in attempts if hasattr(attempt, "job_id")}
        if not job_ids:
            return {}
        rows = await self.session.execute(
            select(BrainstormJobRow.id, BrainstormJobRow.snapshot).where(
                BrainstormJobRow.id.in_(job_ids)
            )
        )
        routes: dict[UUID, RouteSpec] = {}
        for job_id, payload in rows:
            try:
                binding = decode_subscription_record(cast(Mapping[str, object], payload["route"]))
                if isinstance(binding, RouteBinding):
                    routes[job_id] = binding.effective
            except KeyError, TypeError, ValueError, ValidationError:
                # A malformed legacy snapshot cannot establish a monetary unit.
                continue
        return routes

    async def _attempt_scopes(
        self, attempts: list[BrainstormAttemptRow]
    ) -> dict[UUID, tuple[RouteSpec, QuotaPoolKey]]:
        routes = await self._attempt_routes(attempts)
        if not attempts:
            return {}
        admissions = (
            await self.session.scalars(
                select(BrainstormQuotaAdmission).where(
                    BrainstormQuotaAdmission.attempt_id.in_([attempt.id for attempt in attempts])
                )
            )
        ).all()
        by_attempt = {admission.attempt_id: admission for admission in admissions}
        scopes: dict[UUID, tuple[RouteSpec, QuotaPoolKey]] = {}
        for attempt in attempts:
            route = routes.get(attempt.job_id)
            admission = by_attempt.get(attempt.id)
            if route is None or admission is None:
                continue
            try:
                key = QuotaPoolKey(admission.provider, admission.account, admission.pool)
            except ValueError:
                continue
            if key.provider == route.provider:
                scopes[attempt.id] = route, key
        return scopes

    @classmethod
    def _money_evidence(
        cls,
        attempts: list[BrainstormAttemptRow],
        scopes: Mapping[UUID, tuple[RouteSpec, QuotaPoolKey]],
    ) -> tuple[
        str | None,
        bool,
        set[tuple[RouteSpec, QuotaPoolKey]],
        set[tuple[RouteSpec, QuotaPoolKey]],
        bool,
        bool,
    ]:
        units: set[str] = set()
        reported_units: set[str] = set()
        proved_routes: dict[tuple[RouteSpec, QuotaPoolKey], set[str]] = {}
        held_routes: set[tuple[RouteSpec, QuotaPoolKey]] = set()
        has_money = False
        unsafe = False
        unknown_hold = False
        for attempt in attempts:
            usage = attempt.usage or {}
            _, held, _ = cls._charges([attempt])
            scope = scopes.get(attempt.id)
            raw_cost = usage.get("estimated_api_cost_minor")
            if raw_cost is not None and cls._bounded_measurement(raw_cost) is None:
                unsafe = True
            observed = cls._bounded_measurement(raw_cost)
            reservation = attempt.reservation
            if reservation is not None and not isinstance(reservation, Mapping):
                unsafe = True
            raw_reserve = (
                reservation.get("estimated_api_cost_minor")
                if isinstance(reservation, Mapping)
                else None
            )
            if raw_reserve is not None and cls._bounded_measurement(raw_reserve) is None:
                unsafe = True
            # Only a trusted terminal measurement discharges uncertainty. An active
            # zero/absent amount still has monetary exposure, even without a finite hold.
            exposed = not (
                attempt.process_settled and attempt.usage_known is True and observed is not None
            )
            if exposed and raw_reserve is None:
                unknown_hold = True
            if (observed is not None and observed > 0) or exposed:
                has_money = True
                raw_currency = usage.get("currency")
                currency = cls._currency(raw_currency)
                if (raw_currency is not None and currency is None) or (
                    observed is not None and observed > 0 and currency is None
                ):
                    unsafe = True
                if currency is not None:
                    reported_units.add(currency)
                    if observed is not None and observed > 0:
                        units.add(currency)
                        if scope is not None:
                            proved_routes.setdefault(scope, set()).add(currency)
                        else:
                            unsafe = True
            if exposed or held["estimated_api_cost_minor"] > 0:
                if scope is None:
                    unsafe = True
                else:
                    held_routes.add(scope)
        unit = next(iter(units)) if len(units) == 1 else None
        compatible = {route for route, values in proved_routes.items() if values == {unit}}
        if len(reported_units) > 1 or (unit is not None and not held_routes <= compatible):
            unsafe = True
        return unit, unsafe, compatible, held_routes, has_money, unknown_hold

    async def _reservation(
        self,
        row: BrainstormJobRow,
        snapshot: AuthoringJobSnapshot,
        pool: SubscriptionQuotaPool,
        ledger: BrainstormBudgetLedger,
        next_number: int,
    ) -> tuple[dict[str, int | None] | None, bool]:
        validate_brainstorm_budget(snapshot.budget)
        try:
            budget = decode_subscription_record(ledger.ceiling)
        except (TypeError, ValueError) as error:
            raise BrainstormConflict("epic budget ceiling is invalid") from error
        if not isinstance(budget, TaskBudget):
            raise BrainstormConflict("epic budget ceiling is invalid")
        disabled = frozenset(ledger.disabled_dimensions)
        attempts = await self._epic_attempts(row.epic_id)
        charged, held, unknown = self._charges(attempts)
        from forge.persistence.repositories.epic_budget import PostgresEpicBudgetRepository

        aggregate = await PostgresEpicBudgetRepository(
            self.session, legacy_hold=self.epic_ceiling or snapshot.budget
        ).totals(row.epic_id)
        charged, held = aggregate.known, aggregate.held
        aggregate_unknown = aggregate.shared_unknown and not row.override_unknown_usage
        if attempts:
            scopes = await self._attempt_scopes(attempts)
            unit, unsafe_money, compatible, pending, has_money, unknown_hold = self._money_evidence(
                attempts, scopes
            )
            try:
                candidate_key = QuotaPoolKey(pool.provider, pool.account, pool.pool)
            except ValueError as error:
                raise BrainstormConflict("epic discovery quota scope is invalid") from error
            candidate = snapshot.route.effective, candidate_key
            if candidate_key.provider != candidate[0].provider:
                raise BrainstormConflict("epic discovery quota scope is invalid")
            if not row.override_unknown_usage and (
                unsafe_money
                or (
                    unit is not None
                    and aggregate.currency is not None
                    and unit != aggregate.currency
                )
                or (
                    unknown_hold
                    and budget.max_cost_minor is not None
                    and "estimated_api_cost_minor" not in disabled
                )
                or (
                    has_money
                    and budget.max_cost_minor != 0
                    and "estimated_api_cost_minor" not in disabled
                    and (
                        candidate not in compatible if unit is not None else pending != {candidate}
                    )
                )
            ):
                return None, any(not attempt.process_settled for attempt in attempts)
        owner_retry = (
            row.retry_authorized_until is not None and next_number <= row.retry_authorized_until
        )
        if (
            charged["provider_attempts"] + held["provider_attempts"] >= budget.max_provider_attempts
            and "provider_attempts" not in disabled
            and not owner_retry
        ) or (
            unknown > 0
            and unknown >= budget.unknown_telemetry_policy.max_uncertain_attempts
            and not row.override_unknown_usage
        ):
            return None, False
        limits = {
            "duration_ms": None
            if "duration_ms" in disabled
            else budget.max_duration_seconds * 1000,
            "tool_call_count": None if "tool_call_count" in disabled else budget.max_tool_calls,
            "input_tokens": None if "input_tokens" in disabled else budget.max_input_tokens,
            "output_tokens": None if "output_tokens" in disabled else budget.max_output_tokens,
            "estimated_api_cost_minor": None
            if "estimated_api_cost_minor" in disabled
            else budget.max_cost_minor,
        }
        remaining = {
            key: None if limit is None else limit - charged[key] - held[key]
            for key, limit in limits.items()
        }
        blocked = {
            key
            for key, value in remaining.items()
            if value is not None and value < (1000 if key == "duration_ms" else 1)
        }
        if blocked and not owner_retry:
            active_held = dict.fromkeys(charged, 0)
            for attempt in attempts:
                if attempt.process_settled:
                    continue
                for key in active_held:
                    reserved = attempt.reservation.get(key)
                    if type(reserved) is int:
                        consumed = attempt.tool_calls_used if key == "tool_call_count" else 0
                        active_held[key] += max(reserved - consumed, 0)
            waitable = all(
                active_held[key] > 0
                and cast(int, limits[key]) - charged[key] - (held[key] - active_held[key])
                >= (1000 if key == "duration_ms" else 1)
                for key in blocked
            )
            return None, waitable
        if aggregate_unknown:
            return None, any(not attempt.process_settled for attempt in attempts)
        # The shared epic ceiling grants capacity, while the frozen job budget
        # still caps one provider invocation independently.
        attempt_limits = {
            "duration_ms": snapshot.budget.max_duration_seconds * 1000,
            "tool_call_count": snapshot.budget.max_tool_calls,
            "input_tokens": snapshot.budget.max_input_tokens,
            "output_tokens": snapshot.budget.max_output_tokens,
            "estimated_api_cost_minor": snapshot.budget.max_cost_minor,
        }

        def narrower(value: int | None, local: int | None) -> int | None:
            if value is None:
                return local
            if local is None:
                return value
            return min(value, local)

        return {
            key: attempt_limits[key]
            if owner_retry and key in blocked
            else narrower(value, attempt_limits[key])
            for key, value in remaining.items()
        }, False

    async def _lock_epic_ledger(
        self, row: BrainstormJobRow, ceiling: dict[str, object]
    ) -> BrainstormBudgetLedger:
        await self.session.execute(
            insert(BrainstormBudgetLedger)
            .values(epic_id=row.epic_id, project_id=row.project_id, ceiling=ceiling)
            .on_conflict_do_nothing()
        )
        ledger = await self.session.get(BrainstormBudgetLedger, row.epic_id, with_for_update=True)
        if ledger is None or ledger.project_id != row.project_id:
            raise BrainstormConflict("epic budget ledger conflicts with project")
        return ledger

    async def conversation(
        self, epic_id: UUID, project_id: UUID, conversation_id: UUID, *, lock: bool = False
    ) -> BrainstormConversation:
        query = select(BrainstormConversation).where(BrainstormConversation.id == conversation_id)
        if lock:
            query = query.with_for_update()
        row = await self.session.scalar(query)
        if row is None or row.epic_id != epic_id or row.project_id != project_id:
            raise BrainstormNotFound("conversation is not under this epic and project")
        return row

    async def job(
        self, epic_id: UUID, project_id: UUID, job_id: UUID, *, lock: bool = False
    ) -> BrainstormJobRow:
        query = select(BrainstormJobRow).where(BrainstormJobRow.id == job_id)
        if lock:
            query = query.with_for_update()
        row = await self.session.scalar(query)
        if row is None or row.epic_id != epic_id or row.project_id != project_id:
            raise BrainstormNotFound("job is not under this epic and project")
        return row

    async def turn(self, conversation_id: UUID, turn_id: UUID) -> BrainstormTurnRow:
        row = await self.session.get(BrainstormTurnRow, turn_id)
        if row is None or row.conversation_id != conversation_id:
            raise BrainstormNotFound("turn is not under this conversation")
        return row

    async def turns(self, conversation_id: UUID) -> tuple[BrainstormTurn, ...]:
        rows = (
            await self.session.scalars(
                select(BrainstormTurnRow)
                .where(BrainstormTurnRow.conversation_id == conversation_id)
                .order_by(BrainstormTurnRow.ordinal)
            )
        ).all()
        proposals = (
            await self.session.scalars(
                select(BrainstormJobRow).where(
                    BrainstormJobRow.conversation_id == conversation_id,
                    BrainstormJobRow.proposal.is_not(None),
                )
            )
        ).all()
        by_turn = {
            proposal.turn_id: proposal
            for job in proposals
            for proposal in (_parse_proposal(job.proposal, self.decode_snapshot(job).kind),)
            if proposal is not None
        }
        return tuple(
            BrainstormTurn.model_validate(
                {
                    "turn_id": row.id,
                    "conversation_id": row.conversation_id,
                    "role": row.role,
                    "text": row.text,
                    "pending": row.pending,
                    "proposal": by_turn.get(row.id),
                }
            )
            for row in rows
        )

    async def frozen_history(
        self, snapshot: AuthoringJobSnapshot, *, conversation_id: UUID
    ) -> tuple[BrainstormTurn, ...]:
        history = (await self.turns(conversation_id))[: snapshot.conversation_version - 1]
        if not history or history[-1].turn_id != snapshot.prompt_turn_id:
            raise BrainstormConflict("frozen prompt history conflicts")
        validate_invocation_context(snapshot, history)
        return history

    async def threads(self, epic_id: UUID, project_id: UUID) -> tuple[BrainstormThread, ...]:
        conversations = (
            await self.session.scalars(
                select(BrainstormConversation)
                .where(
                    BrainstormConversation.epic_id == epic_id,
                    BrainstormConversation.project_id == project_id,
                )
                .order_by(BrainstormConversation.created_at, BrainstormConversation.id)
            )
        ).all()
        jobs = (
            await self.session.scalars(
                select(BrainstormJobRow)
                .where(
                    BrainstormJobRow.epic_id == epic_id, BrainstormJobRow.project_id == project_id
                )
                .order_by(BrainstormJobRow.created_at, BrainstormJobRow.id)
            )
        ).all()
        by_conversation: dict[UUID, list[UUID]] = {row.id: [] for row in conversations}
        for job in jobs:
            by_conversation[job.conversation_id].append(job.id)
        return tuple(
            BrainstormThread(
                conversation_id=row.id,
                conversation_version=row.version,
                job_ids=tuple(by_conversation[row.id]),
            )
            for row in conversations
        )

    async def append(self, conversation: BrainstormConversation, turn: BrainstormTurn) -> int:
        if turn.conversation_id != conversation.id:
            raise BrainstormConflict("turn conversation conflicts")
        ordinal = conversation.version
        self.session.add(
            BrainstormTurnRow(
                id=turn.turn_id,
                conversation_id=conversation.id,
                ordinal=ordinal,
                role=turn.role,
                text=turn.text,
                pending=turn.pending,
            )
        )
        conversation.version += 1
        await self.session.flush()
        return conversation.version

    @staticmethod
    def snapshot_payload(snapshot: AuthoringJobSnapshot) -> dict[str, object]:
        return {
            "schema_version": 1,
            "job_id": str(snapshot.job_id),
            "epic_id": str(snapshot.epic_id),
            "project_id": str(snapshot.project_id),
            "conversation_id": str(snapshot.conversation_id),
            "kind": snapshot.kind,
            "input_brief_revision_id": str(snapshot.input_brief_revision_id)
            if snapshot.input_brief_revision_id
            else None,
            "input_brief_digest": snapshot.input_brief_digest,
            "input_draft_digest": snapshot.input_draft_digest,
            "draft_content": snapshot.draft_content.model_dump(mode="json"),
            "accepted_content": snapshot.accepted_content.model_dump(mode="json")
            if snapshot.accepted_content
            else None,
            "input_graph_revision_id": str(snapshot.input_graph_revision_id)
            if snapshot.input_graph_revision_id
            else None,
            "input_graph_digest": snapshot.input_graph_digest,
            "expected_epic_version": snapshot.expected_epic_version,
            "conversation_version": snapshot.conversation_version,
            "prompt_turn_id": str(snapshot.prompt_turn_id),
            "profile_id": str(snapshot.profile_id) if snapshot.profile_id else None,
            "profile_version": snapshot.profile_version,
            "route": encode_subscription_record(snapshot.route),
            "budget": encode_subscription_record(snapshot.budget),
            "reservation_id": str(snapshot.reservation_id),
        }

    @staticmethod
    def decode_snapshot(row: BrainstormJobRow) -> AuthoringJobSnapshot:
        value = dict(row.snapshot)
        value["route"] = decode_subscription_record(cast(Mapping[str, object], value["route"]))
        value["budget"] = decode_subscription_record(cast(Mapping[str, object], value["budget"]))
        return AuthoringJobSnapshot.model_validate(value)

    @staticmethod
    def receipt(row: BrainstormJobRow, key: str) -> AuthoringReceipt:
        PostgresBrainstormRepository._valid_key(key)
        return AuthoringReceipt.model_validate(
            {"job_id": row.id, "job_version": row.version, "state": row.state, "replay_key": key}
        )

    async def outcome(self, row: BrainstormJobRow) -> AuthoringOutcome:
        attempt = (
            await self.session.get(BrainstormAttemptRow, row.current_attempt_id)
            if row.current_attempt_id
            else None
        )
        attempts = await self._epic_attempts(row.epic_id)
        charged, held, unknown = self._charges(attempts)
        scopes = await self._attempt_scopes(attempts)
        unit, unsafe_money, _, _, has_money, unknown_hold = self._money_evidence(attempts, scopes)
        suppress_money = unsafe_money or unknown_hold or (has_money and unit is None)
        unknown_dimensions = {
            key
            for recorded in attempts
            for key in charged
            if not recorded.process_settled
            or self._bounded_measurement((recorded.usage or {}).get(key)) is None
            or (key == "tool_call_count" and (recorded.usage or {}).get("tool_call_count_unknown"))
            or (
                key == "estimated_api_cost_minor"
                and (
                    recorded.usage_known is not True
                    or (
                        (recorded.usage or {}).get("currency") is None
                        and (recorded.usage or {}).get(key) != 0
                    )
                )
            )
        }
        if suppress_money:
            unknown_dimensions.add("estimated_api_cost_minor")
        raw_usage = attempt.usage if attempt else None
        usage = None
        if raw_usage is not None and attempt is not None:
            recorded_tools = raw_usage.get("tool_call_count", 0)
            recorded_duration = raw_usage.get("duration_ms")
            lower = duration_floor(raw_usage)
            projected = {
                "duration_ms": recorded_duration
                if type(recorded_duration) is int
                and recorded_duration >= lower
                and recorded_duration <= _MAX_USAGE
                and (attempt.process_settled or not attempt.launch_intent)
                else None,
                "duration_lower_bound_ms": lower,
                "tool_call_count": max(
                    attempt.tool_calls_used, self._bounded_measurement(recorded_tools) or 0
                ),
                "input_tokens": self._bounded_measurement(raw_usage.get("input_tokens")),
                "output_tokens": self._bounded_measurement(raw_usage.get("output_tokens")),
                "estimated_api_cost_minor": self._bounded_measurement(
                    raw_usage.get("estimated_api_cost_minor")
                )
                if not suppress_money
                else None,
            }
            missing = tuple(
                key
                for key in (
                    "duration_ms",
                    "input_tokens",
                    "output_tokens",
                    "estimated_api_cost_minor",
                )
                if projected[key] is None
            )
            usage = BrainstormMeasuredUsage.model_validate({**projected, "unknown_fields": missing})
        safe_currency = unit if not suppress_money else None
        reservation = None
        if attempt is not None:
            try:
                reservation = BrainstormReservation.model_validate(attempt.reservation)
            except ValidationError:
                pass
        overflow = {key for key in charged if charged[key] > _MAX_USAGE or held[key] > _MAX_USAGE}
        public_charged, public_held = dict(charged), dict(held)
        if suppress_money:
            public_charged["estimated_api_cost_minor"] = 0
            public_held["estimated_api_cost_minor"] = 0
        invalid_money = any(
            value is not None and self._bounded_measurement(value) is None
            for recorded in attempts
            for value in (
                (recorded.usage or {}).get("estimated_api_cost_minor"),
                (recorded.reservation or {}).get("estimated_api_cost_minor")
                if isinstance(recorded.reservation, Mapping)
                else None,
            )
        )
        current_cost = (raw_usage or {}).get("estimated_api_cost_minor")
        current_money_conflict = invalid_money or (
            suppress_money
            and (current_cost is not None or (raw_usage or {}).get("currency") is not None)
            and not (
                attempt is not None
                and attempt.process_settled
                and attempt.usage_known is True
                and current_cost == 0
            )
        )
        return AuthoringOutcome.model_validate(
            {
                "job_id": row.id,
                "job_version": row.version,
                "state": row.state,
                "proposal_digest": row.proposal_digest,
                "proposal": (
                    _parse_proposal(row.proposal, self.decode_snapshot(row).kind)
                    if row.proposal
                    else None
                ),
                "adopted_revision_id": row.adopted_revision_id,
                "failure": row.failure,
                "usage_known": attempt.usage_known
                and not self._unsafe_usage(raw_usage or {})
                and not current_money_conflict
                if attempt
                else None,
                # No admitted attempt means there is no provider process to
                # settle. A first admission refusal is therefore recoverable.
                "process_settled": attempt.process_settled if attempt else True,
                "usage": usage,
                "reservation": reservation,
                # A saturated public amount is a lower bound, never used for admission.
                # held_reasons marks its dimension unknown; raw arithmetic stays exact.
                "cumulative_usage": BrainstormAmounts.model_validate(
                    {key: min(value, _MAX_USAGE) for key, value in public_charged.items()}
                ),
                "held_reservations": BrainstormAmounts.model_validate(
                    {key: min(value, _MAX_USAGE) for key, value in public_held.items()}
                ),
                "uncertain_attempts": unknown,
                "currency": safe_currency,
                "unknown_usage_fields": usage.unknown_fields if usage else (),
                "held_reasons": BrainstormHeldReasons.model_validate(
                    {
                        key: "unsettled_or_unknown"
                        for key, value in held.items()
                        if value or key in overflow or key in unknown_dimensions
                    }
                ),
            }
        )

    async def lock_command(self, epic_id: UUID, key: str) -> None:
        """Serialize one idempotency key before reading receipts or command state."""
        self._valid_key(key)
        identity = sha256(epic_id.bytes + key.encode("utf-8")).digest()
        await self.session.execute(
            select(func.pg_advisory_xact_lock(int.from_bytes(identity[:8], "big", signed=True)))
        )

    async def replay(self, epic_id: UUID, key: str, digest: str) -> dict[str, object] | None:
        self._valid_key(key)
        row = await self.session.scalar(
            select(BrainstormReceiptRow).where(
                BrainstormReceiptRow.epic_id == epic_id,
                BrainstormReceiptRow.key == self._key_digest(key),
            )
        )
        if row is None:
            return None
        if row.request_digest != digest:
            raise BrainstormConflict("idempotency key payload conflicts")
        response = dict(row.response)
        if response.pop("_authoring_receipt", False):
            response["replay_key"] = key
        return response

    async def save_receipt(
        self, epic_id: UUID, key: str, digest: str, response: dict[str, object]
    ) -> None:
        self._valid_key(key)
        stored = dict(response)
        if "replay_key" in stored:
            stored.pop("replay_key")
            stored["_authoring_receipt"] = True
        self.session.add(
            BrainstormReceiptRow(
                id=uuid4(),
                epic_id=epic_id,
                key=self._key_digest(key),
                request_digest=digest,
                response=stored,
            )
        )
        await self.session.flush()

    @staticmethod
    def _key_digest(key: str) -> str:
        return sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def _valid_key(key: str) -> None:
        # Reject recognized credential forms even though all durable keys are hashed.
        if not isinstance(key, str) or not key or len(key) > 255 or redact_durable_text(key) != key:
            raise ValueError("invalid idempotency key")

    async def audit(
        self,
        epic_id: UUID,
        actor_id: UUID | None,
        action: str,
        subject_id: UUID,
        detail: dict[str, object],
    ) -> None:
        self.session.add(
            BrainstormAuditRow(
                id=uuid4(),
                epic_id=epic_id,
                actor_id=actor_id,
                action=action,
                subject_id=subject_id,
                detail=detail,
            )
        )
        await self.session.flush()

    async def claim(
        self,
        owner: str,
        lease_seconds: int = 30,
        *,
        kinds: frozenset[str] = frozenset(("brainstorm",)),
    ) -> tuple[BrainstormJobRow, BrainstormAttemptRow] | None:
        now = datetime.now(UTC)
        seen: list[UUID] = []
        resource_scope: tuple[UUID, str, str, str] | None = None
        while len(seen) < 50:
            query = (
                select(BrainstormJobRow)
                .where(
                    BrainstormJobRow.state.in_(("queued", "quota_wait", "capacity_wait")),
                    (
                        BrainstormJobRow.next_eligible_at.is_(None)
                        | (BrainstormJobRow.next_eligible_at <= now)
                    ),
                    BrainstormJobRow.id.not_in(seen),
                    BrainstormJobRow.snapshot["kind"].astext.in_(kinds),
                )
                .order_by(BrainstormJobRow.created_at, BrainstormJobRow.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            row = await self.session.scalar(query)
            if row is None:
                return None
            seen.append(row.id)
            previous = (
                await self.session.get(BrainstormAttemptRow, row.current_attempt_id)
                if row.current_attempt_id
                else None
            )
            if row.current_attempt_id and (previous is None or not previous.process_settled):
                row.state = "reconciling"
                row.version += 1
                await self.session.flush()
                continue
            try:
                snapshot = self.decode_snapshot(row)
                validate_brainstorm_budget(snapshot.budget)
                await self.frozen_history(snapshot, conversation_id=row.conversation_id)
                key = self.quota_policy.key_for(snapshot.route.effective)
            except BrainstormConflict, ValueError:
                row.state, row.failure = "failed", "input_conflict"
                row.version += 1
                await self.session.flush()
                continue
            candidate_scope = (row.epic_id, key.provider, key.account, key.pool)
            if resource_scope is not None and candidate_scope != resource_scope:
                return None
            resource_scope = candidate_scope
            number = previous.number + 1 if previous else 1
            attempt = BrainstormAttemptRow(
                id=uuid4(),
                job_id=row.id,
                number=number,
                state="admitted",
                owner=owner,
                fence=uuid4(),
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                launch_intent=False,
                process_started=False,
                process_settled=False,
            )
            # Claims retain the ledger before quota. Delivery prepares the same
            # ledger before it reaches a pool, so neither path can invert them.
            ledger = await self._lock_epic_ledger(
                row, encode_subscription_record(self.epic_ceiling or snapshot.budget)
            )
            pool = await self.quota_pool(snapshot)
            if pool.blocked and (
                pool.probe_attempt_id is not None
                or pool.next_eligible_at is None
                or pool.next_eligible_at > now
            ):
                eligible = (
                    max(pool.next_eligible_at or now, now + timedelta(seconds=5))
                    if pool.probe_attempt_id is not None
                    else pool.next_eligible_at or now + timedelta(minutes=5)
                )
                if row.state != "quota_wait" or row.wait_pool_revision != pool.revision:
                    row.state, row.next_eligible_at = "quota_wait", eligible
                    row.wait_pool_revision = pool.revision
                    row.version += 1
                else:
                    row.next_eligible_at = eligible
                await self.session.flush()
                continue
            try:
                reservation, waitable = await self._reservation(row, snapshot, pool, ledger, number)
            except BrainstormConflict:
                row.state, row.failure = "failed", "input_conflict"
                row.version += 1
                await self.session.flush()
                continue
            if reservation is None:
                if waitable:
                    if row.state != "capacity_wait":
                        row.state, row.failure = "capacity_wait", None
                        row.version += 1
                    row.next_eligible_at = now + timedelta(seconds=5)
                else:
                    row.state, row.failure = "failed", "budget_exhausted"
                    row.version += 1
                await self.session.flush()
                continue
            attempt.reservation = cast(dict[str, object], reservation)
            self.session.add(attempt)
            self.session.add(
                BrainstormQuotaAdmission(
                    attempt_id=attempt.id,
                    provider=pool.provider,
                    account=pool.account,
                    pool=pool.pool,
                    revision=pool.revision,
                    probe=pool.blocked,
                )
            )
            if pool.blocked:
                pool.probe_attempt_id = attempt.id
            row.current_attempt_id = attempt.id
            row.state, row.next_eligible_at = "running", None
            # An owner retry grants one concrete next admission, not a sticky
            # waiver for later invocations or independently queued work.
            row.retry_authorized_until = None
            row.override_unknown_usage = False
            row.wait_pool_revision = None
            row.version += 1
            await self.session.flush()
            await self.audit(
                row.epic_id,
                None,
                "attempt_admitted",
                attempt.id,
                {"job_id": str(row.id), "number": number},
            )
            return row, attempt
        return None
