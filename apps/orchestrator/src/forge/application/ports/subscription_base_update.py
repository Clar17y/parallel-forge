"""Budget reservation before a controlled base update changes the candidate."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BaseUpdateReservation:
    primary_task_version: int
    scheduled_repairs: int
    candidate_epoch: int

    def payload(self) -> dict[str, object]:
        return {
            "primary_task_version": self.primary_task_version,
            "scheduled_repairs": self.scheduled_repairs,
            "candidate_epoch": self.candidate_epoch,
        }

    @classmethod
    def from_payload(cls, value: object) -> BaseUpdateReservation:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"primary_task_version", "scheduled_repairs", "candidate_epoch"}
            or any(
                type(value[key]) is not int or value[key] < minimum
                for key, minimum in (
                    ("primary_task_version", 1),
                    ("scheduled_repairs", 0),
                    ("candidate_epoch", 0),
                )
            )
        ):
            raise ValueError("base update reservation differs")
        return cls(
            value["primary_task_version"], value["scheduled_repairs"], value["candidate_epoch"]
        )
