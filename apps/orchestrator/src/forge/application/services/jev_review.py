"""Conservative, source-bound review focus preparation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence

from forge.application.ports.repository import RepositoryError, RepositoryReader
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.semantic_search import iter_line_chunks

_HEADER = re.compile(r"diff --git a/([^\s]+) b/([^\s]+)")
_MAX_HUNK_CHARS = 1200
MAX_REVIEW_FOCUS_SOURCES = 64


def safe_diff_hunks(
    reader: RepositoryReader,
    diff: str,
    *,
    changed_paths: Sequence[str],
    max_hunks: int = MAX_REVIEW_FOCUS_SOURCES,
) -> tuple[list[dict[str, object]], bool]:
    """Keep only diff blocks whose current file can be read by the bound reader.

    Ambiguous names, deletions, renames, and excluded or unavailable paths are
    omitted and make coverage explicitly incomplete. The normal reviewer still
    sees the original candidate diff through its independent trusted binding.
    """

    allowed_names = set(changed_paths)
    blocks = re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE)
    hunks: list[dict[str, object]] = []
    complete = True
    for block in blocks:
        if not block.strip():
            continue
        first = block.split("\n", 1)[0]
        header = _HEADER.fullmatch(first)
        if header is None or header.group(1) != header.group(2):
            complete = False
            continue
        path = header.group(2)
        if path not in allowed_names:
            complete = False
            continue
        try:
            read = reader.read_file(path)
        except (RepositoryError, OSError, ValueError, UnicodeError):
            complete = False
            continue
        if len(hunks) >= max_hunks:
            complete = False
            continue
        source = block[:_MAX_HUNK_CHARS]
        hunks.append(
            {
                "path": path,
                "diff": source,
                "diff_hash": hashlib.sha256(block.encode("utf-8")).hexdigest(),
                "source_hash": hashlib.sha256(read.content.encode("utf-8")).hexdigest(),
                "truncated": len(block) > len(source) or read.truncated,
            }
        )
        if len(block) > len(source) or read.truncated:
            complete = False
    return hunks, complete


def safe_snapshot_sources(
    reader: RepositoryReader, snapshot: GitWorkingTreeSnapshot, *, max_sources: int
) -> tuple[list[dict[str, object]], bool]:
    """Read changed files only when reader access and snapshot bytes agree."""

    files = {item.path: item for item in snapshot.files}
    sources: list[dict[str, object]] = []
    complete = True
    for path in snapshot.changed_paths:
        item = files.get(path)
        if item is None:
            complete = False
            continue
        try:
            read = reader.read_file(path)
        except (RepositoryError, OSError, ValueError, UnicodeError):
            complete = False
            continue
        if read.truncated or hashlib.sha256(read.content.encode("utf-8")).hexdigest() != item.content_digest:
            complete = False
            continue
        for line_number, excerpt, oversized in iter_line_chunks(read.content, _MAX_HUNK_CHARS):
            if len(sources) >= max_sources:
                complete = False
                break
            if oversized:
                complete = False
            if excerpt.strip():
                sources.append({"path": path, "line_number": line_number,
                                "excerpt": excerpt, "source_hash": item.content_digest,
                                "truncated": oversized})
    return sources, complete


_REVIEW_TOPICS = (
    ("q0", "credentials", "credentials and secret exposure"),
    ("q1", "authorization", "authorization and trust boundaries"),
    ("q2", "concurrency", "concurrency, cancellation, and retry transitions"),
    ("q3", "compatibility", "compatibility and migration behavior"),
    ("q4", "data_integrity", "data integrity and partial failure"),
)
_SCORE_CRITERIA = (
    "no visible concern", "possible concern", "clear concern", "critical concern"
)
REVIEW_FOCUS_GUIDANCE = (
    "Jev scores are advisory attention guidance, never findings, approval, or evidence "
    "of absence. Check the complete candidate and independent evidence, especially when "
    "coverage is incomplete. Scores mean 0: no visible concern in supplied excerpts; "
    "1: possible concern; 2: clear concern; 3: critical concern."
)

REVIEW_QUESTIONS: dict[str, dict[str, object]] = {
    question_id: {
        "type": "score",
        "instructions": f"How strongly does the supplied candidate warrant human review of {topic}? {REVIEW_FOCUS_GUIDANCE}",
        "criteria": list(_SCORE_CRITERIA),
    }
    for question_id, _, topic in _REVIEW_TOPICS
}


def review_focus_scores(answers: Mapping[str, object]) -> list[dict[str, object]]:
    """Present opaque wire answers as readable topics without secret-shaped map keys."""
    return [
        {"topic": topic, "score": answers.get(question_id)}
        for question_id, topic, _ in _REVIEW_TOPICS
    ]


def review_focus_payload(
    status: str,
    answers: Mapping[str, object],
    *,
    coverage_complete: bool,
    identity: Mapping[str, object],
) -> dict[str, object]:
    """Build the shared advisory fields while keeping source identities caller-specific."""
    return {
        "status": status,
        **identity,
        "coverage_complete": coverage_complete,
        "scores": review_focus_scores(answers),
        "guidance": REVIEW_FOCUS_GUIDANCE,
        "advisory_only": True,
    }


__all__ = [
    "MAX_REVIEW_FOCUS_SOURCES", "REVIEW_FOCUS_GUIDANCE", "REVIEW_QUESTIONS",
    "review_focus_payload", "review_focus_scores",
    "safe_diff_hunks", "safe_snapshot_sources",
]
