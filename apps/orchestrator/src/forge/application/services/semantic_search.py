"""Bounded repository excerpts for advisory semantic search.

Every source byte is obtained through the controlled RepositoryReader. Jev only
scores these numbered excerpts; the original text is returned to the caller.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from forge.application.ports.repository import (
    RepositoryError,
    RepositoryReader,
    is_hidden_search_path,
)

_EXCERPT_CHARS = 400


def iter_line_chunks(content: str, max_chars: int) -> Iterator[tuple[int, str, bool]]:
    """Yield bounded, line-aligned excerpts and whether each exceeded its cap."""
    if max_chars < 1:
        raise ValueError("line chunk bound must be positive")
    lines = content.splitlines(keepends=True)
    start = 0
    while start < len(lines):
        end = start
        size = 0
        while end < len(lines) and (size + len(lines[end]) <= max_chars or end == start):
            size += len(lines[end])
            end += 1
        chunk = "".join(lines[start:end])
        excerpt = chunk[:max_chars]
        yield start + 1, excerpt, size > len(excerpt)
        start = end


@dataclass(frozen=True, slots=True)
class SemanticCandidate:
    path: str
    line_number: int
    excerpt: str
    content_hash: str
    file_hash: str
    truncated: bool

    def payload(self, *, score: float | None = None, confidence: float | None = None) -> dict[str, object]:
        return {
            "path": self.path,
            "line_number": self.line_number,
            "excerpt": self.excerpt,
            "content_hash": self.content_hash,
            "score": score,
            "confidence": confidence,
            "truncated": self.truncated,
            "untrusted_repository_content": True,
        }


def collect_candidates(
    reader: RepositoryReader, path: str, *, max_candidates: int
) -> tuple[tuple[SemanticCandidate, ...], bool]:
    """Enumerate contained regular text files in reader order with a hard cap."""

    if type(max_candidates) is not int or max_candidates < 1:
        raise ValueError("semantic candidate bound must be positive")
    entries = reader.list_files(path)
    candidates: list[SemanticCandidate] = []
    complete = True
    for entry in entries:
        if is_hidden_search_path(entry.path):
            continue
        if len(candidates) >= max_candidates:
            complete = False
            break
        try:
            read = reader.read_file(entry.path)
        except (RepositoryError, OSError, ValueError, UnicodeError):
            complete = False
            continue
        file_hash = hashlib.sha256(read.content.encode("utf-8")).hexdigest()
        if read.truncated:
            complete = False
        for line_number, excerpt, oversized in iter_line_chunks(read.content, _EXCERPT_CHARS):
            if len(candidates) >= max_candidates:
                complete = False
                break
            if oversized:
                complete = False
            if excerpt.strip():
                candidates.append(SemanticCandidate(
                    path=read.path,
                    line_number=line_number,
                    excerpt=excerpt,
                    content_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                    file_hash=file_hash,
                    truncated=read.truncated or oversized,
                ))
    return tuple(candidates), complete


def candidates_current(reader: RepositoryReader, candidates: tuple[SemanticCandidate, ...]) -> bool:
    """Reject a model ordering if a source changed while it was evaluated."""

    try:
        expected: dict[str, str] = {}
        for item in candidates:
            known = expected.setdefault(item.path, item.file_hash)
            if known != item.file_hash:
                return False
        return all(
            hashlib.sha256(reader.read_file(path).content.encode("utf-8")).hexdigest() == digest
            for path, digest in expected.items()
        )
    except (RepositoryError, OSError, ValueError, UnicodeError):
        return False


def select_candidates(
    candidates: tuple[SemanticCandidate, ...],
    answers: Mapping[str, Mapping[str, object]],
    *,
    top_k: int,
) -> list[dict[str, object]]:
    """Keep relevant scores and unknown items; retain baseline if none are relevant."""

    scored: list[tuple[float, int, SemanticCandidate, float]] = []
    unknown: list[SemanticCandidate] = []
    for index, item in enumerate(candidates):
        answer = answers.get(f"c{index}")
        score = jev_score(answers, index)
        confidence = answer.get("confidence") if isinstance(answer, Mapping) else None
        if score is None or not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            unknown.append(item)
            continue
        scored.append((float(score), index, item, float(confidence)))
    ranked = sorted((row for row in scored if row[0] > 0), key=lambda row: (-row[0], row[1]))[:top_k]
    if not ranked:
        return [item.payload() for item in candidates]
    return [item.payload(score=score, confidence=confidence) for score, _, item, confidence in ranked] + [
        item.payload() for item in unknown
    ]


__all__ = ["SemanticCandidate", "candidates_current", "collect_candidates", "select_candidates"]


def jev_score(answers: Mapping[str, Mapping[str, object]], index: int) -> float | None:
    """Return a valid advisory score while treating booleans and malformed values as unknown."""
    answer = answers.get(f"c{index}")
    value = answer.get("score") if isinstance(answer, Mapping) else None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 3:
        return None
    return float(value)
