"""Read-only source/application lineage checks; never new execution authority."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.subscription_gateway import (
    SubscriptionDecision,
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.ports.subscription_handoff import handoff_claim_error
from forge.domain.operation import canonical_digest
from forge.domain.plan import PlanOutput, ScopedPlanOutput
from forge.domain.subscription import (
    AcceptDecision,
    AttemptIdentity,
    AttemptTelemetry,
    BoundReassignDecision,
    BoundScopeResponseDecision,
    DelegateDecision,
    ExecutionEnvelope,
    ForwardFeedbackDecision,
    HandoffStatus,
    LogicalTaskContract,
    ReassignDecision,
    ReviewSelection,
    ScopeRequestDecision,
    ScopeResponseDecision,
    SpecialistPurpose,
    TaskHandoff,
    WaitDecision,
    decode_subscription_record,
    encode_subscription_record,
)
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionDecisionRecord,
    SubscriptionEnvelope,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.repositories.subscription_launch import launches_confirmed

RECORD_PREFIXES = {
    "DelegateDecision": "delegation",
    "WaitDecision": "wait",
    "TaskHandoff": "handoff",
    "ReviewedTaskHandoff": "handoff",
    "ScopeRequestDecision": "scope-request",
    "BoundScopeResponseDecision": "scope-response",
    "BoundReassignDecision": "reassignment",
    "ForwardFeedbackDecision": "feedback-forward",
    "ReviewSelection": "review-selection",
    "AcceptDecision": "acceptance",
}
_EXPECTED_DISPOSITIONS = {
    "DelegateDecision": {"delegated"},
    "WaitDecision": {"waiting"},
    "TaskHandoff": {"handoff_completed"},
    "ReviewedTaskHandoff": {"handoff_completed"},
    "ScopeRequestDecision": {"scope_requested"},
    "BoundScopeResponseDecision": {"scope_responded"},
    "BoundReassignDecision": {"reassigned"},
    "ForwardFeedbackDecision": {"feedback_forwarded"},
    "ReviewSelection": {"candidate_prepared", "review_selected"},
    "AcceptDecision": {"task_accepted", "acceptance_prepared"},
}
APPLIED_DISPOSITIONS = frozenset(
    value for values in _EXPECTED_DISPOSITIONS.values() for value in values
)


@dataclass(frozen=True)
class HistoricalSource:
    contract: LogicalTaskContract
    envelope: ExecutionEnvelope
    decision: SubscriptionDecision | None
    failed: bool


@dataclass(frozen=True)
class UsageEvidence:
    """One row of the shared exact joins, held only for its streaming batch."""

    attempt: SubscriptionAttempt
    task: SubscriptionTask
    result: SubscriptionAttemptResult | None
    consumption: SubscriptionAttemptConsumption | None
    launch: SubscriptionClientLaunch | None
    launch_count: int
    record: SubscriptionDecisionRecord | None

    def source(self, envelope: SubscriptionEnvelope | None) -> HistoricalSource | None:
        return historical_source(
            self.attempt,
            self.task,
            self.result,
            self.consumption,
            envelope,
            self.launch,
            self.launch_count,
        )

    def applied(self, source: HistoricalSource | None) -> bool:
        return applied_decision(source, self.attempt, self.result, self.record)


def _decode(value: object) -> object:
    if not isinstance(value, Mapping):
        raise TypeError
    return decode_subscription_record(value)


def historical_source(
    attempt: SubscriptionAttempt,
    task: SubscriptionTask,
    result: SubscriptionAttemptResult | None,
    consumption: SubscriptionAttemptConsumption | None,
    envelope: SubscriptionEnvelope | None,
    launch: SubscriptionClientLaunch | None,
    launch_count: int,
) -> HistoricalSource | None:
    if result is None or envelope is None:
        return None
    try:
        payload = result.result_payload
        context = payload["proposal_context"]
        if not isinstance(context, Mapping):
            raise TypeError
        identity = _decode(payload["attempt"])
        contract = _decode(context["task"])
        frozen = _decode(context["envelope"])
        telemetry = _decode(payload["telemetry"])
        if (
            type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 4
            or canonical_digest(payload) != result.result_digest
            or not isinstance(identity, AttemptIdentity)
            or (identity.run_id, identity.task_id, identity.attempt_id, identity.attempt_number)
            != (attempt.run_id, attempt.task_row_id, attempt.id, attempt.attempt_number)
            or not isinstance(contract, LogicalTaskContract)
            or (contract.run_id, contract.task_id, contract.parent_task_id)
            != (task.run_id, task.id, task.parent_task_id)
            or not isinstance(frozen, ExecutionEnvelope)
            or frozen.run_id != attempt.run_id
            or _decode(envelope.payload) != frozen
            or not frozen.permits_route(contract.purpose, contract.route)
            or not isinstance(telemetry, AttemptTelemetry)
            or encode_subscription_record(telemetry) != attempt.telemetry_payload
            or consumption is None
            or consumption.telemetry_payload != attempt.telemetry_payload
            or canonical_digest(context["task"]) != attempt.task_digest
            or canonical_digest(context["envelope"]) != attempt.envelope_digest
            or context["route"] != attempt.route_payload
            or context["route"] != encode_subscription_record(contract.route)
            or context["budget"] != encode_subscription_record(contract.budget)
            or context["candidate_epoch"] != attempt.candidate_epoch
            or context["task_version"] != attempt.task_version
        ):
            raise ValueError
        failures = (payload["failure"], payload["effective_failure"])
        for value in failures:
            if value is not None:
                if not isinstance(value, str):
                    raise TypeError
                SubscriptionFailure(value)
        failed = any(value is not None for value in failures)
        value = payload["decision"]
        decision: SubscriptionDecision | None
        if value is None:
            if not failed:
                raise ValueError
            decision = None
        elif isinstance(value, Mapping) and value.get("type") == "PlanOutput":
            plan = value["value"]
            if not isinstance(plan, Mapping):
                raise ValueError
            decision = (ScopedPlanOutput if "owned_paths" in plan else PlanOutput).model_validate(
                plan
            )
        else:
            decoded = _decode(value)
            # Constructing a neutral result validates the decision's typed run,
            # source task/attempt and permissible target identity semantics.
            if not isinstance(
                decoded,
                (
                    TaskHandoff,
                    DelegateDecision,
                    WaitDecision,
                    ScopeRequestDecision,
                    ScopeResponseDecision,
                    ReassignDecision,
                    AcceptDecision,
                    ReviewSelection,
                    ForwardFeedbackDecision,
                ),
            ):
                raise TypeError
            invocation = SubscriptionInvocationResult(attempt=identity, decision=decoded)
            decision = invocation.decision
        expected = payload.get("launch_proof")
        proof = (
            None if expected is None else SubscriptionLaunchTerminalProof.model_validate(expected)
        )
        if not failed:
            if (
                launch_count != 1
                or launch is None
                or not launches_confirmed(
                    [launch],
                    proof,
                    require_decision=True,
                    worker_identity=attempt.lease_owner,
                )
            ):
                raise ValueError
        elif proof is not None and (
            launch_count != 1
            or launch is None
            or not launches_confirmed(
                [launch],
                proof,
                require_decision=False,
                worker_identity=attempt.lease_owner,
            )
        ):
            raise ValueError
        return HistoricalSource(contract, frozen, decision, failed)
    except KeyError, TypeError, ValueError:
        # Legacy, redacted or inconsistent records remain inspectable as
        # unverified. They cannot silently become verified task successes.
        return None


def applied_decision(
    source: HistoricalSource | None,
    attempt: SubscriptionAttempt,
    result: SubscriptionAttemptResult | None,
    record: SubscriptionDecisionRecord | None,
) -> bool:
    if source is None or source.failed or result is None or record is None:
        return False
    decision = source.decision
    prefix = RECORD_PREFIXES.get(type(decision).__name__)
    if (
        prefix is None
        or not result.accepted
        or result.disposition not in _EXPECTED_DISPOSITIONS.get(type(decision).__name__, ())
        or attempt.status != "terminal"
        or (record.run_id, record.task_row_id, record.attempt_id)
        != (attempt.run_id, attempt.task_row_id, attempt.id)
        or record.idempotency_key != f"{prefix}:{attempt.id}"
        or record.record_type != type(decision).__name__
        or record.payload != encode_subscription_record(decision)
        or record.created_at < attempt.created_at
    ):
        return False
    worker = isinstance(decision, (TaskHandoff, ScopeRequestDecision))
    if worker != (
        source.contract.purpose is not SpecialistPurpose.PRIMARY
        and source.contract.parent_task_id is not None
    ):
        return False
    if not worker and (
        source.contract.purpose is not SpecialistPurpose.PRIMARY
        or source.contract.parent_task_id is not None
    ):
        return False
    if (
        isinstance(decision, (DelegateDecision, WaitDecision, ScopeRequestDecision))
        and result.application_payload is None
    ):
        return result.application_digest is None
    receipt = result.application_payload
    if (
        not isinstance(receipt, dict)
        or type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != 1
        or result.application_digest != canonical_digest(receipt)
    ):
        return False
    try:
        return _application_matches(source, attempt, result, receipt)
    except KeyError, TypeError, ValueError:
        return False


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _uuid(value: object) -> bool:
    return isinstance(value, str) and str(UUID(value)) == value and UUID(value).int != 0


def _application_matches(
    source: HistoricalSource,
    attempt: SubscriptionAttempt,
    result: SubscriptionAttemptResult,
    receipt: dict[str, object],
) -> bool:
    """Validate historical receipt shape/bindings; do not re-run current effects."""
    decision = source.decision
    if isinstance(decision, DelegateDecision):
        return (
            receipt.get("kind") == "candidate_reopened"
            and _uuid(receipt["selection_attempt_id"])
            and _digest(receipt["selection_application_digest"])
            and type(receipt["source_epoch"]) is int
            and receipt["source_epoch"] == attempt.candidate_epoch
            and attempt.candidate_epoch is not None
            and receipt["reopened_epoch"] == attempt.candidate_epoch + 1
        )
    if isinstance(decision, (BoundScopeResponseDecision, BoundReassignDecision)):
        updated = _decode(receipt["updated_task"])
        if (
            receipt.get("response_result_digest") != result.result_digest
            or receipt["child_task_id"] != str(decision.task_id)
            or type(receipt["child_version"]) is not int
            or receipt["child_version"] < 0
            or not _digest(receipt["prior_task_digest"])
            or not isinstance(updated, LogicalTaskContract)
            or (updated.run_id, updated.task_id, updated.parent_task_id)
            != (attempt.run_id, decision.task_id, attempt.task_row_id)
            or not source.envelope.permits_route(updated.purpose, updated.route)
        ):
            return False
        if isinstance(decision, BoundReassignDecision):
            return (
                receipt.get("kind") == "reassignment"
                and decision.preserve_partial_work
                and receipt["source_attempt_id"] == str(decision.source_attempt_id)
                and _digest(receipt["source_result_digest"])
                and _digest(receipt["source_history_digest"])
                and receipt["child_version"] == decision.expected_task_version
                and updated.route.effective == decision.new_route
            )
        return (
            receipt.get("kind") == "scope_response"
            and receipt["request_attempt_id"] == str(decision.request_attempt_id)
            and _digest(receipt["request_result_digest"])
            and all(path in updated.owned_paths for path in decision.granted_paths)
        )
    if isinstance(decision, ForwardFeedbackDecision):
        return (
            receipt.get("kind") == "feedback_forwarded"
            and receipt.get("result_digest") == result.result_digest
            and receipt.get("feedback_receipt_id") == str(decision.feedback_receipt_id)
            and receipt.get("feedback_digest") == decision.feedback_digest
            and receipt.get("target_task_id") == str(decision.task_id)
            and _digest(receipt.get("binding_digest"))
            and _nonnegative_int(receipt.get("observed_run_version"))
            and _nonnegative_int(receipt.get("observed_task_version"))
            and receipt.get("delivery")
            in {
                "retained",
                "queued",
                "paused",
                "after_current_attempt",
                "accepted",
                "cancelled",
                "budget_exhausted",
            }
        )
    if receipt.get("result_digest") != result.result_digest:
        return False
    if isinstance(decision, TaskHandoff):
        expires = receipt["observation_expires_at"]
        return (
            receipt.get("kind") == "completed_handoff"
            and decision.status is HandoffStatus.COMPLETED
            and handoff_claim_error(decision, source.contract) is None
            and all(
                _digest(receipt[name])
                for name in (
                    "verified_evidence_digest",
                    "current_tree_digest",
                    "output_digest",
                    "manifest_digest",
                )
            )
            and _uuid(receipt["observation_id"])
            and isinstance(expires, str)
            and datetime.fromisoformat(expires).utcoffset() is not None
            and type(receipt["candidate_epoch"]) is int
            and receipt["candidate_epoch"] == attempt.candidate_epoch
            and type(receipt["task_version"]) is int
            and attempt.task_version is not None
            and receipt["task_version"] >= attempt.task_version
        )
    if isinstance(decision, ReviewSelection):
        if not (
            receipt.get("kind") == "candidate_intent"
            and attempt.candidate_epoch is not None
            and type(receipt["candidate_epoch"]) is int
            and receipt["candidate_epoch"] == attempt.candidate_epoch + 1
            and receipt["proposed_commit"] == decision.candidate_commit
            and receipt["proposed_tree_digest"] == decision.candidate_tree_digest
        ):
            return False
        if result.disposition == "review_selected":
            observed = CandidateInspection.from_payload(receipt["observation"])
            selection = receipt["selection"]
            if (
                not isinstance(selection, dict)
                or selection.get("review_required") is not decision.review_required
                or observed.tree_digest != decision.candidate_tree_digest
                or (
                    decision.candidate_commit is not None
                    and observed.head_sha != decision.candidate_commit
                )
            ):
                return False
            child = selection["review_task"]
            if not decision.review_required:
                return child is None
            reviewer = _decode(child)
            return (
                isinstance(reviewer, LogicalTaskContract)
                and reviewer.parent_task_id == attempt.task_row_id
                and reviewer.run_id == attempt.run_id
                and reviewer.route.effective == decision.reviewer_route
            )
        return True
    if isinstance(decision, AcceptDecision):
        if result.disposition == "task_accepted":
            # The streaming cross-source pass additionally verifies the exact
            # child result/application, ownership and retained candidate claims.
            return (
                receipt.get("kind") == "task_acceptance"
                and receipt["primary_task_id"] == str(attempt.task_row_id)
                and receipt["target_task_id"] == str(decision.task_id)
            )
        review = receipt["review_sources"]
        if not isinstance(review, dict):
            return False
        candidate = CandidateInspection.from_payload(review["candidate"])
        return (
            receipt.get("kind") == "acceptance_intent"
            and decision.task_id == attempt.task_row_id
            and review["run_id"] == str(attempt.run_id)
            and review["primary_task_id"] == str(attempt.task_row_id)
            and review["candidate_epoch"] == receipt["candidate_epoch"] == attempt.candidate_epoch
            and candidate.tree_digest == decision.candidate_tree_digest
            and (
                decision.candidate_commit is None or candidate.head_sha == decision.candidate_commit
            )
            and receipt["receipt_claims"] == list(decision.evidence_receipt_ids)
            and _uuid(review["selection_attempt_id"])
            and _digest(review["selection_result_digest"])
            and _digest(review["selection_application_digest"])
        )
    return False
