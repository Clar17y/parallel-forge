"""One authenticated preview/apply boundary for subscription result recovery."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.domain.subscription_recovery import (
    RecoveryApplyRequest,
    RecoveryBudgetImpact,
    RecoveryPreview,
    RecoveryPreviewRequest,
    RecoveryReceipt,
)
from forge.persistence.repositories.subscription_recovery import RecoveryConflict

RecoveryActor = AuthenticatedActor | LocalOperatorProfileActor


class SubscriptionRecoveryService:
    def __init__(self, work_factory: Callable[[], UnitOfWork]) -> None:
        self._factory = work_factory

    async def preview(
        self,
        *,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        actor: RecoveryActor,
        request: RecoveryPreviewRequest,
    ) -> RecoveryPreview:
        if not isinstance(actor, (AuthenticatedActor, LocalOperatorProfileActor)):
            raise RecoveryConflict("operator authentication required")
        async with self._factory() as work:
            source = await work.subscription_recovery.preview(
                run_id, task_id, attempt_id, request.action
            )
            key = await work.subscription_recovery.signing_key()
            await work.rollback()
        expires = datetime.now(UTC) + timedelta(minutes=5)
        token = _signed(
            {
                "schema_version": 1,
                "run_id": str(run_id),
                "task_id": str(task_id),
                "attempt_id": str(attempt_id),
                "actor_id": str(actor.actor_id),
                "action": request.action.value,
                "binding": source.binding,
                "expires_at": expires.isoformat(),
            },
            key,
        )
        return RecoveryPreview(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            action=request.action,
            eligible=source.eligible,
            reason_code=source.reason_code,
            message=source.message,
            changes=source.changes,
            retained_evidence=source.retained_evidence,
            budget_impact=RecoveryBudgetImpact(
                provider_attempts=source.provider_attempts,
                repair_units=source.repair_units,
            ),
            preview_token=token,
            expires_at=expires,
        )

    async def apply(
        self,
        *,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        actor: RecoveryActor,
        idempotency_key: str,
        request: RecoveryApplyRequest,
    ) -> RecoveryReceipt:
        if not isinstance(actor, (AuthenticatedActor, LocalOperatorProfileActor)):
            raise RecoveryConflict("operator authentication required")
        if not idempotency_key or len(idempotency_key) > 255:
            raise RecoveryConflict("invalid recovery idempotency key")
        request_digest = hashlib.sha256(
            json.dumps(
                {
                    "run_id": str(run_id),
                    "task_id": str(task_id),
                    "attempt_id": str(attempt_id),
                    "actor_id": str(actor.actor_id),
                    "action": request.action.value,
                    "preview_token": request.preview_token,
                    "reason": request.reason,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with self._factory() as work:
            prior = await work.subscription_recovery.receipt_for(
                run_id, actor.actor_id, idempotency_key
            )
            if prior is not None:
                if prior.request_digest != request_digest:
                    raise RecoveryConflict("recovery idempotency key conflicts")
                await work.rollback()
                return prior.receipt
            key = await work.subscription_recovery.signing_key()
            claims = _verified(request.preview_token, key)
            if (
                claims.get("schema_version") != 1
                or claims.get("run_id") != str(run_id)
                or claims.get("task_id") != str(task_id)
                or claims.get("attempt_id") != str(attempt_id)
                or claims.get("actor_id") != str(actor.actor_id)
                or claims.get("action") != request.action.value
            ):
                raise RecoveryConflict("recovery preview identity differs")
            try:
                expires = datetime.fromisoformat(str(claims["expires_at"]))
            except KeyError, TypeError, ValueError:
                raise RecoveryConflict("recovery preview expiry is invalid") from None
            if expires.tzinfo is None:
                raise RecoveryConflict("recovery preview expired")
            binding = claims.get("binding")
            if not isinstance(binding, str) or len(binding) != 64:
                raise RecoveryConflict("recovery preview binding is invalid")
            receipt = await work.subscription_recovery.apply(
                run_id,
                task_id,
                attempt_id,
                request.action,
                actor_id=actor.actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                binding=binding,
                expires_at=expires,
                reason=request.reason,
            )
            await work.commit()
            return receipt


def _signed(claims: dict[str, object], key: bytes) -> str:
    data = (
        base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    digest = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
    return f"{data}.{digest}"


def _verified(token: str, key: bytes) -> dict[str, object]:
    try:
        data, signature = token.split(".", 1)
        expected = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        if not isinstance(claims, dict):
            raise TypeError
        return claims
    except TypeError, ValueError:
        raise RecoveryConflict("recovery preview signature is invalid") from None
