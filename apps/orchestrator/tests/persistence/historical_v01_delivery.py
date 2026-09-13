"""Current test driver using only APIs present in the pinned v0.1 package."""

import hashlib
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from forge.api.app import create_app
from forge.api.security import CSRF_HEADER, SESSION_COOKIE
from forge.application.services.auth import AuthService
from forge.domain.actor import AgentRole
from forge.domain.agent import DeveloperOutput, ReviewDecision, ReviewOutput
from forge.domain.run import RunState
from forge.domain.tool import ToolCallStatus, ToolName, ToolRequest
from forge.persistence.models import RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from httpx import ASGITransport, AsyncClient
from legacy_tool_fixture import SimulatedWorkerExit, legacy_tools
from pydantic import TypeAdapter
from sqlalchemy import func, update


def fixture_value(value):
    # Frozen tool arguments/results contain mapping proxies. The test pipe uses
    # plain JSON values; production receipts remain owned by the old services.
    return TypeAdapter(type(value)).dump_python(value, mode="json", fallback=dict)


class HistoricalDelivery:
    def __init__(self, case, *, unfinished):
        self.case, self.unfinished = case, unfinished
        self.evidence = {}

    async def execute(self, request, result_for):
        if request.role is AgentRole.REVIEWER:
            assert not self.unfinished
            return result_for(
                request,
                ReviewOutput(
                    decision=ReviewDecision.APPROVE,
                    findings=(),
                    tested_claims=("Historical controller validation retained",),
                    missing_evidence=(),
                    summary="Scripted approval of the frozen counter change",
                ),
            )
        assert request.role is AgentRole.DEVELOPER
        service, context, tree, writer = await legacy_tools(
            self.case, self.case.session_factory, request
        )
        if self.unfinished:
            selected = ToolRequest(
                name=ToolName.REPOSITORY_WRITE_FILE,
                arguments={
                    "path": "src/counter.py",
                    "content": "def increment(value):\n    return value + 1\n",
                },
            )
            receipt = await service.invoke(context, selected)
            assert receipt.status is ToolCallStatus.SUCCEEDED and writer.calls == 1
            self.evidence = {
                "tool_context": fixture_value(context),
                "tool_request": fixture_value(selected),
                "tool_receipt": fixture_value(receipt),
                "write_count": writer.calls,
                "partial_text": (tree.path / "src/counter.py").read_text(),
            }
            raise SimulatedWorkerExit
        calls = []

        async def call(name, arguments):
            receipt = await service.invoke(
                replace(context, invocation_id=uuid4()), ToolRequest(name=name, arguments=arguments)
            )
            calls.append(receipt)
            assert receipt.status is ToolCallStatus.SUCCEEDED, receipt
            return receipt

        source = await call(ToolName.REPOSITORY_READ_FILE, {"path": "src/counter.py"})
        tests = await call(ToolName.REPOSITORY_READ_FILE, {"path": "tests/test_counter.py"})
        await call(
            ToolName.REPOSITORY_WRITE_FILE,
            {
                "path": "src/counter.py",
                "content": source.metadata["content"].replace("value + 2", "value + 1"),
            },
        )
        await call(
            ToolName.REPOSITORY_WRITE_FILE,
            {
                "path": "tests/test_counter.py",
                "content": tests.metadata["content"] + "    assert increment(-1) == 0\n",
            },
        )
        await call(ToolName.BUILD_RUN_NAMED_CHECK, {"command_name": "unit"})
        await call(ToolName.GIT_COMMIT, {"message": "Fix counter increment and negative boundary"})
        assert writer.calls == 2
        candidate = service._git.candidate_diff(tree)
        # Release only this ignored fixture barrier before controller validation.
        barrier = tree.path / ".forge-acceptance" / "slow-unit.release"
        barrier.parent.mkdir(exist_ok=True)
        barrier.write_bytes(b"release\n")
        return result_for(
            request,
            DeveloperOutput(
                summary="Counter repaired with a negative boundary assertion",
                changed_paths=("src/counter.py", "tests/test_counter.py"),
                tests_added_or_changed=("tests/test_counter.py",),
                named_checks_run=("unit",),
                local_commit_sha=candidate.head_sha,
                diff_digest=hashlib.sha256(candidate.diff.text.encode()).hexdigest(),
                unresolved_concerns=(),
                plan_deviations=(),
            ),
            tool_count=len(calls),
        )


async def advance_legacy(case, *, scenario, control):
    auth = AuthService(case.factory)
    operator = await auth.exchange_bootstrap(await auth.issue_bootstrap())
    commands = PostgresCommandRepository(case.session_factory)
    path = f"/api/runs/{case.run.id}"

    async def process(expected):
        queued = await commands.claim_next(worker_id="historical-a9", lease_seconds=60)
        assert queued is not None and queued.command_type == expected
        try:
            async with case.factory() as work:
                await case.handlers[expected](queued, work)
        except SimulatedWorkerExit:
            assert scenario == "unfinished" and expected == "implement"
            # Controlled clock expiry models the exited worker; do not sleep.
            async with case.session_factory() as session, session.begin():
                await session.execute(
                    update(RunCommand)
                    .where(RunCommand.id == queued.id)
                    .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
                )
            return
        await commands.complete(queued.id, worker_id="historical-a9")

    async with AsyncClient(
        transport=ASGITransport(
            app=create_app(case.settings, session_factory=case.session_factory)
        ),
        base_url=case.settings.web_origin,
        headers={"Origin": case.settings.web_origin, CSRF_HEADER: operator.csrf_token},
        cookies={SESSION_COOKIE: operator.session_token},
    ) as client:
        proposal = {
            "gate": "plan",
            "run_version": case.run.version,
            "evidence_digest": case.run.pending_evidence_digest,
        }
        challenge = await client.post(path + "/approval-challenges", json=proposal)
        assert challenge.status_code == 200
        approval = await client.post(
            path + "/approvals", json={**proposal, "challenge_token": challenge.json()["token"]}
        )
        assert approval.status_code == 202
        for expected in ("approve_plan", "prepare_worktree", "implement"):
            await process(expected)
        if scenario == "review":
            for expected in ("validate", "review"):
                await process(expected)
        async with case.factory() as work:
            case.run = await work.runs.get(case.run.id)
        assert case.run.state is (
            RunState.IMPLEMENTING if scenario == "unfinished" else RunState.AWAITING_PR_APPROVAL
        )
        value = {}
        if scenario == "review":
            assert control in ("pause", "cancel")
            body = {"command_type": control, "expected_run_version": case.run.version}
            pending = await client.post(
                path + "/commands", json=body, headers={"Idempotency-Key": "a9-review-control"}
            )
            assert pending.status_code == 202
            value["pending_control"] = pending.json()
            # In-memory pipe handoff only. The parent removes this credential
            # envelope before any provenance, assertion output or manifest.
            value["operator_session"] = fixture_value(operator)
        return value
