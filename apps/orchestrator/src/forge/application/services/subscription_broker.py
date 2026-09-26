"""Provider-neutral, fail-closed admission for subscription tool callbacks.

The provider only supplies an opaque token, call key, closed tool name and
schema-bounded arguments.  Every authority fact remains in the Forge-created
``BrokerAuthorizationBinding`` closure.
"""

from __future__ import annotations

import asyncio
import hmac
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from uuid import UUID, uuid5

from forge.agents.subscription_protocol import ProviderToolCall
from forge.application.ports.tool_schemas import arguments_match_schema
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.operation import canonical_digest, thaw_payload
from forge.domain.paths import normalize_policy_path
from forge.domain.scheduling import TaskEffectLease, TaskLease
from forge.domain.subscription import BrokerAuthorizationBinding, ToolCallBinding
from forge.domain.tool import (
    SubscriptionToolAuthorizationContext,
    ToolCallStatus,
    ToolError,
    ToolErrorCode,
    ToolName,
    ToolRequest,
    ToolResult,
)

_CALL_KEY = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,254}\Z")


class BrokerDenied(PermissionError):
    """Stable callback denial; deliberately contains no provider input."""

    def __init__(self) -> None:
        super().__init__("subscription tool callback denied")


@dataclass(frozen=True, slots=True, kw_only=True)
class BrokerReceipt:
    operation_id: UUID
    accepted: bool
    result: Mapping[str, object]


