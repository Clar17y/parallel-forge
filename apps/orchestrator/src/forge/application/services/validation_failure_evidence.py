"""Bounded failure detail extracted from persisted controller check output.

The inputs are the already-persisted, already-redacted stdout/stderr artifacts
bound to a validation evidence manifest. Nothing here executes commands or
reads unredacted sources. Every output is deterministically capped so the
repair handoff summary stays small enough for existing context bounding.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

MAX_FAILURE_DETAIL_CHARS = 1600
MAX_FAILED_NODES_PER_CHECK = 8
MAX_EXCERPT_CHARS_PER_CHECK = 400

_FAILED_LINE = re.compile(r"^FAILED[ \t]+(\S+)", re.MULTILINE)
_COUNT = re.compile(r"(\d+)\s+(failed|passed|errors?|skipped|xfailed|xpassed|deselected)\b")
_TRUNCATED_MARKER = " [truncated]"


@dataclass(frozen=True, slots=True)
class CheckFailureEvidence:
    check_name: str
    failing_nodes: tuple[str, ...]
    extra_node_count: int
    counts: str | None
    excerpt: str


def _output_text(blob: bytes) -> str:
    """Decode a persisted command-output artifact into plain output text."""
    if not blob:
        return ""
    text: str | None = None
    try:
        decoded = json.loads(blob)
    except (ValueError, UnicodeDecodeError):
        decoded = None
    if isinstance(decoded, dict) and decoded.get("stream") in ("stdout", "stderr"):
        candidate = decoded.get("text")
        if isinstance(candidate, str):
            text = candidate
    if text is None:
        text = blob.decode("utf-8", errors="replace")
    # Drop control characters (keeping newline/tab) so binary-ish output
    # degrades to a bounded printable tail instead of corrupting the summary.
    return "".join(
        char if char in "\n\t" or (ord(char) >= 0x20 and ord(char) != 0x7F) else "�"
        for char in text
    )


def _counts(text: str) -> str | None:
    for line in reversed(text.splitlines()):
        matches = _COUNT.findall(line)
        if matches and (" in " in line or line.lstrip().startswith("=")):
            return ", ".join(f"{amount} {label}" for amount, label in matches)
    return None


def extract_check_failure(check_name: str, blob: bytes) -> CheckFailureEvidence:
    """Extract bounded evidence from one failed check's persisted output."""
    text = _output_text(blob)
    nodes: list[str] = []
    seen: set[str] = set()
    for node in _FAILED_LINE.findall(text):
        if node not in seen:
            seen.add(node)
            nodes.append(node)
    excerpt = text.rstrip()[-MAX_EXCERPT_CHARS_PER_CHECK:].strip()
    return CheckFailureEvidence(
        check_name=check_name,
        failing_nodes=tuple(nodes[:MAX_FAILED_NODES_PER_CHECK]),
        extra_node_count=max(0, len(nodes) - MAX_FAILED_NODES_PER_CHECK),
        counts=_counts(text),
        excerpt=excerpt,
    )


def _format_check(evidence: CheckFailureEvidence) -> str:
    parts = [f"- {evidence.check_name}:"]
    if evidence.failing_nodes:
        nodes = ", ".join(evidence.failing_nodes)
        if evidence.extra_node_count:
            nodes += f" (+{evidence.extra_node_count} more)"
        parts.append(f"failing tests: {nodes};")
    else:
        parts.append("failing tests: none identified;")
    if evidence.counts:
        parts.append(f"counts: {evidence.counts};")
    if evidence.excerpt:
        parts.append(f"output tail: {evidence.excerpt}")
    return " ".join(parts).rstrip("; ")


def build_failure_detail(evidences: list[CheckFailureEvidence]) -> str:
    """Join per-check evidence under one deterministic total cap."""
    if not evidences:
        return ""
    detail = "Controller validation failure detail:"
    for evidence in evidences:
        section = _format_check(evidence)
        candidate = f"{detail}\n{section}"
        if len(candidate) <= MAX_FAILURE_DETAIL_CHARS:
            detail = candidate
            continue
        remaining = MAX_FAILURE_DETAIL_CHARS - len(detail) - 1 - len(_TRUNCATED_MARKER)
        if remaining > 0:
            detail = f"{detail}\n{section[:remaining].rstrip()}{_TRUNCATED_MARKER}"
        else:
            detail = detail[: MAX_FAILURE_DETAIL_CHARS - len(_TRUNCATED_MARKER)] + _TRUNCATED_MARKER
        break
    return detail
