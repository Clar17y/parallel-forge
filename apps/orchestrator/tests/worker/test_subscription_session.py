"""Session composition owns the broker binding and process lifecycle."""

from dataclasses import replace

import pytest
from forge.agents.subscription_protocol import ProviderToolCall
from forge.application.services.subscription_broker import BrokerDenied
from forge.domain.run import RunState
from forge.domain.tool import ToolName
from forge.worker.subscription_session import ControlledSubscriptionSessionFactory
from test_subscription_attempt_runner import invocation


async def test_session_revocation_denies_callbacks_before_tool_setup():
    admission, request = invocation()
    request = replace(
        request,
        run_state=RunState.IMPLEMENTING,
        attempt_budget=replace(request.task.budget, max_provider_attempts=1, max_repairs=0),
    )
    captured = []

    async def forbidden_tools(*args):
        pytest.fail("revoked callback must not construct tools")

    def gateway(admitted, supplied, broker, lifecycle):
        assert admitted == admission and supplied == request
        captured.append(broker)

        class Gateway:
            async def execute(self, value):
                raise AssertionError("not invoked")

        return Gateway()

    session = ControlledSubscriptionSessionFactory(
        lambda: pytest.fail("unexpected DB"), tools=forbidden_tools, gateway=gateway
    )(admission, request)
    await session.revoke()
    with pytest.raises(BrokerDenied):
        await captured[0](
            ProviderToolCall(
                call_key="read",
                thread_id="t",
                turn_id="v",
                name=ToolName.REPOSITORY_READ_FILE.value,
                arguments={"path": "README.md"},
            )
        )


@pytest.mark.parametrize("missing", ["phase", "budget", "identity"])
def test_session_requires_durable_request_context(missing):
    admission, request = invocation()
    request = replace(
        request,
        run_state=RunState.IMPLEMENTING,
        attempt_budget=replace(request.task.budget, max_provider_attempts=1, max_repairs=0),
    )
    if missing == "phase":
        request = replace(request, run_state=None)
    elif missing == "budget":
        request = replace(request, attempt_budget=None)
    else:
        admission = replace(admission, task=replace(admission.task, owned_paths=("other",)))
    factory = ControlledSubscriptionSessionFactory(
        lambda: None,
        tools=lambda *args: None,
        gateway=lambda *args: pytest.fail("must not construct gateway"),
    )
    with pytest.raises(ValueError, match="durable admission"):
        factory(admission, request)
