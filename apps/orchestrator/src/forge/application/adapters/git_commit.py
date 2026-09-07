"""Durable-operation adapters for the two-phase controlled Git commit effect."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from typing import Final, cast
from uuid import UUID

from forge.application.ports.operations import GitCommitReceiptReader
from forge.application.ports.worktrees import (
    ControlledGitPort,
    ManagedWorktree,
    PreparedGitCommit,
    PublishedGitCommit,
)
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationStatus,
    canonical_digest,
)

PREPARE_GIT_COMMIT_KIND: Final = "git.commit.prepare.v1"
PUBLISH_GIT_COMMIT_KIND: Final = "git.commit.publish.v1"


class GitCommitOperationError(RuntimeError):
    """A persisted Git commit operation is malformed or no longer bound."""


class PrepareGitCommitAdapter:
    """Stage one already-authorized commit and persist a digest-only receipt."""

    def __init__(self, git: ControlledGitPort, worktree: ManagedWorktree) -> None:
        self._git = git
        self._worktree = worktree

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        values = _prepare_request(intent, self._worktree)
        prepared = await asyncio.to_thread(
            self._git.prepare_commit, self._worktree, _text(values, "message")
        )
        if not isinstance(prepared, PreparedGitCommit) or (
            prepared.worktree_identity != self._worktree.identity
        ):
            raise GitCommitOperationError("controlled Git preparation result is invalid")
        return OperationOutcome(payload=_prepare_receipt(intent, values, prepared))

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        # Reconciliation must never stage a potentially changed index.
        _prepare_request(intent, self._worktree)
        if intent.status is OperationStatus.SUCCEEDED:
            return intent.to_outcome()
        return _needs_reconciliation()


class PublishGitCommitAdapter:
    """Publish only the exact durable snapshot made by a preparation intent."""

    def __init__(
        self,
        git: ControlledGitPort,
        worktree: ManagedWorktree,
        receipts: GitCommitReceiptReader,
    ) -> None:
        self._git = git
        self._worktree = worktree
        self._receipts = receipts

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        values, prepared = await self._publication(intent)
        published = await asyncio.to_thread(self._git.commit_prepared, self._worktree, prepared)
        return _publish_outcome(intent, values, prepared, published)

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        values, prepared = await self._publication(intent)
        published = await asyncio.to_thread(
            self._git.inspect_prepared_commit, self._worktree, prepared
        )
        if published is None:
            return _needs_reconciliation()
        return _publish_outcome(intent, values, prepared, published)

    async def _publication(
        self, intent: OperationIntent
    ) -> tuple[dict[str, object], PreparedGitCommit]:
        values = _publish_request(intent, self._worktree)
        try:
            preparation = await self._receipts.get(UUID(_text(values, "preparation_intent_id")))
        except TypeError, ValueError, LookupError:
            raise GitCommitOperationError("prepared commit receipt is unavailable") from None
        return values, _prepared_from_intent(preparation, values, self._worktree)


_AUTHORITY_KEYS: Final = {
    "agent_execution_id",
    "policy_version",
    "request_digest",
    "run_id",
    "step_id",
    "tool_call_id",
    "worktree_id",
}


def _prepare_request(intent: OperationIntent, worktree: ManagedWorktree) -> dict[str, object]:
    return _request(
        intent,
        worktree,
        PREPARE_GIT_COMMIT_KIND,
        {
            "agent_execution_id",
            "base_sha",
            "message",
            "message_digest",
            "policy_version",
            "request_digest",
            "run_id",
            "step_id",
            "tool_call_id",
            "worktree_id",
        },
    )


def _publish_request(intent: OperationIntent, worktree: ManagedWorktree) -> dict[str, object]:
    return _request(
        intent,
        worktree,
        PUBLISH_GIT_COMMIT_KIND,
        {
            "agent_execution_id",
            "base_sha",
            "message",
            "message_digest",
            "policy_version",
            "preparation_intent_id",
            "previous_sha",
            "request_digest",
            "run_id",
            "step_id",
            "tool_call_id",
            "tree_sha",
            "worktree_id",
        },
    )


def _request(
    intent: OperationIntent,
    worktree: ManagedWorktree,
    kind: str,
    keys: set[str],
) -> dict[str, object]:
    if (
        not isinstance(intent, OperationIntent)
        or intent.kind != kind
        or intent.request_schema_version != 1
    ):
        raise GitCommitOperationError("Git commit operation request is invalid")
    payload = intent.request_payload
    if set(payload) != keys or canonical_digest(payload) != intent.request_digest:
        raise GitCommitOperationError("Git commit operation request is invalid")
    if any(not isinstance(payload[key], str) for key in keys - {"policy_version"}):
        raise GitCommitOperationError("Git commit operation request is invalid")
    values = {key: payload[key] for key in keys}
    if (
        values["run_id"] != str(intent.run_id)
        or values["worktree_id"] != worktree.identity.worktree_name
    ):
        raise GitCommitOperationError("Git commit operation binding is invalid")
    if intent.run_id != worktree.identity.run_id:
        raise GitCommitOperationError("Git commit operation binding is invalid")
    if (
        values["base_sha"] != worktree.base_sha
        or _message_digest(_text(values, "message")) != values["message_digest"]
    ):
        raise GitCommitOperationError("Git commit operation binding is invalid")
    if values["request_digest"] != canonical_digest({"message": _text(values, "message")}):
        raise GitCommitOperationError("Git commit operation binding is invalid")
    # PreparedGitCommit is the existing canonical message validator.  Creating
    # it here rejects malformed requests before any Git operation is attempted.
    try:
        PreparedGitCommit(
            worktree_identity=worktree.identity,
            previous_sha=worktree.base_sha,
            tree_sha=worktree.base_sha,
            message=_text(values, "message"),
        )
        UUID(str(values.get("preparation_intent_id", intent.id)))
        for key in ("agent_execution_id", "step_id", "tool_call_id"):
            parsed = UUID(_text(values, key))
            if parsed.int == 0 or str(parsed) != _text(values, key):
                raise ValueError
        if type(values["policy_version"]) is not int or values["policy_version"] <= 0:
            raise ValueError
    except TypeError, ValueError:
        raise GitCommitOperationError("Git commit operation request is invalid") from None
    return values


def _prepared_from_intent(
    intent: OperationIntent,
    publication: Mapping[str, object],
    worktree: ManagedWorktree,
) -> PreparedGitCommit:
    if (
        not isinstance(intent, OperationIntent)
        or intent.status is not OperationStatus.SUCCEEDED
        or intent.kind != PREPARE_GIT_COMMIT_KIND
        or intent.request_schema_version != 1
        or intent.outcome_schema_version != 1
        or intent.outcome is None
        or str(intent.id) != publication["preparation_intent_id"]
        or intent.run_id != UUID(_text(publication, "run_id"))
    ):
        raise GitCommitOperationError("prepared commit receipt is invalid")
    request = _prepare_request(intent, worktree)
    receipt = intent.outcome
    required = _AUTHORITY_KEYS | {
        "base_sha",
        "message_digest",
        "preparation_intent_id",
        "previous_sha",
        "tree_sha",
    }
    if (
        set(receipt) != required
        or any(not isinstance(receipt[key], str) for key in required - {"policy_version"})
        or type(receipt["policy_version"]) is not int
    ):
        raise GitCommitOperationError("prepared commit receipt is invalid")
    values = {key: receipt[key] for key in required}
    if (
        values["preparation_intent_id"] != str(intent.id)
        or values["worktree_id"] != worktree.identity.worktree_name
        or values["base_sha"] != worktree.base_sha
        or values["message_digest"] != request["message_digest"]
        or values["request_digest"] != request["request_digest"]
        or any(values[key] != request[key] for key in _AUTHORITY_KEYS)
        or any(values[key] != publication[key] for key in required - {"preparation_intent_id"})
    ):
        raise GitCommitOperationError("prepared commit receipt is invalid")
    try:
        return PreparedGitCommit(
            worktree_identity=worktree.identity,
            previous_sha=str(values["previous_sha"]),
            tree_sha=str(values["tree_sha"]),
            message=str(publication["message"]),
        )
    except TypeError, ValueError:
        raise GitCommitOperationError("prepared commit receipt is invalid") from None


def _prepare_receipt(
    intent: OperationIntent, values: Mapping[str, object], prepared: PreparedGitCommit
) -> dict[str, object]:
    if _message_digest(prepared.message) != values["message_digest"]:
        raise GitCommitOperationError("controlled Git preparation result is invalid")
    return {
        **{key: values[key] for key in _AUTHORITY_KEYS},
        "base_sha": values["base_sha"],
        "message_digest": values["message_digest"],
        "preparation_intent_id": str(intent.id),
        "previous_sha": prepared.previous_sha,
        "request_digest": values["request_digest"],
        "tree_sha": prepared.tree_sha,
        "worktree_id": values["worktree_id"],
    }


def _publish_outcome(
    intent: OperationIntent,
    values: Mapping[str, object],
    prepared: PreparedGitCommit,
    published: object,
) -> OperationOutcome:
    if not isinstance(published, PublishedGitCommit) or (
        published.worktree_identity != prepared.worktree_identity
        or published.previous_sha != prepared.previous_sha
        or published.tree_sha != prepared.tree_sha
        or published.message != prepared.message
    ):
        raise GitCommitOperationError("controlled Git publication result is invalid")
    return OperationOutcome(
        payload={
            **{key: values[key] for key in _AUTHORITY_KEYS},
            "base_sha": values["base_sha"],
            "message_digest": values["message_digest"],
            "new_sha": published.new_sha,
            "preparation_intent_id": values["preparation_intent_id"],
            "previous_sha": prepared.previous_sha,
            "request_digest": values["request_digest"],
            "tree_sha": prepared.tree_sha,
            "worktree_id": values["worktree_id"],
        }
    )


def _message_digest(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _text(values: Mapping[str, object], key: str) -> str:
    """Return a string already checked by the strict request/receipt codecs."""

    return cast(str, values[key])


def _needs_reconciliation() -> OperationOutcome:
    return OperationOutcome(
        status=OperationStatus.NEEDS_RECONCILIATION,
        error="Git commit outcome requires reconciliation",
    )


__all__ = [
    "PREPARE_GIT_COMMIT_KIND",
    "PUBLISH_GIT_COMMIT_KIND",
    "GitCommitOperationError",
    "PrepareGitCommitAdapter",
    "PublishGitCommitAdapter",
]
