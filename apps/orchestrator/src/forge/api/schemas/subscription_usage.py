"""Measured subscription subtotals with explicit coverage and result counts."""

from math import isclose
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from forge.api.schemas.subscription_tasks import AttemptRoute

NonnegativeInt = Annotated[int, Field(ge=0, strict=True)]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubscriptionUsageMetric(_Closed):
    known_total: NonnegativeInt | None
    measured_attempts: NonnegativeInt
    unknown_attempts: NonnegativeInt

    @model_validator(mode="after")
    def measurement_has_coverage(self) -> Self:
        if (self.known_total is None) != (self.measured_attempts == 0):
            raise ValueError("usage subtotal and coverage disagree")
        return self


class SubscriptionUsageItem(_Closed):
    project_id: UUID
    run_id: UUID
    purpose: str
    effective_route: AttemptRoute
    currency: str | None
    attempts: Annotated[int, Field(ge=1, strict=True)]
    recorded_results: NonnegativeInt
    failed_results: NonnegativeInt
    pending_results: NonnegativeInt
    input_tokens: SubscriptionUsageMetric
    output_tokens: SubscriptionUsageMetric
    cached_input_tokens: SubscriptionUsageMetric
    duration_ms: SubscriptionUsageMetric
    tool_calls: SubscriptionUsageMetric
    named_checks: SubscriptionUsageMetric
    estimated_api_cost_minor: SubscriptionUsageMetric

    @model_validator(mode="after")
    def counts_describe_attempts(self) -> Self:
        if self.recorded_results + self.pending_results != self.attempts:
            raise ValueError("usage result counts disagree")
        if self.failed_results > self.recorded_results:
            raise ValueError("usage failure count exceeds recorded results")
        for metric in (
            self.input_tokens,
            self.output_tokens,
            self.cached_input_tokens,
            self.duration_ms,
            self.tool_calls,
            self.named_checks,
            self.estimated_api_cost_minor,
        ):
            if metric.measured_attempts + metric.unknown_attempts != self.attempts:
                raise ValueError("usage measurement coverage disagrees with attempt count")
        if self.currency is None and self.estimated_api_cost_minor.known_total is not None:
            raise ValueError("usage cost requires an identified currency")
        return self


class SubscriptionUsagePage(_Closed):
    items: list[SubscriptionUsageItem] = Field(max_length=100)
    has_more: StrictBool
    assessment: SubscriptionUsageAssessment | None = None

    @model_validator(mode="after")
    def assessment_details_match_page(self) -> Self:
        if self.assessment is not None:

            def key(item: SubscriptionUsageItem | SubscriptionUsageOutcome) -> tuple[object, ...]:
                return (
                    item.project_id,
                    item.run_id,
                    item.purpose,
                    item.effective_route,
                    item.currency,
                )

            if (
                self.assessment.outcomes_has_more != self.has_more
                or [key(item) for item in self.items]
                != [key(item) for item in self.assessment.outcomes]
                or any(
                    outcome.attempts > item.attempts
                    for item, outcome in zip(self.items, self.assessment.outcomes, strict=True)
                )
            ):
                raise ValueError("assessment details differ from the complete usage group page")
        return self


class SubscriptionUsageShare(_Closed):
    numerator: NonnegativeInt | None
    denominator: NonnegativeInt | None
    primary_attempts: NonnegativeInt
    all_attempts: NonnegativeInt
    numerator_measured_attempts: NonnegativeInt
    denominator_measured_attempts: NonnegativeInt
    numerator_unknown_attempts: NonnegativeInt
    denominator_unknown_attempts: NonnegativeInt
    coverage: Annotated[float, Field(ge=0, le=1, strict=True)] | None
    share: Annotated[float, Field(ge=0, le=1, strict=True)] | None

    @model_validator(mode="after")
    def counts_and_shares_match(self) -> Self:
        if (
            self.primary_attempts > self.all_attempts
            or self.numerator_measured_attempts + self.numerator_unknown_attempts
            != self.primary_attempts
            or self.denominator_measured_attempts + self.denominator_unknown_attempts
            != self.all_attempts
            or self.numerator_measured_attempts > self.denominator_measured_attempts
            or self.numerator_unknown_attempts > self.denominator_unknown_attempts
            or (self.numerator is None) != (self.numerator_measured_attempts == 0)
            or (self.denominator is None) != (self.denominator_measured_attempts == 0)
            or (
                self.numerator is not None
                and (self.denominator is None or self.numerator > self.denominator)
            )
        ):
            raise ValueError("primary measurement and coverage disagree")
        expected_coverage = (
            self.denominator_measured_attempts / self.all_attempts if self.all_attempts else None
        )
        expected_share = (
            self.numerator / self.denominator
            if self.numerator is not None and self.denominator
            else None
        )
        for actual, expected in ((self.coverage, expected_coverage), (self.share, expected_share)):
            if (actual is None) != (expected is None) or (
                actual is not None
                and expected is not None
                and not isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12)
            ):
                raise ValueError("primary share or coverage differs from measured counts")
        return self


