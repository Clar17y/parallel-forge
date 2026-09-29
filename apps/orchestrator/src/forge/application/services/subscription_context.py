"""Bound approved plan prose inside the aggregate invocation context limit."""

from collections.abc import Mapping

from forge.application.ports.subscription_gateway import validate_subscription_context


def _fits(context: Mapping[str, object]) -> bool:
    try:
        validate_subscription_context(context)
    except ValueError as exc:
        if str(exc) != "subscription context exceeds its bound":
            raise
        return False
    return True


def _utf8_prefix(value: str, maximum: int) -> str:
    return value.encode("utf-8")[:maximum].decode("utf-8", errors="ignore")


def bounded_approved_context(
    context: dict[str, object], implementation: dict[str, object] | None
) -> dict[str, object]:
    """Keep full small plans; otherwise retain a verified provenance reference.

    The task contract carries the approved scope and checks. The full plan remains
    in the immutable gate snapshot, never replaced by this advisory projection.
    """
    context["approved_implementation"] = implementation
    if _fits(context):
        return context
    context["approved_implementation"] = None
    if not _fits(context):
        raise ValueError("subscription base context exceeds its bound")
    if implementation is None:
        raise ValueError("subscription context exceeds its bound")
    plan = implementation["approved_plan"]
    if not isinstance(plan, dict):
        raise TypeError("approved plan context differs")
    reference: dict[str, object] = {
        "revision": implementation["revision"],
        "source_attempt_id": implementation["source_attempt_id"],
        "approval_id": implementation["approval_id"],
        "plan_digest": implementation["plan_digest"],
        "projection": "bounded_approved_plan",
        "full_plan_omitted": True,
        "plan_authority": "approved task contract carries scope and named checks",
        "omitted_fields": [key for key in plan if key != "summary"],
    }
    summary = plan.get("summary")
    if isinstance(summary, str):
        prefix = _utf8_prefix(summary, 2048)
        reference["summary"] = prefix
        reference["summary_shortened"] = prefix != summary
    context["approved_implementation"] = reference
    if _fits(context):
        return context
    reference.pop("summary", None)
    reference["summary_omitted"] = True
    reference.pop("summary_shortened", None)
    reference["omitted_fields"] = list(plan)
    if _fits(context):
        return context
    raise ValueError("subscription approved plan reference exceeds remaining context bound")
