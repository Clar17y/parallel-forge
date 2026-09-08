"""Authenticated current prompt metadata, without instruction contents."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from forge.agents.prompt_loader import PromptLoader, PromptLoadError
from forge.api.dependencies import require_operator
from forge.application.services.auth import AuthenticatedActor
from forge.domain.actor import AgentRole


class PromptMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: AgentRole
    version: str
    digest: str
    scope: Literal["current_configuration"] = "current_configuration"


def _metadata(root: Path) -> list[PromptMetadata]:
    loader = PromptLoader(root)
    result = []
    for role in (AgentRole.PLANNER, AgentRole.DEVELOPER, AgentRole.REVIEWER):
        prompt = loader.load(role)
        result.append(PromptMetadata(role=role, version=prompt.version, digest=prompt.digest))
    return result


def router_for() -> APIRouter:
    router = APIRouter()

    @router.get("/agent-prompts", response_model=list[PromptMetadata])
    async def prompts(
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> list[PromptMetadata]:
        root = request.app.state.settings.prompt_root or Path("agents")
        try:
            return await run_in_threadpool(_metadata, root)
        except PromptLoadError:
            raise HTTPException(503, "prompt metadata unavailable") from None

    return router
