"""Process health contract."""

from fastapi import APIRouter
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    role: str


def router_for(role: str) -> APIRouter:
    """Build the health router for one process role."""

    router = APIRouter()

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", role=role)

    return router
