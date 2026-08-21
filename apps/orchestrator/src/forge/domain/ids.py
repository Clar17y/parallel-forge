"""Typed UUID identifiers shared by domain and application boundaries."""

from typing import NewType
from uuid import UUID

ProjectId = NewType("ProjectId", UUID)
RunId = NewType("RunId", UUID)
TaskId = NewType("TaskId", UUID)

__all__ = ["ProjectId", "RunId", "TaskId"]
