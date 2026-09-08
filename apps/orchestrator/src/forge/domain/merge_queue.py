"""Queue admission identity, separate from the authoritative merged-PR outcome."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MergeQueueReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    repository: str = Field(
        min_length=3, max_length=512, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
    )
    pull_request_number: int = Field(ge=1)
    pull_request_node_id: str = Field(min_length=1, max_length=512, pattern=r"^\S+$")
    entry_id: str = Field(min_length=1, max_length=512, pattern=r"^\S+$")
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    merge_method: Literal["merge", "squash", "rebase"]
