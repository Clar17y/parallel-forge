"""Closed, immutable authoring snapshots for epic work-item graphs."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.domain.operation import canonical_digest
from forge.domain.payload import validate_durable_payload


class GraphRevisionNotFound(RuntimeError):
    pass


class GraphNotAccepted(RuntimeError):
    pass


class GraphBindingConflict(RuntimeError):
    pass


class GraphValidationError(ValueError):
    pass


def _text(value: str, maximum: int, *, bytes_limit: bool = False) -> str:
    if (
        not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8") if bytes_limit else value) > maximum
    ):
        raise ValueError("graph text is invalid")
    validate_durable_payload(value)
    return value


def _digest(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("graph digest is invalid")
    return value


class ItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: UUID
    disposition: Literal["required", "deferred"]
    ordinal: int = Field(ge=0, strict=True)
    title: str
    outcome: str
    acceptance_criteria: list[str] = Field(min_length=1, max_length=64)
    source_requirement_ids: list[UUID] = Field(min_length=1, max_length=64)
    dependency_item_ids: list[UUID] = Field(default_factory=list, max_length=128)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: str) -> str:
        return _text(value, 256, bytes_limit=True)

    @field_validator("outcome")
    @classmethod
    def valid_outcome(cls, value: str) -> str:
        return _text(value, 5000)

    @field_validator("acceptance_criteria")
    @classmethod
    def valid_criteria(cls, value: list[str]) -> list[str]:
        return [_text(item, 5000) for item in value]

    @model_validator(mode="after")
    def unique_references(self) -> ItemInput:
        for ids in (self.source_requirement_ids, self.dependency_item_ids):
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate graph reference")
        return self


class ItemSnapshot(ItemInput):
    graph_revision_id: UUID
    item_digest: str

    _valid_digest = field_validator("item_digest")(_digest)


class GraphRevisionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    expected_epic_version: int = Field(ge=1, strict=True)
    brief_revision_id: UUID
    brief_digest: str
    items: list[ItemInput] = Field(max_length=128)

    _valid_digest = field_validator("brief_digest")(_digest)


class GraphAdoptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    expected_epic_version: int = Field(ge=1, strict=True)
    graph_revision_id: UUID
    graph_digest: str

    _valid_digest = field_validator("graph_digest")(_digest)


class GraphRevisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    graph_revision_id: UUID
    epic_id: UUID
    brief_revision_id: UUID
    brief_digest: str
    revision_number: int = Field(ge=1, strict=True)
    epic_version: int = Field(ge=1, strict=True)
    graph_digest: str
    items: list[ItemSnapshot]
    created_at: datetime

    _valid_brief_digest = field_validator("brief_digest")(_digest)
    _valid_graph_digest = field_validator("graph_digest")(_digest)


class AcceptedGraph(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    epic_id: UUID
    brief_revision_id: UUID
    brief_digest: str
    graph_revision_id: UUID
    graph_digest: str
    items: list[ItemSnapshot]


class ItemReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    item_id: UUID
    status: Literal["ready", "blocked", "deferred"]
    reason: str = Field(max_length=256)
    dependency_item_ids: list[UUID]


def _item_payload(item: ItemInput) -> dict[str, object]:
    return {
        "schema_version": 1,
        "item_id": str(item.item_id),
        "disposition": item.disposition,
        "ordinal": item.ordinal,
        "title": item.title,
        "outcome": item.outcome,
        "acceptance_criteria": list(item.acceptance_criteria),
        "source_requirement_ids": sorted(str(value) for value in item.source_requirement_ids),
        "dependency_item_ids": sorted(str(value) for value in item.dependency_item_ids),
    }


def validate_graph(items: list[ItemInput], *, adoption: bool = False) -> None:
    if len(items) > 128 or (adoption and not items):
        raise GraphValidationError("graph item count is invalid")
    indexed = {item.item_id: item for item in items}
    if len(indexed) != len(items):
        raise GraphValidationError("duplicate graph item")
    for item in items:
        if item.item_id in item.dependency_item_ids or any(
            dep not in indexed for dep in item.dependency_item_ids
        ):
            raise GraphValidationError("graph edge is invalid")
    remaining = {node: len(item.dependency_item_ids) for node, item in indexed.items()}
    dependents: dict[UUID, list[UUID]] = {node: [] for node in indexed}
    for item in items:
        for dep in item.dependency_item_ids:
            dependents[dep].append(item.item_id)
    ready = [node for node, count in remaining.items() if count == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for dependent in dependents[node]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)
    if visited != len(items):
        raise GraphValidationError("graph contains cycle")
    if adoption:
        for item in items:
            if item.disposition != "required":
                continue
            seen: set[UUID] = set()
            pending = list(item.dependency_item_ids)
            while pending:
                dep = pending.pop()
                if dep in seen:
                    continue
                seen.add(dep)
                target = indexed[dep]
                if target.disposition == "deferred":
                    raise GraphValidationError("required item depends on deferred item")
                pending.extend(target.dependency_item_ids)


def make_snapshot(
    graph_revision_id: UUID, items: list[ItemInput], brief_revision_id: UUID, brief_digest: str
) -> tuple[list[ItemSnapshot], str]:
    validate_graph(items)
    ordered = sorted(items, key=lambda item: (item.ordinal, str(item.item_id)))
    snapshots = [
        ItemSnapshot.model_validate(
            {
                **item.model_dump(mode="python"),
                "source_requirement_ids": sorted(item.source_requirement_ids, key=str),
                "dependency_item_ids": sorted(item.dependency_item_ids, key=str),
                "graph_revision_id": graph_revision_id,
                "item_digest": canonical_digest(_item_payload(item)),
            }
        )
        for item in ordered
    ]
    content = {
        "schema_version": 1,
        "brief_revision_id": str(brief_revision_id),
        "brief_digest": brief_digest,
        "items": [item.model_dump(mode="json") for item in snapshots],
    }
    validate_durable_payload(content)
    if (
        len(
            json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        > 262144
    ):
        raise GraphValidationError("graph snapshot is too large")
    return snapshots, canonical_digest(content)


def project_readiness(items: list[ItemSnapshot]) -> list[ItemReadiness]:
    return [
        ItemReadiness(
            item_id=item.item_id,
            status="deferred"
            if item.disposition == "deferred"
            else "blocked"
            if item.dependency_item_ids
            else "ready",
            reason="Explicitly deferred"
            if item.disposition == "deferred"
            else "Pending verified prerequisite integration"
            if item.dependency_item_ids
            else "Structurally ready; execution eligibility is checked separately",
            dependency_item_ids=item.dependency_item_ids,
        )
        for item in sorted(items, key=lambda item: (item.ordinal, str(item.item_id)))
    ]
