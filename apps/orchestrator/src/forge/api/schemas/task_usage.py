"""Strict wire authoring for optional task monitoring allowances."""

from dataclasses import fields, is_dataclass
from typing import Literal, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from forge.application.ports.task_usage import OwnerCommandReceipt, UsageReport
from forge.domain.task_usage_contract import (
    BaselineReference,
    CheckpointRecord,
    MonitoringPolicy,
    Observation,
    OwnerCommand,
    WorkUnitBinding,
)


def _reject_extra(value: object, expected: object) -> None:
    if not isinstance(value, dict):
        return
    candidates = (expected, *get_args(expected))
    for candidate in candidates:
        if not is_dataclass(candidate):
            continue
        hints = get_type_hints(candidate)
        allowed = {field.name for field in fields(candidate)}
        extra = value.keys() - allowed
        if extra:
            raise ValueError(f"unexpected wire fields: {sorted(extra)}")
        for name, item in value.items():
            hint = hints[name]
            origin = get_origin(hint)
            if origin in (tuple, list) and isinstance(item, (tuple, list)):
                for child in item:
                    _reject_extra(child, get_args(hint)[0])
            else:
                _reject_extra(item, hint)
        break


class StrictWire:
    def __init__(self, value_type: type):
        self.value_type = value_type
        self.adapter: TypeAdapter[object] = TypeAdapter(value_type)

    def validate_python(self, value: object) -> object:
        try:
            _reject_extra(value, self.value_type)
        except ValueError as exc:
            from pydantic_core import PydanticCustomError

            raise ValidationError.from_exception_data(
                self.value_type.__name__,
                [
                    {
                        "type": PydanticCustomError("extra_forbidden", str(exc)),
                        "loc": (),
                        "input": value,
                    }
                ],
            ) from exc
        return self.adapter.validate_python(value)

    def dump_python(self, value: object, *, mode: Literal["json", "python"] = "python") -> object:
        return self.adapter.dump_python(value, mode=mode)


WorkUnitWire = StrictWire(WorkUnitBinding)
ObservationWire = StrictWire(Observation)
PolicyWire = StrictWire(MonitoringPolicy)
BaselineWire = StrictWire(BaselineReference)
CheckpointWire = StrictWire(CheckpointRecord)
OwnerCommandWire = StrictWire(OwnerCommand)
OwnerCommandReceiptWire = StrictWire(OwnerCommandReceipt)
UsageReportWire = StrictWire(UsageReport)


class TaskAllowanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    manual_estimate_tokens: int | None = Field(default=None, ge=1)
    manual_estimate_context_tokens: int | None = Field(default=None, ge=1)
    hard_token_cap: int | None = Field(default=None, ge=1)
    hard_context_cap: int | None = Field(default=None, ge=1)
    legacy_max_input_tokens: int | None = Field(default=None, ge=0)
    legacy_max_output_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_domain(self) -> TaskAllowanceInput:
        # Share the cap rule with the domain instead of maintaining a second meaning.
        from uuid import UUID

        from forge.domain.task_usage_contract import MonitoringPolicy

        MonitoringPolicy(
            project_id=UUID(int=0),
            hard_token_cap=self.hard_token_cap,
            hard_context_cap=self.hard_context_cap,
        )
        return self


def decode_legacy_allowance(payload: dict[str, object]) -> TaskAllowanceInput:
    """Retain finite legacy envelopes; absent and explicit null remain uncapped."""

    values = dict(payload)
    aliases = {
        "max_tokens": "hard_token_cap",
        "max_context_tokens": "hard_context_cap",
        "max_input_tokens": "legacy_max_input_tokens",
        "max_output_tokens": "legacy_max_output_tokens",
    }
    if "max_tokens" in values and ("max_input_tokens" in values or "max_output_tokens" in values):
        raise ValueError("conflicting legacy token cap shapes")
    for alias, target in aliases.items():
        if alias in values:
            if target in values:
                raise ValueError(f"conflicting allowance aliases: {alias} and {target}")
            values[target] = values.pop(alias)
    return TaskAllowanceInput.model_validate(values)
