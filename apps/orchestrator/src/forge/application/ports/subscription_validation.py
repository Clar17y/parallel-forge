"""Durable controller validation bound to one primary acceptance source."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from forge.application.ports.subscription_candidate import CandidateInspection
from forge.domain.command import CommandEnvelope


@dataclass(frozen=True, slots=True)
class AcceptanceValidationRepair:
    repaired: bool
    primary_task_version: int
    scheduled_repairs: int
    candidate_epoch: int

    def payload(self) -> dict[str, object]:
        return {
            "repaired": self.repaired,
            "primary_task_version": self.primary_task_version,
            "scheduled_repairs": self.scheduled_repairs,
            "candidate_epoch": self.candidate_epoch,
        }

    @classmethod
    def from_payload(cls, value: object) -> AcceptanceValidationRepair:
        if not isinstance(value, Mapping) or set(value) != {
            "repaired",
            "primary_task_version",
            "scheduled_repairs",
            "candidate_epoch",
        }:
            raise ValueError("validation repair receipt differs")
        if type(value["repaired"]) is not bool or any(
            type(value[key]) is not int or value[key] < minimum
            for key, minimum in (
                ("primary_task_version", 1),
                ("scheduled_repairs", 0),
                ("candidate_epoch", 0),
            )
        ):
            raise ValueError("validation repair receipt values differ")
        return cls(
            value["repaired"],
            value["primary_task_version"],
            value["scheduled_repairs"],
            value["candidate_epoch"],
        )


@dataclass(frozen=True, slots=True)
class AcceptanceValidationBinding:
    attempt_id: UUID
    result_digest: str
    application_digest: str
    candidate: CandidateInspection
    receipt_evidence_digest: str
    approval_id: UUID
    command: CommandEnvelope

    def payload(self) -> dict[str, object]:
        command = self.command
        return {
            "schema_version": 1,
            "source_attempt_id": str(self.attempt_id),
            "source_result_digest": self.result_digest,
            "source_application_digest": self.application_digest,
            "candidate": self.candidate.payload(),
            "receipt_evidence_digest": self.receipt_evidence_digest,
            "approval_id": str(self.approval_id),
            "command": {
                "id": str(command.id),
                "key": command.idempotency_key,
                "type": command.command_type,
                "payload": dict(command.payload),
                "schema_version": command.payload_schema_version,
                "version": command.expected_run_version,
                "actor_id": str(command.actor_id),
            },
        }
