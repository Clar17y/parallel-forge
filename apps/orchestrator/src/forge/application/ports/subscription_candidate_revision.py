"""Closed evidence for reopening a subscription candidate at its PR gate."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from forge.application.ports.subscription_candidate import CandidateInspection


@dataclass(frozen=True, slots=True)
class AcceptanceRevision:
    reason: str
    pr_evidence_digest: str
    observation: CandidateInspection
    diff_digest: str
    feedback_digest: str | None = None
    feedback: str | None = None

    def __post_init__(self) -> None:
        if self.reason not in {"content_drift", "operator_feedback"}:
            raise ValueError("candidate revision reason differs")
        for digest in (self.pr_evidence_digest, self.diff_digest):
            if not isinstance(digest, str) or re.fullmatch("[0-9a-f]{64}", digest) is None:
                raise ValueError("candidate revision digest differs")
        if not isinstance(self.observation, CandidateInspection):
            raise TypeError("candidate revision observation differs")
        if self.reason == "operator_feedback":
            if (
                not isinstance(self.feedback, str)
                or not self.feedback.strip()
                or "\x00" in self.feedback
                or len(self.feedback.encode("utf-8")) > 16_384
                or not isinstance(self.feedback_digest, str)
                or re.fullmatch("[0-9a-f]{64}", self.feedback_digest) is None
            ):
                raise ValueError("candidate revision feedback differs")
        elif self.feedback is not None or self.feedback_digest is not None:
            raise ValueError("candidate drift cannot carry operator feedback")

    def payload(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "pr_evidence_digest": self.pr_evidence_digest,
            "observation": self.observation.payload(),
            "diff_digest": self.diff_digest,
            "feedback_digest": self.feedback_digest,
        }

    @classmethod
    def from_payload(cls, value: object, *, feedback: str | None) -> AcceptanceRevision:
        if not isinstance(value, Mapping) or set(value) != {
            "reason",
            "pr_evidence_digest",
            "observation",
            "diff_digest",
            "feedback_digest",
        }:
            raise ValueError("candidate revision payload differs")
        if (
            not isinstance(value["reason"], str)
            or not isinstance(value["pr_evidence_digest"], str)
            or not isinstance(value["diff_digest"], str)
            or (
                value["feedback_digest"] is not None
                and not isinstance(value["feedback_digest"], str)
            )
        ):
            raise ValueError("candidate revision payload types differ")
        return cls(
            value["reason"],
            value["pr_evidence_digest"],
            CandidateInspection.from_payload(value["observation"]),
            value["diff_digest"],
            value["feedback_digest"],
            feedback,
        )
