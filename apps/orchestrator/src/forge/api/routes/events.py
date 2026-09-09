"""Authenticated resumable event stream; PostgreSQL remains authoritative."""

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from forge.api.dependencies import require_operator
from forge.api.sse import event_stream, parse_cursor
from forge.application.services.auth import AuthenticatedActor
from forge.persistence.repositories.runs import PersistenceDataError


def router_for() -> APIRouter:
    router = APIRouter()

    @router.get("/runs/{run_id}/events", response_class=StreamingResponse)
    async def events(
        run_id: UUID,
        request: Request,
        after: str | None = Query(default=None),
        actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> StreamingResponse:
        try:
            cursor = parse_cursor(request.headers.get("Last-Event-ID"), after)
        except ValueError:
            raise HTTPException(422, "invalid event cursor") from None
        query = request.app.state.event_query
        if query is None:
            raise HTTPException(503, "event reads unavailable")
        if not await query.exists(run_id):
            raise HTTPException(404, "run not found")

        async def checked_stream() -> AsyncIterator[bytes]:
            try:
                async for frame in event_stream(
                    cursor=cursor,
                    read_page=lambda position: query.page(run_id, position),
                    check_session=lambda: request.app.state.auth_service.session_info(actor),
                    disconnected=request.is_disconnected,
                    shutdown=request.app.state.shutdown_event,
                ):
                    yield frame
            except SQLAlchemyError, PersistenceDataError, ValueError:
                # Headers already went out. Close without emitting backend errors.
                return

        return StreamingResponse(
            checked_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router
