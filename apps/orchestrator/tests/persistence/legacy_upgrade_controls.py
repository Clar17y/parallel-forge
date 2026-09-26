"""Two real authenticated API clients exercise retained legacy plan controls."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

from forge.api.app import create_app
from forge.api.security import CSRF_HEADER, SESSION_COOKIE
from forge.application.ports.commands import CommandLane
from forge.domain.run import RunState
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.worker.composition import compose_worker_handlers
from httpx import ASGITransport, AsyncClient
from legacy_upgrade_case import LegacyPlanGateway


@asynccontextmanager
async def authenticated_tabs(case, session_factory, operator_session):
    origin = case.settings.web_origin
    headers = {"Origin": origin, CSRF_HEADER: operator_session.csrf_token}
    async with AsyncExitStack() as stack:
        yield [
            await stack.enter_async_context(
                AsyncClient(
                    transport=ASGITransport(
                        app=create_app(case.settings, session_factory=session_factory)
                    ),
                    base_url=origin,
                    headers=headers,
                    cookies={SESSION_COOKIE: operator_session.session_token},
                )
            )
            for _ in range(2)
        ]


async def replay_legacy_plan_controls(case, session_factory, operator_session):
    gateway = LegacyPlanGateway()
    handlers = compose_worker_handlers(case.settings, session_factory, agent_gateway=gateway)
    commands = PostgresCommandRepository(session_factory)
    path = f"/api/runs/{case.run.id}"
    responses = []

    async def process(expected, lane=CommandLane.CONTROL):
        value = await commands.claim_next(worker_id="a9-control", lease_seconds=30, lane=lane)
        assert value is not None and value.command_type == expected
        async with case.factory() as work:
            await handlers[expected](value, work)
        await commands.complete(value.id, worker_id="a9-control")

    try:
        async with authenticated_tabs(case, session_factory, operator_session) as tabs:
            original = await asyncio.gather(*(tab.get(path) for tab in tabs))
            assert all(item.status_code == 200 for item in original)
            assert original[0].json() == original[1].json()
            version = original[0].json()["version"]
            body = {"command_type": "pause", "expected_run_version": version}
            pause = await asyncio.gather(
                *(
                    tab.post(path + "/commands", json=body, headers={"Idempotency-Key": "a9-pause"})
                    for tab in tabs
                )
            )
            assert all(item.status_code == 202 for item in pause)
            assert pause[0].json()["id"] == pause[1].json()["id"]
            await process("pause")
            paused = await asyncio.gather(*(tab.get(path) for tab in tabs))
            assert paused[0].json() == paused[1].json()
            assert paused[0].json()["state"] == "PAUSED"
            replay = await tabs[1].post(
                path + "/commands", json=body, headers={"Idempotency-Key": "a9-pause"}
            )
            assert replay.status_code == 202 and replay.json()["id"] == pause[0].json()["id"]
            stale = await tabs[1].post(
                path + "/commands", json=body, headers={"Idempotency-Key": "a9-stale"}
            )
            assert stale.status_code == 409
            changed = await tabs[1].post(
                path + "/commands",
                json={**body, "command_type": "cancel"},
                headers={"Idempotency-Key": "a9-pause"},
            )
            assert changed.status_code == 409
            resume = await tabs[0].post(
                path + "/commands",
                json={
                    "command_type": "resume",
                    "expected_run_version": paused[0].json()["version"],
                },
                headers={"Idempotency-Key": "a9-resume"},
            )
            assert resume.status_code == 202
            await process("resume", CommandLane.NORMAL)
            resumed = await tabs[1].get(path)
            assert resumed.json()["state"] == "AWAITING_PLAN_APPROVAL"
            current_version = resumed.json()["version"]
            proposal = {
                "gate": "plan",
                "run_version": current_version,
                "evidence_digest": case.run.pending_evidence_digest,
            }
            for invalid in (
                {**proposal, "run_version": version},
                {**proposal, "evidence_digest": "b" * 64},
                {**proposal, "gate": "pr"},
                {**proposal, "gate": "merge"},
            ):
                rejected = await tabs[1].post(path + "/approval-challenges", json=invalid)
                assert rejected.status_code == 409
            challenge = await tabs[0].post(path + "/approval-challenges", json=proposal)
            assert challenge.status_code == 200
            authorized = {**proposal, "challenge_token": challenge.json()["token"]}
            approval = await tabs[0].post(path + "/approvals", json=authorized)
            assert approval.status_code == 202
            consumed = await tabs[1].post(path + "/approvals", json=authorized)
            assert consumed.status_code == 409
            await process("approve_plan", CommandLane.NORMAL)
            prepared = await asyncio.gather(*(tab.get(path) for tab in tabs))
            assert prepared[0].json() == prepared[1].json()
            assert prepared[0].json()["state"] == "PREPARING_WORKTREE"
            # Only safe acknowledgements/projections are retained, never session
            # credentials or one-time human-approval challenge tokens.
            responses = [
                original[0].json(),
                pause[0].json(),
                paused[0].json(),
                replay.json(),
                resume.json(),
                resumed.json(),
                approval.json(),
                prepared[0].json(),
            ]
        async with case.factory() as work:
            run = await work.runs.get(case.run.id)
            assert run.state is RunState.PREPARING_WORKTREE
            assert await work.subscription.envelope_for_run(run.id) is None
        assert gateway.requests == []
        return responses
    finally:
        await handlers.aclose()
