"""Authenticated operator quota inspection and exhaustion reporting."""
# ruff: noqa: B008, BLE001

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from forge.api.dependencies import (
    require_idempotency_key,
    require_operator,
    require_operator_mutation,
)
from forge.api.errors import translate_error
from forge.api.schemas.subscription_quota import QuotaExhaustionReportRequest, QuotaStatusResponse
from forge.application.services.auth import AuthenticatedActor
from forge.domain.subscription_quota import PoolQuotaStatus


def router_for() -> APIRouter:
    router = APIRouter()

    @router.get("/subscription-quota", response_model=list[QuotaStatusResponse])
    async def list_quota(
        request: Request,
        offset: int = Query(default=0, ge=0, le=1_000_000),
        limit: int = Query(default=100, ge=1, le=100),
        _actor: AuthenticatedActor = Depends(require_operator),
    ) -> list[QuotaStatusResponse]:
        try:
            return [_response(value) for value in await _service(request).list(offset=offset, limit=limit)]
        except Exception as error:
            raise translate_error(error) from None

    @router.post(
        "/subscription-quota/reports",
        response_model=QuotaStatusResponse,
        status_code=status.HTTP_200_OK,
    )
    async def report_quota(
        body: QuotaExhaustionReportRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> QuotaStatusResponse:
        try:
            value = await _service(request).report_exhaustion(
                actor=actor, idempotency_key=idempotency_key, request=body
            )
            return _response(value)
        except Exception as error:
            raise translate_error(error) from None

    return router


def _service(request: Request) -> Any:
    value = getattr(request.app.state, "subscription_quota_service", None)
    if value is None:
        raise HTTPException(status_code=503, detail="quota inspection unavailable")
    return value


def _response(value: PoolQuotaStatus) -> QuotaStatusResponse:
    return QuotaStatusResponse(
        provider=value.key.provider,
        account=value.key.account,
        pool=value.key.pool,
        status=value.status,
        revision=value.revision,
        observed_at=value.observed_at,
        reason=value.reason,
        reset_at=value.reset_at,
        next_eligible_at=value.next_eligible_at,
        retry_basis=value.retry_basis,
        probe_attempt_id=value.probe_attempt_id,
        recovered_at=value.recovered_at,
    )


__all__ = ["router_for"]
