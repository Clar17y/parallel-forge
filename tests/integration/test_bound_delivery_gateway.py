"""Production delivery gateway derives tools from committed execution authority."""

import json

import pytest
from forge.agents.errors import AgentGatewayError
from forge.application.services.tools import ControlledToolService
from forge.domain.agent import UntrustedContent, UntrustedSourceKind
from forge.persistence.models import Run
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.worker.bound_delivery import BoundDeliveryGateway
from sqlalchemy import text
from test_delivery_development import _service_case
from test_delivery_review import _review_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("role", ["developer", "reviewer"])
@pytest.mark.parametrize("tamper", [False, True, "resumed", "legacy"])
async def test_delivery_gateway_binds_only_the_durable_role_context(
    tmp_path, workflow_session_factory, role, tamper
):
    factory = workflow_session_factory
    if role == "developer":
        case, command, service, gateway, git = await _service_case(tmp_path, factory)
    else:
        case, command, service, gateway, git, _ = await _review_case(tmp_path, factory)
    if tamper == "legacy":
        original_put = service._put

        async def legacy_context(data):
            value = json.loads(data)
            if set(value) == {"schema_version", "execution_id", "context"}:
                data = json.dumps(
                    value["context"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            return await original_put(data)

        service._put = legacy_context
    built = []

    async def tools_for(request, approved, worktree):
        built.append((request, worktree))
        return ControlledToolService(
            unit_of_work_factory=lambda: PostgresUnitOfWork(factory),
            artifact_store=case.artifact_store,
            controlled_git=git,
            worktree=worktree,
        )

    class Underlying:
        def __init__(self, provider):
            self.provider = provider

        async def execute(self, request):
            assert self.provider.tools_for(request).names == request.allowed_tools
            # Provider invocation must not retain the run's transaction lock.
            async with factory() as session, session.begin():
                await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                assert await session.get(Run, request.run_id, with_for_update=True) is not None
            return await gateway.execute(request)

    bound = BoundDeliveryGateway(
        unit_of_work_factory=lambda: PostgresUnitOfWork(factory),
        artifact_store=case.artifact_store,
        prompt_loader=service._prompts,
        git_factory=lambda _policy: git,
        tool_service_factory=tools_for,
        gateway_factory=Underlying,
    )

    class Invoker:
        async def execute(self, request):
            if tamper == "resumed":
                async with PostgresUnitOfWork(factory) as work:
                    run = await work.runs.get(request.run_id)
                    paused = await work.runs.pause(run.id, run.version, "test.paused", {})
                    await work.runs.resume(run.id, paused.version, "test.resumed", {})
                    await work.commit()
                with pytest.raises(AgentGatewayError):
                    await bound.execute(request)
                assert not built and not gateway.requests
                raise RuntimeError("test stopped after refused stale admission")
            if tamper is True:
                changed = request.context.model_copy(
                    update={
                        "original_task": UntrustedContent.from_text(
                            "Unapproved task",
                            source_kind=UntrustedSourceKind.TASK,
                            source_reference="injected",
                        )
                    }
                )
                with pytest.raises(AgentGatewayError):
                    await bound.execute(request.model_copy(update={"context": changed}))
                assert not built and not gateway.requests
            return await bound.execute(request)

    service._gateway = Invoker()
    async with PostgresUnitOfWork(factory) as work:
        if tamper == "resumed":
            with pytest.raises(RuntimeError, match="test stopped"):
                await service.execute(command, work)
            return
        await service.execute(command, work)
    assert len(gateway.requests) == 1
    assert len(built) == 1
    expected_path = git.path if role == "developer" else git.worktree.path
    assert built[0][1].path == expected_path
