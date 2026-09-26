"""Scripted A2 roles exercise the real broker, integration and review lifecycle."""

import asyncio
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from forge.agents.subscription_protocol import ProviderToolCall
from forge.application.ports.scheduling import SchedulingConflict
from forge.application.services.subscription_broker import BrokerDenied
from forge.domain.agent import ReviewDecision, ReviewOutput
from forge.domain.run import RunState
from forge.domain.subscription import (
    AcceptDecision,
    CheckResultEvidence,
    DelegateDecision,
    HandoffStatus,
    LogicalTaskContract,
    ReasoningEffort,
    ReviewedTaskHandoff,
    ReviewSelection,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
    TaskHandoff,
    WaitDecision,
)
from forge.domain.tool import ToolName
from forge.evaluations.subscription_fixtures import (
    clean_slow_unit_markers,
    release_slow_unit_barrier,
    wait_slow_unit_entered,
)
from test_subscription_parallel_acceptance import ParallelCatalogScript

OPUS = RouteSpec(
    provider="anthropic", client="claude", model="claude-opus-5", effort=ReasoningEffort.MEDIUM
)
CATALOG_PATHS = ("alpha/value.txt", "beta/value.txt")


class CatalogLifecycleScript(ParallelCatalogScript):
    def __init__(self):
        super().__init__()
        self.repeat_id, self.integration_id = uuid4(), uuid4()
        self.alpha_release = asyncio.Event()
        self.commit_entered, self.commit_release = threading.Event(), threading.Event()
        self.handoffs = {}
        self.primary_stage = 0
        self.worktree = None
        self.barrier_observations = {}
        self.observe_barrier = None

    def child(self, request, identity, purpose, paths, checks=()):
        return LogicalTaskContract(
            run_id=request.task.run_id,
            task_id=identity,
            parent_task_id=request.task.task_id,
            purpose=purpose,
            route=request.envelope.route_for(purpose),
            owned_paths=paths,
            named_checks=checks,
            budget=TaskBudget(max_provider_attempts=1),
        )

    def delegate(self, request, children, rationale):
        return DelegateDecision(
            run_id=request.task.run_id,
            parent_task_id=request.task.task_id,
            child_tasks=children,
            rationale=rationale,
        )

    def primary(self, request):
        if self.primary_stage == 0:
            self.primary_stage = 1
            return self.delegate(
                request,
                tuple(
                    self.child(request, identity, SpecialistPurpose.ROUTINE_IMPLEMENTATION, (path,))
                    for identity, path in zip(self.children.values(), CATALOG_PATHS, strict=True)
                ),
                "Disjoint workers own complete catalog edits",
            )
        if self.primary_stage == 1:
            self.primary_stage = 2
            return self.delegate(
                request,
                (
                    self.child(
                        request,
                        self.repeat_id,
                        SpecialistPurpose.ROUTINE_IMPLEMENTATION,
                        (CATALOG_PATHS[0],),
                    ),
                ),
                "Inspect alpha again after the current alpha owner has settled",
            )
        if self.repeat_id not in self.handoffs:
            return WaitDecision(
                run_id=request.task.run_id,
                task_id=request.task.task_id,
                waiting_on_task_ids=(self.repeat_id,),
                reason="The conflicting alpha task must settle",
            )
        if self.primary_stage == 2:
            self.primary_stage = 3
            return self.delegate(
                request,
                (
                    self.child(
                        request,
                        self.integration_id,
                        SpecialistPurpose.INTEGRATION,
                        CATALOG_PATHS,
                        ("unit", "slow-unit"),
                    ),
                ),
                "Integrate all settled changes in one checkpoint and validate the whole catalog",
            )
        if self.integration_id not in self.handoffs:
            return WaitDecision(
                run_id=request.task.run_id,
                task_id=request.task.task_id,
                waiting_on_task_ids=(self.integration_id,),
                reason="Integration must settle before review",
            )
        integration = self.handoffs[self.integration_id]
        if self.primary_stage == 3:
            self.primary_stage = 4
            return ReviewSelection(
                run_id=request.task.run_id,
                candidate_commit=integration.candidate_commit,
                candidate_tree_digest=integration.candidate_tree_digest,
                review_required=True,
                reviewer_route=OPUS,
            )
        review = self.handoffs["review"]
        return AcceptDecision(
            run_id=request.task.run_id,
            task_id=request.task.task_id,
            candidate_commit=integration.candidate_commit,
            candidate_tree_digest=integration.candidate_tree_digest,
            evidence_receipt_ids=(*integration.evidence_receipt_ids, *review.evidence_receipt_ids),
            rationale="Accept the settled integrated checkpoint, bound checks and fresh review",
        )

    async def execute(self, request, broker):
        if request.run_state is RunState.PLANNING:
            plan = await super().execute(request, broker)
            return plan.model_copy(update={"required_checks": ("unit", "slow-unit")})
        if request.task.purpose is SpecialistPurpose.PRIMARY:
            return self.primary(request)

        async def call(key, tool, arguments):
            receipt = await broker(
                ProviderToolCall(
                    call_key=key,
                    thread_id="a2-lifecycle",
                    turn_id=str(request.attempt.attempt_id),
                    name=tool.value,
                    arguments=arguments,
                )
            )
            self.receipts.append(receipt)
            return receipt

        if request.task.purpose is SpecialistPurpose.INTEGRATION:
            return await self.integrate(request, call)
        if request.task.purpose is SpecialistPurpose.INDEPENDENT_REVIEW:
            for path in CATALOG_PATHS:
                read = await call(
                    f"read-{path.split('/')[0]}", ToolName.REPOSITORY_READ_FILE, {"path": path}
                )
                assert read["status"] == "succeeded"
                assert read["metadata"]["content"] == f"{path.split('/')[0]}-v2\n"
            with pytest.raises(BrokerDenied):
                await call(
                    "review-write",
                    ToolName.REPOSITORY_WRITE_FILE,
                    {"path": CATALOG_PATHS[0], "content": "forbidden\n"},
                )
            snapshot = await call("review-snapshot", ToolName.GIT_DIFF, {"scope": "snapshot"})
            assert snapshot["status"] == "succeeded"
            handoff = ReviewedTaskHandoff(
                run_id=request.task.run_id,
                task_id=request.task.task_id,
                attempt_id=request.attempt.attempt_id,
                status=HandoffStatus.COMPLETED,
                candidate_commit=self.handoffs[self.integration_id].candidate_commit,
                candidate_tree_digest=snapshot["metadata"]["candidate_tree_digest"],
                evidence_receipt_ids=(snapshot["operation_id"],),
                summary="Inspected the frozen catalog independently through read-only tools",
                review_output=ReviewOutput(
                    decision=ReviewDecision.APPROVE,
                    missing_evidence=(),
                    tested_claims=("Both exact catalog values read from the frozen candidate",),
                    summary="No catalog findings",
                ),
            )
            self.handoffs["review"] = handoff
            return handoff
        if request.task.task_id == self.repeat_id:
            read = await call(
                "settled-alpha", ToolName.REPOSITORY_READ_FILE, {"path": CATALOG_PATHS[0]}
            )
            assert read["metadata"]["content"] == "alpha-v2\n"
        else:
            name = next(
                name for name, identity in self.children.items() if identity == request.task.task_id
            )
            self.entered[name].set()
            self.any_entered.set()
            await asyncio.wait_for(self.release.wait(), 20)
            if name == "beta":
                with pytest.raises(BrokerDenied):
                    await call(
                        "foreign",
                        ToolName.REPOSITORY_WRITE_FILE,
                        {"path": CATALOG_PATHS[0], "content": "foreign\n"},
                    )
                self.denial = True
            written = await call(
                "write",
                ToolName.REPOSITORY_WRITE_FILE,
                {"path": f"{name}/value.txt", "content": f"{name}-v2\n"},
            )
            assert written["status"] == "succeeded"
            self.written[name].set()
            await asyncio.wait_for(
                asyncio.gather(*(value.wait() for value in self.written.values())), 20
            )
            if name == "alpha":
                await asyncio.wait_for(self.alpha_release.wait(), 30)
        snapshot = await call("snapshot", ToolName.GIT_DIFF, {"scope": "snapshot"})
        assert snapshot["status"] == "succeeded"
        return self.handoff(request, snapshot)

    def handoff(self, request, snapshot, *, checks=(), commit=None):
        handoff = TaskHandoff(
            run_id=request.task.run_id,
            task_id=request.task.task_id,
            attempt_id=request.attempt.attempt_id,
            status=HandoffStatus.COMPLETED,
            candidate_tree_digest=snapshot["metadata"]["candidate_tree_digest"],
            candidate_commit=commit["metadata"]["new_sha"] if commit else None,
            changed_paths=request.task.owned_paths,
            check_results=tuple(
                CheckResultEvidence(
                    command_name=name,
                    exit_code=0,
                    passed=True,
                    output_digest=item["metadata"]["command_result_digest"],
                    duration_ms=item["metadata"]["command_duration_ms"],
                    receipt_id=item["operation_id"],
                )
                for name, item in zip(request.task.named_checks, checks, strict=True)
            ),
            evidence_receipt_ids=tuple(
                item["operation_id"] for item in (*checks, *((commit,) if commit else ()), snapshot)
            ),
            summary="Completed owned catalog outcome with actual receipts",
        )
        self.handoffs[request.task.task_id] = handoff
        return handoff

    async def denied_during_barrier(self, name, request, call):
        if self.observe_barrier:
            self.barrier_observations[name] = await self.observe_barrier(name, request)
        with pytest.raises((BrokerDenied, SchedulingConflict)):
            await call(
                f"write-during-{name}",
                ToolName.REPOSITORY_WRITE_FILE,
                {"path": CATALOG_PATHS[0], "content": "barrier-violation\n"},
            )

    async def integrate(self, request, call):
        assert isinstance(self.worktree, Path)
        commit_task = asyncio.create_task(
            call(
                "checkpoint",
                ToolName.GIT_COMMIT,
                {"message": "Integrate both settled catalog edits"},
            )
        )
        try:
            assert await asyncio.to_thread(self.commit_entered.wait, 10)
            await self.denied_during_barrier("checkpoint", request, call)
        finally:
            self.commit_release.set()
            commit = await commit_task
        assert commit["status"] == "succeeded"
        unit = await call("unit", ToolName.BUILD_RUN_NAMED_CHECK, {"command_name": "unit"})
        clean_slow_unit_markers(self.worktree)
        slow_task = asyncio.create_task(
            call("slow-unit", ToolName.BUILD_RUN_NAMED_CHECK, {"command_name": "slow-unit"})
        )
        try:
            assert await asyncio.to_thread(wait_slow_unit_entered, self.worktree)
            await self.denied_during_barrier("slow-unit", request, call)
        finally:
            release_slow_unit_barrier(self.worktree)
            slow = await slow_task
        assert unit["status"] == slow["status"] == "succeeded"
        snapshot = await call("integrated-snapshot", ToolName.GIT_DIFF, {"scope": "snapshot"})
        assert snapshot["status"] == "succeeded"
        return self.handoff(request, snapshot, checks=(unit, slow), commit=commit)
