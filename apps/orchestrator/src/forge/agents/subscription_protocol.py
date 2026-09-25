"""Strict model-facing JSON, converted to existing Forge domain decisions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from forge.application.ports.subscription_gateway import (
    SubscriptionDecision,
    SubscriptionInvocationRequest,
    SubscriptionInvocationResult,
    _freeze,
)
from forge.application.ports.tool_schemas import GIT_DIFF_SCOPES, TOOL_ARGUMENT_SCHEMAS
from forge.domain.plan import ScopedPlanOutput
from forge.domain.run import RunState
from forge.domain.subscription import (
    AcceptDecision,
    BoundReassignDecision,
    BoundScopeResponseDecision,
    DelegateDecision,
    ForwardFeedbackDecision,
    LogicalTaskContract,
    ReassignDecision,
    ReviewedTaskHandoff,
    ReviewSelection,
    ScopeRequestDecision,
    SpecialistPurpose,
    TaskHandoff,
    WaitDecision,
    validate_task_dag,
)
from forge.domain.subscription_delegation import validate_child_authority
from forge.domain.tool import ToolName
from pydantic import TypeAdapter


class ProtocolError(RuntimeError):
    """Malformed, foreign or inadmissible provider data; messages contain no payload."""


def freeze_context(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProtocolError("context must be an object")
    try:
        return cast(Mapping[str, object], _freeze(value))
    except TypeError, ValueError, RecursionError:
        raise ProtocolError("context is invalid or exceeds its bound") from None


def json_value(value: object) -> Any:
    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON key")
        result[key] = value
    return result


def parse_json(text: str) -> Mapping[str, object]:
    try:
        if len(text.encode("utf-8")) > 1_048_576:
            raise ValueError
        value = json.loads(text, object_pairs_hook=_pairs)
        return freeze_context(value)
    except TypeError, ValueError, RecursionError:
        raise ProtocolError("invalid structured JSON") from None


def _record[T](kind: type[T], payload: Mapping[str, object]) -> T:
    try:
        return TypeAdapter(kind).validate_json(
            json.dumps(json_value(freeze_context(payload)), allow_nan=False),
            strict=True,
            extra="forbid",
        )
    except TypeError, ValueError:
        raise ProtocolError("invalid structured decision") from None


def tool_input_schema(tool: ToolName) -> dict[str, Any]:
    required, optional = TOOL_ARGUMENT_SCHEMAS[tool]
    properties: dict[str, Any] = {key: {"type": "string"} for key in sorted(required | optional)}
    if tool is ToolName.GIT_DIFF:
        properties["scope"] = {
            "type": "string",
            "enum": list(GIT_DIFF_SCOPES),
            "description": "snapshot records a dirty working-tree manifest; candidate requires a clean committed tree.",
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": sorted(required),
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderToolCall:
    call_key: str
    thread_id: str
    turn_id: str
    name: str
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        for value in (self.call_key, self.thread_id, self.turn_id, self.name):
            if (
                type(value) is not str
                or not value
                or len(value.encode("utf-8")) > 255
                or any(ord(c) < 32 for c in value)
            ):
                raise ProtocolError("invalid tool callback identity")
        object.__setattr__(self, "arguments", freeze_context(self.arguments))


def decode_tool_call(payload: object, *, expected_namespace: str | None = None) -> ProviderToolCall:
    if not isinstance(payload, Mapping) or set(payload) - {
        "callId",
        "threadId",
        "turnId",
        "tool",
        "arguments",
        "namespace",
    }:
        raise ProtocolError("invalid tool callback shape")
    if payload.get("namespace") != expected_namespace:
        raise ProtocolError("unregistered tool namespace")
    try:
        return ProviderToolCall(
            call_key=payload["callId"],
            thread_id=payload["threadId"],
            turn_id=payload["turnId"],
            name=payload["tool"],
            arguments=payload["arguments"],
        )
    except KeyError, TypeError, ValueError:
        raise ProtocolError("invalid tool callback") from None


_DECISIONS: dict[str, type[Any]] = {
    "handoff": TaskHandoff,
    "wait": WaitDecision,
    "scope_request": ScopeRequestDecision,
    "scope_response": BoundScopeResponseDecision,
    "accept": AcceptDecision,
    "reassign": BoundReassignDecision,
    "review_selection": ReviewSelection,
    "forward_feedback": ForwardFeedbackDecision,
}
_PRIMARY_KINDS = frozenset(
    {
        "plan",
        "delegate",
        "wait",
        "scope_response",
        "accept",
        "reassign",
        "review_selection",
        "forward_feedback",
    }
)
_CHILD_TARGET_KINDS = frozenset(
    {
        "accept",
        "reassign",
        "scope_response",
        "forward_feedback",
    }
)


def _child(payload: object, request: SubscriptionInvocationRequest) -> LogicalTaskContract:
    if not isinstance(payload, Mapping) or set(payload) & {"route", "run_id", "parent_task_id"}:
        raise ProtocolError("child authority must come from Forge")
    try:
        purpose = SpecialistPurpose(payload["purpose"])
        if purpose is SpecialistPurpose.PRIMARY:
            raise ValueError
        binding = request.envelope.route_for(purpose)
        child = _record(
            LogicalTaskContract,
            {
                **payload,
                "run_id": str(request.attempt.run_id),
                "parent_task_id": str(request.attempt.task_id),
                "route": TypeAdapter(type(binding)).dump_python(binding, mode="json"),
                "budget": payload.get(
                    "budget",
                    TypeAdapter(type(request.task.budget)).dump_python(
                        request.task.budget, mode="json"
                    ),
                ),
            },
        )
        validate_child_authority(request.task, child, request.envelope)
        return child
    except KeyError, TypeError, ValueError:
        raise ProtocolError("child exceeds approved contract") from None


def _phase_allows(kind: str, request: SubscriptionInvocationRequest) -> bool:
    if request.run_state is None:
        return True
    if kind == "plan":
        return request.run_state is RunState.PLANNING
    return not (
        request.task.purpose is SpecialistPurpose.PRIMARY and request.run_state is RunState.PLANNING
    )


def decode_final(
    payload: object, request: SubscriptionInvocationRequest
) -> SubscriptionInvocationResult:
    if not isinstance(payload, Mapping):
        raise ProtocolError("final result must be an object")
    payload = dict(freeze_context(payload))
    kind = payload.pop("kind", None)
    if not isinstance(kind, str):
        raise ProtocolError("missing decision kind")
    pending_feedback = request.untrusted_context.get("pending_worker_feedback")
    if pending_feedback is not None and kind != "forward_feedback":
        raise ProtocolError("pending worker feedback must be forwarded exactly")
    if kind in _PRIMARY_KINDS and request.task.purpose is not SpecialistPurpose.PRIMARY:
        raise ProtocolError("only the primary may make this decision")
    if not _phase_allows(kind, request):
        raise ProtocolError("decision is not allowed in this run phase")
    for name, expected in (
        ("run_id", request.attempt.run_id),
        ("attempt_id", request.attempt.attempt_id),
    ):
        if name in payload and payload.pop(name) != str(expected):
            raise ProtocolError("foreign decision identity")
    target = request.task
    target_id = payload.pop("task_id", str(request.task.task_id))
    if target_id != str(request.task.task_id):
        if kind not in _CHILD_TARGET_KINDS:
            raise ProtocolError("foreign decision task")
        matches = [task for task in request.known_tasks if str(task.task_id) == target_id]
        if len(matches) != 1:
            raise ProtocolError("unknown decision target")
        target = matches[0]
    decision: SubscriptionDecision
    if kind == "plan":
        if set(payload) != {"plan"}:
            raise ProtocolError("invalid plan decision shape")
        decision = _record(ScopedPlanOutput, cast(Mapping[str, object], payload["plan"]))
    elif kind == "delegate":
        if set(payload) != {"children", "rationale"} or not isinstance(
            payload["children"], (tuple, list)
        ):
            raise ProtocolError("invalid delegation shape")
        children = tuple(_child(child, request) for child in payload["children"])
        try:
            validate_task_dag((request.task, *request.known_tasks, *children))
            decision = DelegateDecision(
                run_id=request.attempt.run_id,
                parent_task_id=request.attempt.task_id,
                child_tasks=children,
                rationale=cast(str, payload["rationale"]),
            )
        except TypeError, ValueError:
            raise ProtocolError("invalid delegation graph") from None
    else:
        record_type = _DECISIONS.get(kind)
        if record_type is None:
            raise ProtocolError("unknown decision kind")
        payload["run_id"] = str(request.attempt.run_id)
        if record_type is not ReviewSelection:
            payload["task_id"] = str(target.task_id)
        if record_type is TaskHandoff:
            if request.task.purpose is SpecialistPurpose.INDEPENDENT_REVIEW:
                record_type = ReviewedTaskHandoff
            payload["attempt_id"] = str(request.attempt.attempt_id)
        decision = _record(record_type, payload)
        if isinstance(decision, WaitDecision):
            known_child_ids = {
                task.task_id
                for task in request.known_tasks
                if task.parent_task_id == request.task.task_id
            }
            if (
                len(decision.waiting_on_task_ids) > 64
                or not set(decision.waiting_on_task_ids) <= known_child_ids
            ):
                raise ProtocolError("wait targets must be known direct children")
        if isinstance(decision, ReassignDecision):
            approved = request.envelope.route_for(target.purpose)
            if (
                target.parent_task_id != request.task.task_id
                or target.purpose is SpecialistPurpose.PRIMARY
                or decision.new_route
                not in (
                    approved.effective,
                    *request.envelope.fallbacks_for(target.purpose),
                )
            ):
                raise ProtocolError("unapproved reassignment route")
            if not decision.preserve_partial_work:
                raise ProtocolError("reassignment must preserve partial work")
            assert isinstance(decision, BoundReassignDecision)
            outcomes = request.untrusted_context.get("task_outcomes", ())
            observed = (
                [
                    outcome
                    for outcome in outcomes
                    if isinstance(outcome, Mapping)
                    and outcome.get("task_id") == str(target.task_id)
                ]
                if isinstance(outcomes, (tuple, list))
                else []
            )
            if len(observed) != 1 or observed[0].get("version") != decision.expected_task_version:
                raise ProtocolError("reassignment must name the observed task version")
        if isinstance(decision, ForwardFeedbackDecision) and (
            target.parent_task_id != request.task.task_id
            or target.purpose is SpecialistPurpose.PRIMARY
            or not isinstance(pending_feedback, Mapping)
            or pending_feedback.get("run_id") != str(decision.run_id)
            or pending_feedback.get("primary_task_id") != str(request.task.task_id)
            or pending_feedback.get("task_id") != str(decision.task_id)
            or pending_feedback.get("receipt_id") != str(decision.feedback_receipt_id)
            or pending_feedback.get("feedback_digest") != decision.feedback_digest
        ):
            raise ProtocolError("feedback forwarding does not match the durable request")
        if isinstance(decision, ReviewSelection) and decision.reviewer_route is not None:
            approved = request.envelope.route_for(SpecialistPurpose.INDEPENDENT_REVIEW)
            if decision.reviewer_route not in (
                approved.effective,
                *request.envelope.fallbacks_for(SpecialistPurpose.INDEPENDENT_REVIEW),
            ):
                raise ProtocolError("unapproved reviewer route")
    return SubscriptionInvocationResult(attempt=request.attempt, decision=decision)


def output_schema(request: SubscriptionInvocationRequest) -> dict[str, Any]:
    """Advertise complete fields; runtime validation additionally binds authority."""
    variants = dict(_DECISIONS)
    feedback_only = request.untrusted_context.get("pending_worker_feedback") is not None
    if feedback_only:
        variants = {"forward_feedback": ForwardFeedbackDecision}
    if request.task.purpose is SpecialistPurpose.INDEPENDENT_REVIEW:
        variants["handoff"] = ReviewedTaskHandoff
    variants = {key: value for key, value in variants.items() if _phase_allows(key, request)}
    if request.task.purpose is not SpecialistPurpose.PRIMARY:
        variants = {key: value for key, value in variants.items() if key not in _PRIMARY_KINDS}
    definitions: dict[str, Any] = {}
    choices: list[dict[str, Any]] = []
    for kind, record_type in variants.items():
        schema = TypeAdapter(record_type).json_schema()
        definitions.update(schema.pop("$defs", {}))
        props = schema["properties"]
        for authority in ("run_id", "task_id", "attempt_id"):
            if authority == "task_id" and kind in _CHILD_TARGET_KINDS:
                continue
            props.pop(authority, None)
        # Select the union branch before emitting its fields. Strict providers
        # preserve key order; only plan/delegate previously put kind first.
        schema["properties"] = {"kind": {"const": kind, "type": "string"}, **props}
        schema["required"] = [key for key in schema.get("required", []) if key in props] + ["kind"]
        schema["additionalProperties"] = False
        choices.append(schema)
    if (
        not feedback_only
        and request.task.purpose is SpecialistPurpose.PRIMARY
        and _phase_allows("plan", request)
    ):
        plan = TypeAdapter(ScopedPlanOutput).json_schema()
        named_checks = request.untrusted_context.get("named_checks")
        if (
            isinstance(named_checks, (list, tuple))
            and named_checks
            and all(isinstance(name, str) for name in named_checks)
        ):
            plan["properties"]["required_checks"]["items"] = {
                "type": "string",
                "enum": list(named_checks),
            }
        definitions.update(plan.pop("$defs", {}))
        choices.append(
            {
                "type": "object",
                "properties": {"kind": {"const": "plan", "type": "string"}, "plan": plan},
                "required": ["kind", "plan"],
                "additionalProperties": False,
            }
        )
    if (
        not feedback_only
        and request.task.purpose is SpecialistPurpose.PRIMARY
        and _phase_allows("delegate", request)
    ):
        child = TypeAdapter(LogicalTaskContract).json_schema()
        definitions.update(child.pop("$defs", {}))
        for authority in ("run_id", "parent_task_id", "route"):
            child["properties"].pop(authority, None)
        child["required"] = [key for key in child.get("required", []) if key in child["properties"]]
        child["additionalProperties"] = False
        choices.append(
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "delegate", "type": "string"},
                    "rationale": {"type": "string"},
                    "children": {"type": "array", "minItems": 1, "maxItems": 64, "items": child},
                },
                "required": ["kind", "rationale", "children"],
                "additionalProperties": False,
            }
        )
    schema = {
        "type": "object",
        "properties": {"decision": {"anyOf": choices}},
        "required": ["decision"],
        "additionalProperties": False,
        "$defs": definitions,
    }
    _strict_output_objects(schema)
    return schema


def _strict_output_objects(value: Any) -> None:
    """Use the provider's strict subset; optional values remain explicitly nullable."""
    if isinstance(value, dict):
        value.pop("default", None)
        if "oneOf" in value:
            value["anyOf"] = value.pop("oneOf")
        if value.get("type") == "object":
            value["additionalProperties"] = False
            value["required"] = list(value.get("properties", {}))
        for child in value.values():
            _strict_output_objects(child)
    elif isinstance(value, list):
        for child in value:
            _strict_output_objects(child)


def tool_result_frame(
    call: ProviderToolCall, result: Mapping[str, object], request_id: str | int
) -> dict[str, object]:
    if type(request_id) not in (str, int):
        raise ProtocolError("invalid provider request id")
    return {
        "id": request_id,
        "result": {
            "contentItems": [
                {
                    "type": "inputText",
                    "text": json.dumps(json_value(freeze_context(result)), allow_nan=False),
                }
            ],
            "success": result.get("status") == "succeeded",
        },
    }
