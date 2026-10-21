"""Bound approved plan prose inside the aggregate invocation context limit."""

from collections.abc import Mapping, Sequence

from forge.application.ports.subscription_gateway import validate_subscription_context
from forge.application.ports.subscription_recovery import PriorAttemptEvidence
from forge.application.ports.tools import ToolCallRecord
from forge.domain.tool import ToolCallStatus, ToolName

_MAX_TEXT = 256
_MAX_ATTEMPTS = 8
_CAPS = {
    "files_read": 50,
    "files_written": 50,
    "files_deleted": 25,
    "named_checks": 50,
}


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


def _text(value: object) -> str | None:
    return value[:_MAX_TEXT] if type(value) is str and value else None


def _distinct(values: list[str], cap: int) -> list[str]:
    return list(dict.fromkeys(values))[:cap]


def _search_entry(args: Mapping[str, object], term_key: str) -> dict[str, object]:
    entry: dict[str, object] = {}
    if (term := _text(args.get(term_key))) is not None:
        entry[term_key] = term
    if (path := _text(args.get("path"))) is not None:
        entry["path"] = path
    return entry


def _summarize_attempt(evidence: PriorAttemptEvidence) -> dict[str, object]:
    read: list[str] = []
    written: list[str] = []
    deleted: list[str] = []
    searches: list[dict[str, object]] = []
    renamed: list[dict[str, object]] = []
    checks: list[dict[str, object]] = []
    commits = 0
    for call in evidence.tool_calls:
        if not isinstance(call, ToolCallRecord) or not call.authorized:
            continue
        args = call.normalized_arguments
        succeeded = call.status is ToolCallStatus.SUCCEEDED
        if call.tool_name is ToolName.BUILD_RUN_NAMED_CHECK:
            name = _text(args.get("command_name"))
            if name and call.status in (ToolCallStatus.SUCCEEDED, ToolCallStatus.FAILED):
                checks.append({"name": name, "passed": succeeded, "status": call.status.value})
            continue
        if not succeeded:
            continue
        if call.tool_name is ToolName.REPOSITORY_READ_FILE:
            if (path := _text(args.get("path"))) is not None:
                read.append(path)
        elif call.tool_name in (ToolName.REPOSITORY_SEARCH, ToolName.REPOSITORY_SEARCH_SEMANTIC):
            entry = _search_entry(
                args, "literal" if call.tool_name is ToolName.REPOSITORY_SEARCH else "query"
            )
            if entry and entry not in searches:
                searches.append(entry)
        elif call.tool_name is ToolName.REPOSITORY_WRITE_FILE:
            # Only the path is summarized; file content never enters the checkpoint.
            if (path := _text(args.get("path"))) is not None:
                written.append(path)
        elif call.tool_name is ToolName.REPOSITORY_DELETE_FILE:
            if (path := _text(args.get("path"))) is not None:
                deleted.append(path)
        elif call.tool_name is ToolName.REPOSITORY_RENAME_FILE:
            source, destination = _text(args.get("source")), _text(args.get("destination"))
            if source is not None and destination is not None:
                entry = {"source": source, "destination": destination}
                if entry not in renamed:
                    renamed.append(entry)
        elif call.tool_name is ToolName.GIT_COMMIT:
            commits += 1
    return {
        "attempt_number": evidence.attempt_number,
        "disposition": _text(evidence.disposition),
        "failure": _text(evidence.failure),
        "files_read": _distinct(read, _CAPS["files_read"]),
        "searches": searches[:25],
        "files_written": _distinct(written, _CAPS["files_written"]),
        "files_deleted": _distinct(deleted, _CAPS["files_deleted"]),
        "files_renamed": renamed[:25],
        "git_commits": commits,
        "named_checks": checks[: _CAPS["named_checks"]],
    }


def summarize_prior_attempts(
    attempts: Sequence[PriorAttemptEvidence],
) -> dict[str, object] | None:
    """Build a bounded historical checkpoint from persisted prior-attempt records."""
    if not attempts:
        return None
    ordered = sorted(attempts, key=lambda item: item.attempt_number)
    truncated = len(ordered) > _MAX_ATTEMPTS
    kept = ordered[-_MAX_ATTEMPTS:]
    return {
        "kind": "prior_attempt_checkpoint",
        "historical": True,
        "truncated": truncated,
        "attempts": [_summarize_attempt(item) for item in kept],
    }


def _checkpoint_variants(checkpoint: Mapping[str, object]) -> list[dict[str, object] | None]:
    attempts = checkpoint.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return [None]
    variants: list[dict[str, object] | None] = [dict(checkpoint)]
    for keep in range(len(attempts) - 1, 0, -1):
        variants.append({**checkpoint, "truncated": True, "attempts": list(attempts[-keep:])})
    summary_only = [
        {
            "attempt_number": item.get("attempt_number"),
            "disposition": item.get("disposition"),
            "failure": item.get("failure"),
        }
        for item in attempts[-1:]
        if isinstance(item, dict)
    ]
    variants.append({**checkpoint, "truncated": True, "attempts": summary_only})
    variants.append(None)
    return variants


def bounded_approved_context(
    context: dict[str, object],
    implementation: dict[str, object] | None,
    *,
    checkpoint: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Keep full small plans; otherwise retain a verified provenance reference.

    The task contract carries the approved scope and checks. The full plan remains
    in the immutable gate snapshot, never replaced by this advisory projection.
    A prior-attempt checkpoint degrades (oldest detail first) before any
    task/policy/plan content is displaced.
    """
    context["approved_implementation"] = implementation
    if checkpoint is None:
        if _fits(context):
            return context
    else:
        for variant in _checkpoint_variants(checkpoint):
            if variant is None:
                context.pop("prior_attempt_checkpoint", None)
            else:
                context["prior_attempt_checkpoint"] = variant
            if _fits(context):
                return context
        context.pop("prior_attempt_checkpoint", None)
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