class SubscriptionUsageShares(_Closed):
    input_tokens: SubscriptionUsageShare
    output_tokens: SubscriptionUsageShare
    duration_ms: SubscriptionUsageShare


class SubscriptionUsageOutcome(_Closed):
    project_id: UUID
    run_id: UUID
    purpose: str
    effective_route: AttemptRoute
    currency: str | None
    attempts: NonnegativeInt
    distinct_tasks: NonnegativeInt
    terminal_tasks: NonnegativeInt
    verified_results: NonnegativeInt
    unverified_results: NonnegativeInt
    pending_results: NonnegativeInt
    failed_results: NonnegativeInt
    applied_decisions: NonnegativeInt
    completed_handoffs: NonnegativeInt
    task_acceptances: NonnegativeInt
    fallback_attempts: NonnegativeInt
    latest_fallback_reason: Annotated[str, Field(max_length=255)] | None
    latest_result_disposition: Annotated[str, Field(min_length=1, max_length=32)] | None

    @model_validator(mode="after")
    def outcome_counts_match(self) -> Self:
        if (
            self.distinct_tasks > self.attempts
            or self.terminal_tasks > self.distinct_tasks
            or self.verified_results + self.unverified_results + self.pending_results
            != self.attempts
            or max(self.failed_results, self.applied_decisions, self.completed_handoffs)
            > self.verified_results
            or self.task_acceptances > self.completed_handoffs
            or self.fallback_attempts > self.attempts
            or self.failed_results + self.applied_decisions > self.verified_results
            or self.completed_handoffs > self.applied_decisions
            or self.task_acceptances > self.distinct_tasks
            or (self.latest_result_disposition is None)
            != (self.verified_results + self.unverified_results == 0)
            or (self.latest_fallback_reason is None) != (self.fallback_attempts == 0)
        ):
            raise ValueError("outcome counts disagree")
        return self


class SubscriptionUsageWaits(_Closed):
    decisions: NonnegativeInt
    continued: NonnegativeInt
    unfinished: NonnegativeInt
    ended_without_continuation: NonnegativeInt
    measured_intervals: NonnegativeInt
    unknown_intervals: NonnegativeInt
    elapsed_ms: NonnegativeInt | None

    @model_validator(mode="after")
    def wait_counts_match(self) -> Self:
        if (
            self.continued + self.unfinished + self.ended_without_continuation != self.decisions
            or self.measured_intervals + self.unknown_intervals != self.decisions
            or self.measured_intervals > self.continued
            or (self.elapsed_ms is None) != (self.measured_intervals == 0)
        ):
            raise ValueError("wait interval coverage disagrees")
        return self


class SubscriptionUsageAssessment(_Closed):
    primary_turns: NonnegativeInt
    all_attempts: NonnegativeInt
    delegation_decisions: NonnegativeInt
    wait_decisions: NonnegativeInt
    repair_debits: NonnegativeInt
    fallback_attempts: NonnegativeInt
    preferred_attempts: NonnegativeInt
    unknown_route_attempts: NonnegativeInt
    unverified_decisions: NonnegativeInt
    shares: SubscriptionUsageShares
    waits: SubscriptionUsageWaits
    outcomes: list[SubscriptionUsageOutcome] = Field(max_length=100)
    outcomes_has_more: StrictBool

    @model_validator(mode="after")
    def assessment_counts_match(self) -> Self:
        if (
            self.primary_turns > self.all_attempts
            or self.repair_debits > self.all_attempts
            or self.unverified_decisions > self.all_attempts
            or self.delegation_decisions + self.wait_decisions > self.primary_turns
            or sum(item.attempts for item in self.outcomes) > self.all_attempts
            or self.fallback_attempts + self.preferred_attempts + self.unknown_route_attempts
            != self.all_attempts
            or self.waits.decisions != self.delegation_decisions + self.wait_decisions
        ):
            raise ValueError("assessment totals disagree")
        for metric in (
            self.shares.input_tokens,
            self.shares.output_tokens,
            self.shares.duration_ms,
        ):
            if (metric.primary_attempts, metric.all_attempts) != (
                self.primary_turns,
                self.all_attempts,
            ):
                raise ValueError("assessment measurement scope differs")
        return self


SubscriptionUsagePage.model_rebuild()
