"""Run-serialized Jev admissions; no network operation holds a DB transaction."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, load_only

from forge.application.ports.jev import JevRequest, JevResult
from forge.domain.policy import JevPolicy
from forge.persistence.models.jev import JevEvaluation
from forge.persistence.models.project import ProjectPolicyVersion
from forge.persistence.models.run import Run


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PostgresJevRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _lock_run(self, run_id: UUID) -> Run:
        run = await self._session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None:
            raise ValueError("Jev run is unavailable")
        return run

    async def _authorize(self, request: JevRequest, policy: JevPolicy) -> Run:
        run = await self._lock_run(request.run_id)
        if run.policy_version != request.policy_version:
            raise ValueError("Jev policy version does not match the run")
        policy_row = await self._session.get(
            ProjectPolicyVersion, (run.project_id, run.policy_version)
        )
        stored_jev = None if policy_row is None else policy_row.document.get("jev")
        if (
            not isinstance(stored_jev, dict)
            or JevPolicy.model_validate(stored_jev) != policy
            or policy.mode == "off"
        ):
            raise ValueError("Jev policy does not authorize this run")
        return run

    @staticmethod
    def _same_identity(
        row: JevEvaluation, request: JevRequest, request_digest: str, policy: JevPolicy
    ) -> bool:
        return (
            row.request_digest == request_digest
            and row.policy_version == request.policy_version
            and row.kind == request.kind
            and row.requested_mode == policy.mode
            and row.requested_model == policy.model
            and row.worktree_digest == request.worktree_digest
            and row.candidate_digest == request.candidate_digest
            and row.scope_digest == request.scope_digest
        )

    async def observe_unavailable(
        self, request: JevRequest, *, request_digest: str, policy: JevPolicy,
        diagnostic: str | None = None,
    ) -> None:
        await self._authorize(request, policy)
        await self._record_refusal(
            request, request_digest=request_digest, policy=policy,
            status="unavailable", diagnostic=diagnostic,
        )

    async def _record_refusal(
        self, request: JevRequest, *, request_digest: str, policy: JevPolicy,
        status: Literal["unavailable", "budget_exhausted"], diagnostic: str | None,
    ) -> None:
        operation_digest = _digest(request.operation_key)
        existing = await self._session.get(JevEvaluation, (request.run_id, operation_digest))
        if existing is not None:
            if not self._same_identity(existing, request, request_digest, policy):
                raise ValueError("Jev operation replay conflicts")
            return
        self._session.add(
            JevEvaluation(
                run_id=request.run_id,
                operation_digest=operation_digest,
                policy_version=request.policy_version,
                kind=request.kind,
                requested_mode=policy.mode,
                effective_mode="off",
                requested_model=policy.model,
                worktree_digest=request.worktree_digest,
                request_digest=request_digest,
                candidate_digest=request.candidate_digest,
                scope_digest=request.scope_digest,
                status=status,
                diagnostic=diagnostic,
                reserved_input_units=0,
                actual_input_units=0,
                output_units=0,
                duration_ms=0,
                cache_hits=0,
            )
        )
        await self._session.flush()

    async def reserve(
        self,
        request: JevRequest,
        *,
        request_digest: str,
        model: str,
        input_units: int,
        max_requests: int,
        max_input_units: int,
        mode: str = "on",
        policy: JevPolicy,
    ) -> JevResult | None:
        await self._authorize(request, policy)
        if not policy.allow_remote:
            raise ValueError("Jev policy does not authorize this run")
        operation_digest = _digest(request.operation_key)
        existing = await self._session.get(JevEvaluation, (request.run_id, operation_digest))
        if existing is not None:
            if not self._same_identity(existing, request, request_digest, policy):
                raise ValueError("Jev operation replay conflicts")
            if existing.status == "pending":
                return JevResult(
                    status="unknown", requested_model=model, diagnostic="in_flight_or_interrupted"
                )
            if existing.status in ("ranked", "succeeded"):
                source = (
                    existing
                    if existing.cache_source_digest is None
                    else await self._session.get(
                        JevEvaluation, (request.run_id, existing.cache_source_digest)
                    )
                )
                if (
                    source is None
                    or policy.cache_ttl_seconds == 0
                    or source.created_at + timedelta(seconds=policy.cache_ttl_seconds)
                    <= datetime.now(UTC)
                ):
                    return JevResult(
                        status="unknown",
                        requested_model=model,
                        diagnostic="stale_operation_evidence",
                    )
                existing.cache_hits += 1
                await self._session.flush()
                return JevResult(
                    status="cached",
                    answers=existing.scores or {},
                    requested_model=model,
                    actual_model=existing.actual_model,
                    input_units=existing.actual_input_units,
                    output_units=existing.output_units,
                    duration_ms=existing.duration_ms,
                    diagnostic="operation_replay",
                )
            return JevResult(
                status=existing.status,
                requested_model=model,
                actual_model=existing.actual_model,
                input_units=existing.actual_input_units,
                output_units=existing.output_units,
                duration_ms=existing.duration_ms,
                diagnostic="operation_replay",
            )
        matches = (
            await self._session.scalars(
                select(JevEvaluation)
                .where(
                    JevEvaluation.run_id == request.run_id,
                    JevEvaluation.request_digest == request_digest,
                    JevEvaluation.reserved_input_units > 0,
                    JevEvaluation.status.in_(("pending", "unknown", "ranked", "succeeded")),
                )
                .options(defer(JevEvaluation.scores))
                .order_by(JevEvaluation.created_at.desc(), JevEvaluation.operation_digest.desc())
            )
        ).all()
        if any(
            row.status in ("pending", "unknown")
            and self._same_identity(row, request, request_digest, policy)
            for row in matches
        ):
            return JevResult(
                status="unknown", requested_model=model, diagnostic="matching_admission_uncertain"
            )
        if policy.cache_ttl_seconds > 0:
            source = next(
                (
                    row
                    for row in matches
                    if row.status in ("ranked", "succeeded")
                    and self._same_identity(row, request, request_digest, policy)
                    and row.created_at + timedelta(seconds=policy.cache_ttl_seconds)
                    > datetime.now(UTC)
                ),
                None,
            )
            if source is not None:
                await self._session.refresh(source, attribute_names=["scores"])
                scores = source.scores
                self._session.add(
                    JevEvaluation(
                        run_id=request.run_id,
                        operation_digest=operation_digest,
                        policy_version=request.policy_version,
                        kind=request.kind,
                        requested_mode=policy.mode,
                        effective_mode=source.effective_mode,
                        requested_model=model,
                        actual_model=source.actual_model,
                        worktree_digest=request.worktree_digest,
                        request_digest=request_digest,
                        candidate_digest=request.candidate_digest,
                        scope_digest=request.scope_digest,
                        status=source.status,
                        reserved_input_units=0,
                        actual_input_units=0,
                        output_units=0,
                        duration_ms=0,
                        cache_hits=1,
                        scores=scores,
                        cache_source_digest=source.operation_digest,
                    )
                )
                await self._session.flush()
                return JevResult(
                    status="cached",
                    answers=scores or {},
                    requested_model=model,
                    actual_model=source.actual_model,
                    diagnostic="content_cache",
                )
        calls, reserved = (
            await self._session.execute(
                select(func.count(), func.coalesce(func.sum(JevEvaluation.reserved_input_units), 0))
                .where(JevEvaluation.run_id == request.run_id, JevEvaluation.reserved_input_units > 0)
            )
        ).one()
        if calls >= max_requests or reserved + input_units > max_input_units:
            await self._record_refusal(
                request, request_digest=request_digest, policy=policy,
                status="budget_exhausted", diagnostic="budget_exhausted",
            )
            return JevResult(
                status="budget_exhausted", requested_model=model, diagnostic="run_budget_exhausted"
            )
        self._session.add(
            JevEvaluation(
                run_id=request.run_id,
                operation_digest=operation_digest,
                policy_version=request.policy_version,
                kind=request.kind,
                requested_mode=mode,
                effective_mode=mode,
                requested_model=model,
                worktree_digest=request.worktree_digest,
                request_digest=request_digest,
                candidate_digest=request.candidate_digest,
                scope_digest=request.scope_digest,
                status="pending",
                reserved_input_units=input_units,
                actual_input_units=0,
                output_units=0,
                duration_ms=0,
                cache_hits=0,
            )
        )
        await self._session.flush()
        return None

    async def settle(self, request: JevRequest, response: JevResult) -> None:
        await self._lock_run(request.run_id)
        row = await self._session.get(
            JevEvaluation, (request.run_id, _digest(request.operation_key))
        )
        if row is None:
            raise ValueError("Jev operation was not admitted")
        if row.status != "pending":
            return
        if response.status not in ("ranked", "succeeded", "unavailable", "unknown"):
            raise ValueError("Jev settlement status is invalid")
        row.status = response.status
        row.actual_model = response.actual_model
        row.actual_input_units = response.input_units
        row.reserved_input_units = max(row.reserved_input_units, response.input_units)
        row.output_units = response.output_units
        row.duration_ms = response.duration_ms
        row.diagnostic = response.diagnostic
        row.request_id_digest = _digest(response.request_id) if response.request_id else None
        row.scores = response.answers if response.status in ("ranked", "succeeded") else None
        await self._session.flush()

    async def summary(self, run_id: UUID, *, policy: JevPolicy | None = None) -> dict[str, Any]:
        rows = (
            await self._session.scalars(
                select(JevEvaluation)
                .where(JevEvaluation.run_id == run_id)
                .options(
                    load_only(
                        JevEvaluation.kind,
                        JevEvaluation.status,
                        JevEvaluation.diagnostic,
                        JevEvaluation.reserved_input_units,
                        JevEvaluation.effective_mode,
                        JevEvaluation.actual_model,
                        JevEvaluation.cache_hits,
                        JevEvaluation.actual_input_units,
                        JevEvaluation.output_units,
                        JevEvaluation.duration_ms,
                    )
                )
                .order_by(JevEvaluation.created_at, JevEvaluation.operation_digest)
            )
        ).all()
        kinds = Counter(row.kind for row in rows)
        statuses = Counter("unknown" if row.status == "pending" else row.status for row in rows)
        diagnostics = Counter(row.diagnostic for row in rows if row.diagnostic)
        reserved = sum(row.reserved_input_units for row in rows)
        requested_mode = policy.mode if policy is not None else "off"
        return {
            "requested_mode": requested_mode,
            "effective_mode": rows[-1].effective_mode if rows else "not_yet_observed",
            "requested_model": policy.model if policy is not None else None,
            "actual_model": next(
                (row.actual_model for row in reversed(rows) if row.actual_model), None
            ),
            "review_focus_available": any(
                row.kind == "review_focus" and row.status in ("ranked", "succeeded") for row in rows
            ),
            "availability": (
                "off"
                if policy is None or policy.mode == "off"
                else "not_enabled"
                if not policy.allow_remote
                else "no_samples"
                if not rows
                else "degraded"
                if statuses["unknown"] or statuses["unavailable"] or statuses["budget_exhausted"]
                else "sampled"
            ),
            "calls": sum(row.reserved_input_units > 0 for row in rows),
            "attempts": len(rows),
            "cache_hits": sum(row.cache_hits for row in rows),
            "unknown": statuses["unknown"],
            "actual_input_units": sum(row.actual_input_units for row in rows),
            "actual_output_units": sum(row.output_units for row in rows),
            "reserved_input_units": reserved,
            "duration_ms": sum(row.duration_ms for row in rows),
            "remaining_requests": max(
                0, policy.max_requests_per_run - sum(row.reserved_input_units > 0 for row in rows)
            )
            if policy
            else 0,
            "remaining_input_units": max(0, policy.max_input_units_per_run - reserved)
            if policy
            else 0,
            "by_kind": dict(kinds),
            "by_status": dict(statuses),
            "by_diagnostic": dict(diagnostics),
        }


__all__ = ["PostgresJevRepository"]
