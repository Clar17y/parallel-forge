"""Authenticated paginated run list projection route."""

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import AfterValidator

from forge.api.dependencies import require_operator
from forge.api.schemas.run_list import RunListPage
from forge.application.services.auth import AuthenticatedActor
from forge.domain.run import RunState
from forge.persistence.queries.run_list import RunListQuery


def _reject_nul(value: str | None) -> str | None:
    # PostgreSQL cannot accept a NUL byte in a text bind, so reject it at
    # the API boundary instead of surfacing it as a persistence outage.
    if value is not None and "\x00" in value:
        raise ValueError("search text must not contain NUL bytes")
    return value


def router_for() -> APIRouter:
    router = APIRouter()

    @router.get("/run-projections", response_model=RunListPage)
    async def run_projections(
        request: Request,
        state: RunState | None = Query(None),  # noqa: B008
        states: Annotated[list[RunState] | None, Query()] = None,
        q: Annotated[str | None, Query(max_length=200), AfterValidator(_reject_nul)] = None,
        project_id: UUID | None = Query(None),  # noqa: B008
        attention: bool | None = Query(None),
        exclude_pending_gate: bool | None = Query(None),
        updated_since: datetime | None = Query(None),  # noqa: B008
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> dict[str, object]:
        query = getattr(request.app.state, "run_list_query", None)
        if query is None:
            raise HTTPException(503, "projection unavailable")
        combined: list[str] = []
        for candidate in [state, *(states or [])]:
            if candidate is not None and candidate.value not in combined:
                combined.append(candidate.value)
        items, truncated = await cast(RunListQuery, query).list(
            states=combined or None,
            q=q,
            project_id=project_id,
            attention=attention,
            exclude_pending_gate=exclude_pending_gate,
            updated_since=updated_since,
            offset=offset,
            limit=limit,
        )
        return {"items": items, "offset": offset, "limit": limit, "truncated": truncated}

    return router
