"""Transactional discovery storage. Caller owns commit and brief transaction."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.epic_brainstorm import (
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
    validate_invocation_context,
)
from forge.domain.operation import canonical_digest
from forge.domain.subscription import decode_subscription_record, encode_subscription_record
from forge.domain.subscription_quota import QuotaPolicy
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


class PostgresBrainstormRepository:
    def __init__(self, session: AsyncSession, *, quota_policy: QuotaPolicy | None = None) -> None:
        self.session = session
        self.quota_policy = quota_policy or QuotaPolicy()

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
        pool = await self.quota_pool(snapshot)
        admission = await self.session.get(
            BrainstormQuotaAdmission, attempt.id, with_for_update=True
        )
        if admission is None or (pool.provider, pool.account, pool.pool) != (
            admission.provider,
            admission.account,
            admission.pool,
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
            if attempt.usage_known is False:
                unknown += 1
            usage = attempt.usage or {}
            reservation = attempt.reservation or {}
            for key in charged:
                value = usage.get(key) if attempt.process_settled else None
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
                        or (key == "estimated_api_cost_minor" and usage.get("currency") is None)
                    ) and type(reserve) is int:
                        held[key] += max(reserve - value, 0)
                elif type(reserve) is int:
                    held[key] += reserve
        return charged, held, unknown

    async def _reservation(
        self, row: BrainstormJobRow, snapshot: AuthoringJobSnapshot
    ) -> tuple[dict[str, int | None] | None, bool]:
        ceiling = encode_subscription_record(snapshot.budget)
        await self.session.execute(
            insert(BrainstormBudgetLedger)
            .values(epic_id=row.epic_id, project_id=row.project_id, ceiling=ceiling)
            .on_conflict_do_nothing()
        )
        ledger = await self.session.get(BrainstormBudgetLedger, row.epic_id, with_for_update=True)
        if ledger is None or ledger.project_id != row.project_id or ledger.ceiling != ceiling:
            raise BrainstormConflict("epic discovery ceiling changed")
        attempts = await self._epic_attempts(row.epic_id)
        charged, held, unknown = self._charges(attempts)
        budget = snapshot.budget
        if len(attempts) >= budget.max_provider_attempts or (
            unknown > 0 and unknown >= budget.unknown_telemetry_policy.max_uncertain_attempts
        ):
            return None, False
        limits = {
            "duration_ms": budget.max_duration_seconds * 1000,
            "tool_call_count": budget.max_tool_calls,
            "input_tokens": budget.max_input_tokens,
            "output_tokens": budget.max_output_tokens,
            "estimated_api_cost_minor": budget.max_cost_minor,
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
        if blocked:
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
        return remaining, False

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
                select(BrainstormJobRow.proposal).where(
                    BrainstormJobRow.conversation_id == conversation_id,
                    BrainstormJobRow.proposal.is_not(None),
                )
            )
        ).all()
        by_turn = {
            proposal.turn_id: proposal
            for payload in proposals
            if payload is not None
            for proposal in (BrainstormProposal.model_validate(payload),)
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
        return AuthoringReceipt.model_validate(
            {"job_id": row.id, "job_version": row.version, "state": row.state, "replay_key": key}
        )

    async def outcome(self, row: BrainstormJobRow) -> AuthoringOutcome:
        attempt = (
            await self.session.get(BrainstormAttemptRow, row.current_attempt_id)
            if row.current_attempt_id
            else None
        )
        charged, held, unknown = self._charges(await self._epic_attempts(row.epic_id))
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
                and (attempt.process_settled or not attempt.launch_intent)
                else None,
                "duration_lower_bound_ms": lower,
                "tool_call_count": max(attempt.tool_calls_used, recorded_tools)
                if type(recorded_tools) is int
                else attempt.tool_calls_used,
                "input_tokens": raw_usage.get("input_tokens"),
                "output_tokens": raw_usage.get("output_tokens"),
                "estimated_api_cost_minor": raw_usage.get("estimated_api_cost_minor"),
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
        currency = raw_usage.get("currency") if raw_usage else None
        safe_currency = (
            currency
            if isinstance(currency, str)
            and len(currency) == 3
            and currency.isascii()
            and currency.isupper()
            and currency.isalpha()
            else None
        )
        return AuthoringOutcome.model_validate(
            {
                "job_id": row.id,
                "job_version": row.version,
                "state": row.state,
                "proposal_digest": row.proposal_digest,
                "proposal": BrainstormProposal.model_validate(row.proposal)
                if row.proposal
                else None,
                "adopted_revision_id": row.adopted_revision_id,
                "failure": row.failure,
                "usage_known": attempt.usage_known if attempt else None,
                "process_settled": attempt.process_settled if attempt else False,
                "usage": usage,
                "reservation": BrainstormReservation.model_validate(attempt.reservation)
                if attempt
                else None,
                "cumulative_usage": BrainstormAmounts.model_validate(charged),
                "held_reservations": BrainstormAmounts.model_validate(held),
                "uncertain_attempts": unknown,
                "currency": safe_currency,
                "unknown_usage_fields": usage.unknown_fields if usage else (),
                "held_reasons": BrainstormHeldReasons.model_validate(
                    {key: "unsettled_or_unknown" for key, value in held.items() if value}
                ),
            }
        )

    async def lock_command(self, epic_id: UUID, key: str) -> None:
        """Serialize one idempotency key before reading receipts or command state."""
        if not key or len(key) > 255:
            raise ValueError("invalid idempotency key")
        identity = sha256(epic_id.bytes + key.encode("utf-8")).digest()
        await self.session.execute(
            select(func.pg_advisory_xact_lock(int.from_bytes(identity[:8], "big", signed=True)))
        )

    async def replay(self, epic_id: UUID, key: str, digest: str) -> dict[str, object] | None:
        row = await self.session.scalar(
            select(BrainstormReceiptRow).where(
                BrainstormReceiptRow.epic_id == epic_id, BrainstormReceiptRow.key == key
            )
        )
        if row is None:
            return None
        if row.request_digest != digest:
            raise BrainstormConflict("idempotency key payload conflicts")
        return row.response

    async def save_receipt(
        self, epic_id: UUID, key: str, digest: str, response: dict[str, object]
    ) -> None:
        if not key or len(key) > 255:
            raise ValueError("invalid idempotency key")
        self.session.add(
            BrainstormReceiptRow(
                id=uuid4(), epic_id=epic_id, key=key, request_digest=digest, response=response
            )
        )
        await self.session.flush()

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
        self, owner: str, lease_seconds: int = 30
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
                reservation, waitable = await self._reservation(row, snapshot)
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
