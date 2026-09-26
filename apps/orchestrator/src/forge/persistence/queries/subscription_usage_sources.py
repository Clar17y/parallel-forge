"""Exact source joins shared by assessment and historical acceptance attribution."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import String, and_, case, cast, func, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select

from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionDecisionRecord,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.queries.subscription_usage_proofs import RECORD_PREFIXES


@dataclass(frozen=True)
class UsageSources:
    attempt: type[SubscriptionAttempt]
    task: type[SubscriptionTask]
    result: type[SubscriptionAttemptResult]
    consumption: type[SubscriptionAttemptConsumption]
    launch: type[SubscriptionClientLaunch]
    record: type[SubscriptionDecisionRecord]

    @classmethod
    def named(cls, name: str) -> UsageSources:
        return cls(
            aliased(SubscriptionAttempt, name=f"{name}_attempt"),
            aliased(SubscriptionTask, name=f"{name}_task"),
            aliased(SubscriptionAttemptResult, name=f"{name}_result"),
            aliased(SubscriptionAttemptConsumption, name=f"{name}_consumption"),
            aliased(SubscriptionClientLaunch, name=f"{name}_launch"),
            aliased(SubscriptionDecisionRecord, name=f"{name}_record"),
        )

    def columns(self) -> tuple[Any, ...]:
        # A bounded index lookup within the streaming SQL statement, not one
        # round trip per attempt and not a global launch-history materialization.
        count = (
            select(func.count())
            .select_from(SubscriptionClientLaunch)
            .where(
                SubscriptionClientLaunch.attempt_id == self.attempt.id,
            )
            .correlate(self.attempt)
            .scalar_subquery()
        )
        return (
            self.attempt,
            self.task,
            self.result,
            self.consumption,
            self.launch,
            count,
            self.record,
        )

    def join(self, statement: Select[Any], *, optional: bool = False) -> Select[Any]:
        decision_type = self.result.result_payload["decision"]["record"]["$record"].astext
        key = case(RECORD_PREFIXES, value=decision_type) + ":" + cast(self.attempt.id, String)
        return (
            statement.join(
                self.task,
                and_(
                    self.task.run_id == self.attempt.run_id,
                    self.task.id == self.attempt.task_row_id,
                ),
                isouter=optional,
            )
            .outerjoin(self.result, self.result.attempt_id == self.attempt.id)
            .outerjoin(self.consumption, self.consumption.attempt_id == self.attempt.id)
            .outerjoin(
                self.launch,
                and_(
                    self.launch.attempt_id == self.attempt.id,
                    self.launch.launch_id
                    == self.result.result_payload["launch_proof"]["launch_id"].astext,
                ),
            )
            .outerjoin(
                self.record,
                and_(
                    self.record.run_id == self.attempt.run_id,
                    self.record.idempotency_key == key,
                    self.record.attempt_id == self.attempt.id,
                    self.record.task_row_id == self.attempt.task_row_id,
                ),
            )
        )
