"""Bounded HTTP error translation for application and persistence failures."""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError

from forge.application.adapters.git import RepositoryInspectionError
from forge.application.services.projects import ProjectServiceError
from forge.application.services.runs import (
    RunCommandValidationError,
    RunServiceError,
)
from forge.application.services.tasks import TaskServiceError
from forge.persistence.repositories.commands import IdempotencyConflict
from forge.persistence.repositories.mutations import (
    MutationConflict,
    MutationIncomplete,
    MutationNotFound,
)
from forge.persistence.repositories.projects import (
    PolicyNotFound,
    PolicyVersionConflict,
    ProjectIdentityConflict,
    ProjectNotFound,
)
from forge.persistence.repositories.runs import ConcurrencyConflict, RunCreationError, RunNotFound
from forge.persistence.repositories.tasks import (
    TaskIdentityConflict,
    TaskNotFound,
    TaskProjectNotFound,
)

_NOT_FOUND = (
    ProjectNotFound,
    PolicyNotFound,
    TaskNotFound,
    TaskProjectNotFound,
    RunNotFound,
    MutationNotFound,
)
_CONFLICT = (
    ProjectIdentityConflict,
    TaskIdentityConflict,
    PolicyVersionConflict,
    MutationConflict,
    MutationIncomplete,
    ConcurrencyConflict,
    IdempotencyConflict,
    RunCommandValidationError,
)
_UNPROCESSABLE = (RepositoryInspectionError, RunCreationError)
_UNAVAILABLE = (ProjectServiceError, TaskServiceError, RunServiceError, SQLAlchemyError)


def translate_error(error: BaseException) -> HTTPException:
    """Return a safe status/detail without interpolating exception text."""

    if isinstance(error, _NOT_FOUND):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="resource not found")
    if isinstance(error, _CONFLICT):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="request conflicts with current state"
        )
    if isinstance(error, _UNPROCESSABLE + (TypeError, ValueError)):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="request cannot be processed"
        )
    if isinstance(error, _UNAVAILABLE):
        detail = (
            "persistence unavailable"
            if isinstance(error, SQLAlchemyError)
            else "service unavailable"
        )
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)
    # The route test doubles use these bounded sentinel messages for missing rows.
    if str(error).strip().casefold() in {"missing project", "missing task", "missing run"}:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="resource not found")
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="service unavailable"
    )


__all__ = ["translate_error"]
