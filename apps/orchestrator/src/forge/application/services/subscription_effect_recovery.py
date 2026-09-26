"""Recover fenced subscription effects without reopening tool authority."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from forge.application.ports.tool_recovery import TerminalEffectVerifier
from forge.application.ports.unit_of_work import UnitOfWork


class SubscriptionEffectRecovery:
    """Persist rejected broker receipts for proved, interrupted effects."""

    def __init__(
        self,
        work_factory: Callable[[], AbstractAsyncContextManager[UnitOfWork]],
        *,
        terminal_verifier: TerminalEffectVerifier | None = None,
    ) -> None:
        self._work_factory = work_factory
        self._terminal_verifier = terminal_verifier

    async def reconcile_all(self) -> int:
        settled, cursor = 0, None
        while True:
            async with self._work_factory() as work:
                candidates = await work.subscription.interrupted_effect_ids(cursor, 100)
                await work.rollback()
            if not candidates:
                return settled
            for effect_id in candidates:
                proof = (
                    None
                    if self._terminal_verifier is None
                    else await self._terminal_verifier.verify_terminal_effect(effect_id)
                )
                async with self._work_factory() as work:
                    recovered = (
                        await work.subscription.reconcile_interrupted_effect(effect_id)
                        if proof is None
                        else await work.subscription.reconcile_interrupted_effect(
                            effect_id, verified_terminal=proof
                        )
                    )
                    if recovered:
                        settled += 1
                    await work.commit()
            cursor = candidates[-1]


__all__ = ["SubscriptionEffectRecovery"]
