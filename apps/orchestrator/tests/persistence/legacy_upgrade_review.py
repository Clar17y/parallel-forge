"""Legacy validation/review evidence with pending authenticated pause/cancel."""

import asyncio
import hashlib
from dataclasses import replace
from uuid import uuid4

from forge.application.ports.commands import CommandLane
from forge.application.services.auth import AuthService
from forge.domain.actor import AgentRole
from forge.domain.agent import DeveloperOutput, ReviewDecision, ReviewOutput
from forge.domain.run import RunState
from forge.domain.tool import ToolCallStatus, ToolName, ToolRequest
from forge.evaluations.subscription_fixtures import release_slow_unit_barrier
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.worker.composition import compose_worker_handlers
from legacy_tool_fixture import legacy_tools
from legacy_upgrade_case import LegacyPlanGateway, legacy_plan_case, legacy_result
from legacy_upgrade_controls import authenticated_tabs, replay_legacy_plan_controls


class ReviewedLegacyGateway(LegacyPlanGateway):
    async def execute(self, request):
        if request.role is AgentRole.PLANNER:
            return await super().execute(request)
        self.requests.append(request)
        if request.role is AgentRole.REVIEWER:
            return legacy_result(
                request,
                ReviewOutput(
                    decision=ReviewDecision.APPROVE,
                    findings=(),
                    tested_claims=("Controller validation retained",),
                    missing_evidence=(),
                    summary="Scripted approval of the frozen counter change",
                ),
            )
        assert request.role is AgentRole.DEVELOPER
        service, context, tree, writer = await legacy_tools(
            self.case, self.session_factory, request
        )
        calls = []

        async def call(name, arguments):
            result = await service.invoke(
                replace(context, invocation_id=uuid4()), ToolRequest(name=name, arguments=arguments)
            )
            calls.append(result)
            assert result.status is ToolCallStatus.SUCCEEDED, result
            return result

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
        self.case.tree, self.case.developer_receipts = tree, calls
        # Fixture synchronization only; this path is ignored by the frozen repo.
        release_slow_unit_barrier(tree.path)
        return legacy_result(
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


async def reviewed_legacy_case(session_factory, tmp_path, *, control):
    gateway = ReviewedLegacyGateway()
    case = await legacy_plan_case(session_factory, tmp_path, gateway=gateway)
    try:
        gateway.case, gateway.session_factory = case, session_factory
        auth = AuthService(case.factory)
        case.operator_session = await auth.exchange_bootstrap(await auth.issue_bootstrap())
        await replay_legacy_plan_controls(case, session_factory, case.operator_session)
        commands = PostgresCommandRepository(session_factory)
        for expected in ("prepare_worktree", "implement", "validate", "review"):
            command = await commands.claim_next(worker_id="a9-review", lease_seconds=60)
            assert command is not None and command.command_type == expected
            async with case.factory() as work:
                await case.handlers[expected](command, work)
            await commands.complete(command.id, worker_id="a9-review")
        async with case.factory() as work:
            case.run = await work.runs.get(case.run.id)
            assert case.run.state is RunState.AWAITING_PR_APPROVAL
            assert await work.subscription.envelope_for_run(case.run.id) is None
        assert len(gateway.requests) == 3
        async with authenticated_tabs(case, session_factory, case.operator_session) as tabs:
            body = {"command_type": control, "expected_run_version": case.run.version}
            path = f"/api/runs/{case.run.id}/commands"
            results = await asyncio.gather(
                *(
                    tab.post(path, json=body, headers={"Idempotency-Key": "a9-review-control"})
                    for tab in tabs
                )
            )
            assert all(item.status_code == 202 for item in results)
            assert results[0].json() == results[1].json()
            case.pending_control = results[0].json()
        return case
    except BaseException:
        await case.handlers.aclose()
        raise


async def replay_review_controls(case, session_factory, *, control):
    gateway = LegacyPlanGateway()
    handlers = compose_worker_handlers(case.settings, session_factory, agent_gateway=gateway)
    commands = PostgresCommandRepository(session_factory)
    path = f"/api/runs/{case.run.id}"
    body = {"command_type": control, "expected_run_version": case.run.version}
    try:
        command = await commands.claim_next(
            worker_id="a9-replay", lease_seconds=30, lane=CommandLane.CONTROL
        )
        assert str(command.id) == case.pending_control["id"]
        for _ in range(2):
            async with case.factory() as work:
                await handlers[control](command, work)
        await commands.complete(command.id, worker_id="a9-replay")
        async with authenticated_tabs(case, session_factory, case.operator_session) as tabs:
            projections = await asyncio.gather(*(tab.get(path) for tab in tabs))
            assert all(item.status_code == 200 for item in projections)
            assert projections[0].json() == projections[1].json()
            current = projections[0].json()
            assert current["state"] == ("PAUSED" if control == "pause" else "CANCELLED")
            assert current["version"] == case.run.version + 1
            replay = await tabs[1].post(
                path + "/commands", json=body, headers={"Idempotency-Key": "a9-review-control"}
            )
            assert replay.status_code == 202 and replay.json()["id"] == case.pending_control["id"]
            stale = await tabs[1].post(
                path + "/commands", json=body, headers={"Idempotency-Key": "a9-review-stale"}
            )
            assert stale.status_code == 409
            for gate in ("plan", "pr", "merge"):
                rejected = await tabs[1].post(
                    path + "/approval-challenges",
                    json={
                        "gate": gate,
                        "run_version": current["version"],
                        "evidence_digest": case.run.pending_evidence_digest,
                    },
                )
                assert rejected.status_code == 409
            if control == "pause":
                resumed = await tabs[0].post(
                    path + "/commands",
                    json={
                        "command_type": "resume",
                        "expected_run_version": current["version"],
                    },
                    headers={"Idempotency-Key": "a9-review-resume"},
                )
                assert resumed.status_code == 202
                resume_command = await commands.claim_next(worker_id="a9-replay", lease_seconds=30)
                assert resume_command.command_type == "resume"
                async with case.factory() as work:
                    await handlers["resume"](resume_command, work)
                await commands.complete(resume_command.id, worker_id="a9-replay")
                current = (await tabs[1].get(path)).json()
                assert current["state"] == "AWAITING_PR_APPROVAL"
                proposal = {
                    "gate": "pr",
                    "run_version": current["version"],
                    "evidence_digest": case.run.pending_evidence_digest,
                }
                for invalid in (
                    {**proposal, "run_version": case.run.version},
                    {**proposal, "evidence_digest": "c" * 64},
                    {**proposal, "gate": "merge"},
                ):
                    assert (
                        await tabs[0].post(path + "/approval-challenges", json=invalid)
                    ).status_code == 409
                # Reading/retaining review evidence cannot authorize PR publication.
                assert (
                    await tabs[0].post(path + "/approval-challenges", json=proposal)
                ).status_code == 200
            details = await asyncio.gather(*(tab.get(path + "/projection") for tab in tabs))
            assert all(item.status_code == 200 for item in details)
            assert details[0].json() == details[1].json()
            detail = details[0].json()
            assert detail["candidate"]["validation_evidence_digest"]
            assert detail["candidate"]["review_evidence_digest"]
            if control == "pause":
                assert (
                    detail["candidate"]["pending_evidence_digest"]
                    == case.run.pending_evidence_digest
                )
                assert detail["next_gate"] == "pr"
            assert "approve_merge" not in {item["name"] for item in detail["available_commands"]}
        assert gateway.requests == []
        assert (
            await commands.claim_next(worker_id="a9-no-automatic-release", lease_seconds=30) is None
        )
        async with case.factory() as work:
            assert await work.subscription.envelope_for_run(case.run.id) is None
            events = await work.events.list_after(case.run.id, 0)
            event_type = "run.paused" if control == "pause" else "run.cancelled"
            assert sum(event.event_type == event_type for event in events) == (
                1 + sum(row["event_type"] == event_type for row in case.legacy_rows["run_events"])
            )
            assert not any(
                event.event_type in ("run.pr_approved", "run.merge_approved") for event in events
            )
        return [case.pending_control, replay.json(), current, detail]
    finally:
        await handlers.aclose()
