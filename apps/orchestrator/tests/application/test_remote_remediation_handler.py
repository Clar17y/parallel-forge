"""Command routing preserves legacy behavior and fails closed without subscription support."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.commands import CommandRecoveryRequired


@pytest.mark.parametrize("mode", ["legacy", "subscription", "unconfigured"])
async def test_remote_handler_uses_only_the_matching_execution_path(mode):
    from forge.application.handlers.remote_remediation import RemoteRemediationHandler

    legacy = SimpleNamespace(execute=AsyncMock(return_value="legacy-result"))
    subscription = SimpleNamespace(execute=AsyncMock(return_value="subscription-result"))
    work = SimpleNamespace(
        subscription=SimpleNamespace(
            envelope_for_run=AsyncMock(return_value=None if mode == "legacy" else object())
        )
    )
    command = SimpleNamespace(run_id=uuid4())
    handler = RemoteRemediationHandler(legacy, None if mode == "unconfigured" else subscription)
    if mode == "unconfigured":
        with pytest.raises(CommandRecoveryRequired, match="not configured"):
            await handler(command, work)
        legacy.execute.assert_not_awaited()
        subscription.execute.assert_not_awaited()
    else:
        assert await handler(command, work) == f"{mode}-result"
        selected, other = (legacy, subscription) if mode == "legacy" else (subscription, legacy)
        selected.execute.assert_awaited_once_with(command, work)
        other.execute.assert_not_awaited()
