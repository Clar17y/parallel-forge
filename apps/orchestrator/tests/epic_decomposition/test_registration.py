"""Applied API and main-worker registration with real decomposition dependencies."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.api.app import create_app
from forge.application.services.epic_decomposition import EpicDecompositionService
from forge.domain.subscription import TaskBudget
from forge.domain.subscription_quota import QuotaPolicy
from forge.settings import Settings

from .support import SupervisedGateway
from .test_persistence import prepared, submit


@pytest.mark.asyncio
async def test_applied_api_composes_real_decomposition_uow(decomposition_session_factory) -> None:
    app = create_app(
        settings=Settings(web_origin="http://127.0.0.1:3000"),
        session_factory=decomposition_session_factory,
        epic_decomposition_budget=TaskBudget(max_provider_attempts=1),
    )
    service = app.state.epic_decomposition_service
    assert isinstance(service, EpicDecompositionService)
    assert service._authoring.budget.max_provider_attempts == 1
    async with service._unit_of_work_factory() as work:
        assert work.jobs is not None and work.epic_items is not None


@pytest.mark.asyncio
async def test_applied_main_dispatches_decomposition_and_drains(
    decomposition_session_factory, monkeypatch
) -> None:
    from forge.worker import main

    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    _, _, _, receipt = await submit(service, actor, epic_id, project_id)
    stop = asyncio.Event()
    observed = {"dispatch": False, "drain": False}

    class FakeEngine:
        dispose = AsyncMock()

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            pass

        async def drain(self):
            pass

    original_drain = main.EpicBrainstormWorker.drain

    async def checked_drain(worker):
        observed["drain"] = True
        await original_drain(worker)

    async def poll_regular(_worker, stop_event, _interval):
        await stop_event.wait()

    async def poll_authoring(worker, stop_event, _interval):
        assert worker.kinds == frozenset(("decomposition",))
        assert await worker.run_once(stop_event=stop_event) == receipt.job_id
        observed["dispatch"] = True
        stop_event.set()

    monkeypatch.setattr(main, "create_engine", lambda _url: FakeEngine())
    monkeypatch.setattr(main, "create_session_factory", lambda _engine: factory)
    monkeypatch.setattr(main, "run_startup_recovery", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "Worker", FakeWorker)
    monkeypatch.setattr(main, "_poll", poll_regular)
    monkeypatch.setattr(main, "_poll_brainstorms", poll_authoring)
    monkeypatch.setattr(main.EpicBrainstormWorker, "drain", checked_drain)
    settings = SimpleNamespace(
        database_url="unused", subscription_installations_path=None,
        subscription_quota_policy=QuotaPolicy(),
    )
    await main.run_worker(
        settings=settings, handlers={}, stop_event=stop,
        worker_id=f"decomposition-test-{uuid4().hex}",
        decomposition_gateway_factory=lambda _job: SupervisedGateway(),
        brainstorm_reader_factory=lambda _job: AsyncMock(),
    )
    assert observed == {"dispatch": True, "drain": True}
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert outcome.state == "proposed" and outcome.process_settled


@pytest.mark.asyncio
async def test_authoring_drain_error_still_closes_other_worker_resources(monkeypatch) -> None:
    from forge.worker import main

    stop = asyncio.Event()
    seen = []

    class FakeEngine:
        async def dispose(self):
            seen.append("engine")

    class FakeWorker:
        def __init__(self, *args, worker_id, **kwargs):
            self.worker_id = worker_id

        async def drain(self):
            seen.append("control" if self.worker_id.endswith("-control") else "regular")

    class FakeAuthoringWorker:
        def __init__(self, *args, **kwargs):
            pass

        async def drain(self):
            seen.append("authoring")
            raise RuntimeError("authoring drain failed")

    async def poll_regular(_worker, stop_event, _interval):
        await stop_event.wait()

    async def poll_authoring(_worker, stop_event, _interval):
        stop_event.set()

    monkeypatch.setattr(main, "create_engine", lambda _url: FakeEngine())
    monkeypatch.setattr(main, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(main, "run_startup_recovery", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "Worker", FakeWorker)
    monkeypatch.setattr(main, "EpicBrainstormWorker", FakeAuthoringWorker)
    monkeypatch.setattr(main, "_poll", poll_regular)
    monkeypatch.setattr(main, "_poll_brainstorms", poll_authoring)
    settings = SimpleNamespace(
        database_url="unused", subscription_installations_path=None,
        subscription_quota_policy=QuotaPolicy(),
    )
    with pytest.raises(RuntimeError, match="authoring drain failed"):
        await main.run_worker(
            settings=settings, handlers={}, stop_event=stop,
            worker_id="cleanup-test", decomposition_gateway_factory=lambda _job: SupervisedGateway(),
            brainstorm_reader_factory=lambda _job: AsyncMock(),
        )
    assert seen == ["authoring", "regular", "control", "engine"]