# The durable id is allocated before scheduler admission.  Passing it into the
# Forge-owned adapter is what lets ControlledToolService bind its operation
# intent and audit record to the provider replay key; it is never provider
# selected.
Effect = Callable[[UUID, ToolName, Mapping[str, object]], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ControlledSubscriptionEffect:
    """Trusted bridge from a broker operation to the sole tool authority."""

    service: object
    context: SubscriptionToolAuthorizationContext

    async def __call__(
        self, operation_id: UUID, name: ToolName, arguments: Mapping[str, object]
    ) -> ToolResult:
        invoke = getattr(self.service, "invoke", None)
        if not callable(invoke):
            raise BrokerDenied()
        # Both IDs are Forge-issued; callers never select either one.
        bound = replace(self.context, invocation_id=operation_id, operation_intent_id=operation_id)
        result = await invoke(bound, ToolRequest(name=name, arguments=arguments))
        if not isinstance(result, ToolResult):
            raise BrokerDenied()
        return result


class SubscriptionToolBroker:
    """Binds callback replay identity before the sole controlled effect authority.

    ``effect`` is a Forge-owned controlled-tool adapter.  It receives the
    already-bound durable operation id, never a provider authority field.
    """

    def __init__(
        self,
        work_factory: Callable[[], AbstractAsyncContextManager[UnitOfWork]],
        *,
        lease: TaskLease,
        authority: BrokerAuthorizationBinding,
        effect: Effect,
        owned_paths: tuple[str, ...] = (),
        whole_worktree_exclusive: bool = False,
        expected_candidate_epoch: int | None = None,
    ) -> None:
        if not callable(effect):
            raise TypeError("broker effect must be a Forge-owned callable")
        if (lease.run_id, lease.task_id) != (authority.run_id, authority.task_id):
            raise ValueError("scheduler lease differs from broker authority")
        if not callable(work_factory):
            raise TypeError("broker requires a unit-of-work factory")
        self._work_factory, self._lease = work_factory, lease
        self._authority, self._effect = authority, effect
        self._paths, self._exclusive, self._epoch = (
            tuple(owned_paths),
            whole_worktree_exclusive,
            expected_candidate_epoch,
        )
        self._revoked = False

    async def revoke(self) -> None:
        """Prevent further callback admission without disturbing fenced effects."""

        self._revoked = True

    async def __call__(self, call: ProviderToolCall) -> Mapping[str, object]:
        """Adapt the neutral provider frame without accepting authority fields."""

        if not isinstance(call, ProviderToolCall):
            raise BrokerDenied()
        try:
            tool_name = ToolName(call.name)
        except ValueError:
            raise BrokerDenied() from None
        receipt = await self.invoke(
            token=self._authority.broker_token,
            provider_call_key=call.call_key,
            tool_name=tool_name,
            arguments=call.arguments,
        )
        # ``tool_result_frame`` consumes the canonical result itself and
        # determines success from its top-level ``status`` field.  Keep the
        # durable identity additive rather than nesting that result.
        return {"operation_id": str(receipt.operation_id), **dict(receipt.result)}

    async def invoke(
        self,
        *,
        token: str,
        provider_call_key: str,
        tool_name: ToolName,
        arguments: Mapping[str, object],
    ) -> BrokerReceipt:
        if (
            type(token) is not str
            or not hmac.compare_digest(
                token.encode("utf-8", errors="surrogatepass"),
                self._authority.broker_token.encode("utf-8", errors="surrogatepass"),
            )
            or self._revoked
            or type(provider_call_key) is not str
            or _CALL_KEY.fullmatch(provider_call_key) is None
            or not isinstance(tool_name, ToolName)
            or tool_name not in self._authority.permitted_tools
            or not isinstance(arguments, Mapping)
        ):
            raise BrokerDenied()
        try:
            request = ToolRequest(name=tool_name, arguments=arguments)
            if not arguments_match_schema(tool_name, request.arguments):
                raise BrokerDenied()
            paths, exclusive = _effect_scope(request)
            digest = canonical_digest(dict(request.arguments))
        except TypeError, ValueError, RecursionError:
            raise BrokerDenied() from None
        binding = ToolCallBinding(
            attempt_id=self._authority.attempt_id,
            provider_call_key=provider_call_key,
            durable_operation_id=uuid5(
                self._authority.attempt_id, f"forge-subscription-tool-v1:{provider_call_key}"
            ),
            tool_name=tool_name,
            arguments_digest=digest,
        )
        try:
            stored = await self._bind_operation(
                binding, run_id=self._authority.run_id, task_id=self._authority.task_id
            )
        except TypeError, ValueError:
            raise BrokerDenied() from None
        if stored.tool_name is not tool_name or stored.arguments_digest != digest:
            raise BrokerDenied()
        # Revalidate through scheduler even on receipt replay: a stopped,
        # expired, or stale lease may read its existing receipt but may never
        # gain a new admission.  ``admit_effect`` is idempotent for this id.
        try:
            effect_lease, receipt = await self._admit_and_receipt(
                stored,
                owned_paths=tuple(dict.fromkeys((*self._paths, *paths))),
                whole_worktree_exclusive=self._exclusive or exclusive,
                expected_candidate_epoch=self._epoch,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - scheduler failures are provider-safe denials
            raise BrokerDenied() from None
        if receipt is not None:
            return _broker_receipt(stored, receipt)
        try:
            tool_result = await self._effect(
                stored.durable_operation_id, tool_name, request.arguments
            )
        except asyncio.CancelledError:
            await self._reconcile_effect(effect_lease)
            raise
        except BaseException:  # noqa: BLE001 - effect failures cannot leak provider details
            await self._reconcile_effect(effect_lease)
            raise BrokerDenied() from None
        if not isinstance(tool_result, ToolResult) or tool_result.tool_name is not tool_name:
            await self._reconcile_effect(effect_lease)
            raise BrokerDenied()
        try:
            return await self._finalize_effect(
                stored,
                effect_lease,
                tool_result,
                whole_worktree_exclusive=self._exclusive or exclusive,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the unsettled effect fenced on receipt failure
            raise BrokerDenied() from None

    async def _finalize_effect(
        self,
        binding: ToolCallBinding,
        effect: TaskEffectLease,
        result: ToolResult,
        *,
        whole_worktree_exclusive: bool,
    ) -> BrokerReceipt:
        async with self._work_factory() as work:
            # Match subscription admission's run/task/binding -> scheduler lock
            # order. The receipt and fence transition share one commit.
            prior = await work.subscription.operation_receipt(
                binding,
                run_id=self._authority.run_id,
                task_id=self._authority.task_id,
            )
            if prior is not None:
                if self._revoked:
                    raise BrokerDenied()
                await work.scheduler.admit_effect(
                    effect.task_lease,
                    effect.effect_id,
                    whole_worktree_exclusive=whole_worktree_exclusive,
                    expected_candidate_epoch=effect.candidate_epoch,
                )
                receipt = _broker_receipt(binding, prior)
                await work.commit()
                return receipt
            accepted = await work.scheduler.settle_effect(
                effect,
                accepted=result.status is ToolCallStatus.SUCCEEDED and not self._revoked,
            )
            if not accepted and result.status is ToolCallStatus.SUCCEEDED:
                result = ToolResult(
                    tool_name=result.tool_name,
                    status=ToolCallStatus.DENIED,
                    error=ToolError(
                        code=ToolErrorCode.AUTHORIZATION_DENIED,
                        message="tool result acceptance was revoked",
                    ),
                )
            payload = _receipt_result(result)
            await work.subscription.record_operation_receipt(
                binding,
                run_id=self._authority.run_id,
                task_id=self._authority.task_id,
                receipt={"accepted": accepted, "result": payload},
            )
            await work.commit()
            return BrokerReceipt(
                operation_id=binding.durable_operation_id, accepted=accepted, result=payload
            )

    async def _bind_operation(
        self, binding: ToolCallBinding, *, run_id: UUID, task_id: UUID
    ) -> ToolCallBinding:
        async with self._work_factory() as work:
            value = await work.subscription.bind_operation(binding, run_id=run_id, task_id=task_id)
            await work.commit()
            return value

    async def _admit_and_receipt(
        self,
        binding: ToolCallBinding,
        *,
        owned_paths: tuple[str, ...],
        whole_worktree_exclusive: bool,
        expected_candidate_epoch: int | None,
    ) -> tuple[TaskEffectLease, Mapping[str, object] | None]:
        async with self._work_factory() as work:
            receipt = await work.subscription.operation_receipt(
                binding,
                run_id=self._authority.run_id,
                task_id=self._authority.task_id,
            )
            effect = await work.scheduler.admit_effect(
                self._lease,
                binding.durable_operation_id,
                owned_paths=owned_paths,
                whole_worktree_exclusive=whole_worktree_exclusive,
                expected_candidate_epoch=expected_candidate_epoch,
            )
            if self._revoked:
                raise BrokerDenied()
            await work.commit()
            return effect, receipt

    async def _reconcile_effect(self, effect: TaskEffectLease) -> None:
        async with self._work_factory() as work:
            await work.scheduler.reconcile_effect(effect)
            await work.commit()


__all__ = [
    "BrokerDenied",
    "BrokerReceipt",
    "ControlledSubscriptionEffect",
    "SubscriptionToolBroker",
]


def _broker_receipt(binding: ToolCallBinding, receipt: Mapping[str, object]) -> BrokerReceipt:
    result, accepted = receipt.get("result"), receipt.get("accepted")
    if type(accepted) is not bool or not isinstance(result, Mapping):
        raise BrokerDenied()
    return BrokerReceipt(
        operation_id=binding.durable_operation_id, accepted=accepted, result=dict(result)
    )


def _receipt_result(result: ToolResult) -> dict[str, object]:
    """Return a bounded canonical provider receipt, never adapter internals."""

    receipt: dict[str, object] = {
        "tool_name": result.tool_name.value,
        "status": result.status.value,
        "metadata": thaw_payload(result.metadata),
        "artifact_digests": list(result.artifact_digests),
        "operation_intent_id": str(result.operation_intent_id)
        if result.operation_intent_id is not None
        else None,
    }
    if result.error is not None:
        receipt["error"] = {"code": result.error.code.value, "message": result.error.message}
    return receipt


def _effect_scope(request: ToolRequest) -> tuple[tuple[str, ...], bool]:
    """Derive mutation fences from validated arguments, never caller hints."""
    keys: tuple[str, ...]
    if request.name in {ToolName.REPOSITORY_WRITE_FILE, ToolName.REPOSITORY_DELETE_FILE}:
        keys = ("path",)
    elif request.name is ToolName.REPOSITORY_RENAME_FILE:
        keys = ("source", "destination")
    else:
        keys = ()
    paths: list[str] = []
    for key in keys:
        value = request.arguments[key]
        if not isinstance(value, str):
            raise BrokerDenied()
        paths.append(normalize_policy_path(value))
    return tuple(paths), (
        request.name
        in {
            ToolName.GIT_COMMIT,
            ToolName.BUILD_RUN_NAMED_CHECK,
        }
        or (request.name is ToolName.GIT_DIFF and request.arguments.get("scope") == "snapshot")
    )
