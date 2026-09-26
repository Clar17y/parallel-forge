"""Sanitized provider usage exhaustion evidence."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class QuotaExhaustion:
    """Typed evidence that a provider account allowance was exhausted."""

    observed_at: datetime
    reason: str
    reset_at: datetime | None = None

    def __post_init__(self) -> None:
        observed_at, reason, reset_at = self.observed_at, self.reason, self.reset_at
        _aware(observed_at, "observed_at")
        if type(reason) is not str or not reason or len(reason) > 128:
            raise ValueError("quota reason must be a bounded stable classifier")
        if any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_:-" for char in reason):
            raise ValueError("quota reason must be a stable classifier")
        if reset_at is not None:
            _aware(reset_at, "reset_at")
            if reset_at <= observed_at:
                raise ValueError("reset_at must be after observed_at")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _reset_at(value: object, *, now: datetime, relative: bool = False) -> datetime | None:
    """Absolute reset fields are never guessed to be relative retry delays."""
    candidate: datetime | None
    try:
        if isinstance(value, datetime):
            candidate = value
        elif isinstance(value, str) and not relative:
            candidate = datetime.fromisoformat(value)
        elif (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        ):
            candidate = (
                now + timedelta(seconds=value)
                if relative and value > 0
                else datetime.fromtimestamp(value, UTC)
                if not relative
                else None
            )
        else:
            return None
        if candidate is not None and candidate.utcoffset() is not None and candidate > now:
            return candidate.astimezone(UTC)
    except OverflowError, OSError, TypeError, ValueError:
        pass
    return None


_ABSOLUTE_RESETS = ("reset_at", "resets_at", "resetAt", "resetsAt", "reset_time", "resetTime")
_RELATIVE_RESETS = ("reset_after", "resetAfter", "reset_after_seconds", "resetAfterSeconds")
_DIAGNOSTIC_KEYS = ("error", "errors", "message", "result", "data")
_QUOTA_MESSAGE = re.compile(
    r"(?:(?:api )?error:\s*)?(?:"
    r"you['’]ve hit your(?: (?:session|weekly|plan|monthly|opus|sonnet))? limit|"
    r"(?:daily|plan|monthly|weekly|session|opus|sonnet) (?:quota (?:exhausted|exceeded)|(?:usage )?limit (?:reached|exceeded|exhausted))|"
    r"(?:5[- ]hour|usage) limit (?:reached|exceeded)|"
    r"(?:subscription )?allowance exhausted"
    r")(?:[.!]?(?:\s+|\s*[·—-]\s*)resets?\s+[^\r\n]+)?[.!]?",
    re.IGNORECASE,
)
_TRANSIENT = re.compile(
    r"per (?:minute|second)|requests/(?:min|sec)|rate[-_ ]limit|rate quota|temporarily limiting",
    re.IGNORECASE,
)


def _messages(value: object, *, depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    if isinstance(value, str):
        return [value[:4096]]
    if isinstance(value, Mapping):
        return [
            message
            for key in _DIAGNOSTIC_KEYS
            for message in _messages(value.get(key), depth=depth + 1)
        ]
    if isinstance(value, (list, tuple)):
        return [message for item in value[:64] for message in _messages(item, depth=depth + 1)]
    return []


def _codes(value: object, *, depth: int = 0) -> set[str]:
    if depth > 4:
        return set()
    if isinstance(value, Mapping):
        own = {
            item.upper()
            for key in ("code", "type", "status")
            if isinstance(item := value.get(key), str)
        }
        return own | {
            code for key in _DIAGNOSTIC_KEYS for code in _codes(value.get(key), depth=depth + 1)
        }
    if isinstance(value, (tuple, list)):
        return {code for item in value[:64] for code in _codes(item, depth=depth + 1)}
    return set()


def _message_reset(message: str, *, now: datetime) -> datetime | None:
    iso = re.search(
        r"\bresets?(?:\s+at)?\s+(\d{4}-\d\d-\d\dT[0-9:.]+(?:Z|[+-]\d\d:\d\d))",
        message,
        re.IGNORECASE,
    )
    if iso:
        return _reset_at(iso.group(1), now=now)
    relative = re.search(
        r"\bresets?\s+in\s+((?:\d+\s*(?:hours?|minutes?|seconds?|h|m|s)\s*)+)[.!]?\s*$",
        message,
        re.IGNORECASE,
    )
    if relative:
        parts = re.findall(
            r"(\d+)\s*(hours?|minutes?|seconds?|h|m|s)", relative.group(1), re.IGNORECASE
        )
        units = [unit[0].lower() for _, unit in parts]
        if len(units) == len(set(units)):
            seconds = sum(
                int(value) * {"h": 3600, "m": 60, "s": 1}[unit[0].lower()] for value, unit in parts
            )
            return _reset_at(seconds, now=now, relative=True)
    human = re.search(
        r"\bresets?\s+(?:(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})(?:,\s*(\d{4}))?[,]?\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)",
        message,
        re.IGNORECASE,
    )
    if human:
        month, day, year, hour, minute, ampm, zone_name = human.groups()
        try:
            zone = ZoneInfo(zone_name)
            local = now.astimezone(zone)
            month_number = (
                (
                    "jan",
                    "feb",
                    "mar",
                    "apr",
                    "may",
                    "jun",
                    "jul",
                    "aug",
                    "sep",
                    "oct",
                    "nov",
                    "dec",
                ).index(month[:3].lower())
                + 1
                if month
                else local.month
            )
            if not 1 <= int(hour) <= 12:
                return None
            candidate = datetime(
                int(year) if year else local.year,
                month_number,
                int(day) if day else local.day,
                int(hour) % 12 + (12 if ampm.lower() == "pm" else 0),
                int(minute or 0),
                tzinfo=zone,
            )
            # Reject both ambiguous folds and nonexistent local wall times.
            if candidate.replace(fold=0).utcoffset() != candidate.replace(fold=1).utcoffset():
                return None
            if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != candidate.replace(
                tzinfo=None
            ):
                return None
            return _reset_at(candidate, now=now)
        except LookupError, OverflowError, ValueError:
            pass
    return None


def _nested_reset(value: object, *, now: datetime, depth: int = 0) -> datetime | None:
    if depth > 4:
        return None
    candidates: list[datetime] = []
    if isinstance(value, Mapping):
        for field in (*_ABSOLUTE_RESETS, *_RELATIVE_RESETS):
            candidate = _reset_at(value.get(field), now=now, relative=field in _RELATIVE_RESETS)
            if candidate:
                candidates.append(candidate)
        for key in _DIAGNOSTIC_KEYS:
            candidate = _nested_reset(value.get(key), now=now, depth=depth + 1)
            if candidate:
                candidates.append(candidate)
    elif isinstance(value, (tuple, list)):
        for item in value[:64]:
            candidate = _nested_reset(item, now=now, depth=depth + 1)
            if candidate:
                candidates.append(candidate)
    elif isinstance(value, str):
        candidate = _message_reset(value[:4096], now=now)
        if candidate:
            candidates.append(candidate)
    return max(candidates) if candidates else None


def quota_evidence(
    *, reason: str, now: datetime | None = None, reset_at: object = None
) -> QuotaExhaustion:
    observed = now or utc_now()
    _aware(observed, "observed_at")
    return QuotaExhaustion(observed, reason, _reset_at(reset_at, now=observed))


def classify_claude_error(
    *, status: int | None, errors: object, now: datetime | None = None
) -> tuple[str, QuotaExhaustion | None]:
    observed = now or utc_now()
    messages, codes = _messages(errors), _codes(errors)
    text = " ".join(messages).lower()
    if status == 401 or codes & {"AUTHENTICATION_ERROR", "UNAUTHORIZED", "UNAUTHENTICATED"}:
        return "authentication", None
    if status == 403:
        return "policy_denied", None
    if status is not None and status >= 500:
        return "outage", None
    explicit_quota = bool(codes & {"QUOTA_EXHAUSTED", "INSUFFICIENT_QUOTA", "USAGE_LIMIT_EXCEEDED"})
    if not explicit_quota and _TRANSIENT.search(text):
        return "throttled", None
    if explicit_quota or any(
        _QUOTA_MESSAGE.fullmatch(message.strip()) for message in messages
    ):
        return "quota", quota_evidence(
            reason="claude_account_usage_exhausted",
            now=observed,
            reset_at=_nested_reset(errors, now=observed),
        )
    if status == 429 or "RATE_LIMIT_ERROR" in codes:
        return "throttled", None
    if re.search(
        r"\b(?:authentication|unauthorized|unauthenticated|not logged in|invalid api key|login expired)\b",
        text,
    ):
        return "authentication", None
    if re.search(r"\b(?:temporarily unavailable|service unavailable|overloaded|timeout)\b", text):
        return "outage", None
    if re.search(r"\b(?:unsupported|not supported|unknown model|model not found)\b", text):
        return "unsupported", None
    return "protocol", None


def classify_codex_error(
    error: Mapping[str, object], *, now: datetime | None = None
) -> tuple[str, QuotaExhaustion | None]:
    observed = now or utc_now()
    code = error.get("codexErrorInfo")
    if code == "usageLimitExceeded":
        return "quota", quota_evidence(
            reason="codex_account_usage_exhausted",
            now=observed,
            reset_at=_nested_reset(error, now=observed),
        )
    if code == "rateLimitExceeded":
        return "throttled", None
    if code in ("sessionBudgetExceeded", "contextWindowExceeded"):
        return "budget", None
    if code in ("authenticationFailed", "unauthorized"):
        return "authentication", None
    if code in ("modelUnsupported", "unsupportedModel"):
        return "unsupported", None
    if code in (
        "serverOverloaded",
        "internalServerError",
        "serviceUnavailable",
        "serverError",
        "overloaded",
    ):
        return "outage", None
    if code in ("cyberPolicy", "misalignmentPolicyViolation"):
        return "policy_denied", None
    if isinstance(code, Mapping) and len(code) == 1:
        variant, detail = next(iter(code.items()))
        if variant in (
            "httpConnectionFailed",
            "responseStreamConnectionFailed",
            "responseStreamDisconnected",
            "responseTooManyFailedAttempts",
        ) and isinstance(detail, Mapping):
            status = detail.get("httpStatusCode")
            if status == 429:
                return "throttled", None
            if status == 401:
                return "authentication", None
            if status == 403:
                return "policy_denied", None
            return "outage", None
    return "protocol", None


__all__ = ["QuotaExhaustion", "classify_claude_error", "classify_codex_error", "quota_evidence"]
