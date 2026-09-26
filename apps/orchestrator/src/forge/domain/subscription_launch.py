"""Bounded, immutable terminal evidence from the controlled client supervisor."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SubscriptionLaunchTerminalProof(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    launch_id: str = Field(min_length=1, max_length=255)
    pid: int = Field(gt=0)
    process_identity: str = Field(min_length=1, max_length=255)
    outcome: Literal[
        "exited", "completed", "timeout", "cancelled", "protocol_error", "stop_uncertain"
    ]
    return_code: int | None
    stop_confirmed: bool
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)
    stdout_truncated: bool
    stderr_truncated: bool

    @model_validator(mode="after")
    def stopped_identity(self) -> Self:
        if self.stop_confirmed and (self.return_code is None or self.outcome == "stop_uncertain"):
            raise ValueError("confirmed stop requires a terminal process result")
        return self

    @property
    def permits_decision(self) -> bool:
        # The supervisor may terminate an otherwise healthy interactive server
        # after its final protocol response; a zero exit status is not required.
        return (
            self.stop_confirmed
            and self.outcome in {"exited", "completed"}
            and not self.stdout_truncated
        )
