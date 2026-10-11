"""Providerless tests for Google Gemini and Antigravity task usage observations."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from forge.agents.antigravity_runtime import (
    AntigravityGateway,
    AntigravityInstallation,
    _context_from_payload,
    _ObservationPublisher,
    _tokens_from_usage,
    _validate_authoring_usage_binding,
    _validate_subscription_usage_binding,
)
from forge.agents.epic_brainstorm_gateway import (
    EpicBrainstormGateway,
)
from forge.agents.gemini_gateway import GeminiGateway, GeminiInstallation
from forge.application.ports.epic_brainstorm import (
    BrainstormGatewayResult,
)
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInterrupted,
    SubscriptionInvocationRequest,
    SubscriptionInvocationResult,
)
from forge.application.ports.task_usage import TaskUsageObserver
from forge.application.services.task_usage_observations import project_source
from forge.domain.epic_brainstorm import (
    AuthoringJobSnapshot,
    BrainstormProposal,
    FrozenBriefContent,
)
from forge.domain.subscription import (
    AttemptIdentity,
    AttemptTelemetry,
    AuthMode,
    BillingMode,
    HandoffStatus,
    ReasoningEffort,
    RouteBinding,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
    TaskHandoff,
)
from forge.domain.subscription_installations import (
    GeminiInstallationSpec,
    InstallationQuota,
)
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.domain.task_usage_contract import (
    ContextWindow,
    Observation,
    RouteCohort,
    RouteMember,
    SourceBinding,
    SubjectKey,
    TokenCounts,
    WorkUnitBinding,
)
from forge.tools.epic_brainstorm import BrainstormReadOnlyTools
from forge.tools.repository import RepositoryReader


class FakeUsageObserver(TaskUsageObserver):
    def __init__(self, *, hang: bool = False, fail: bool = False) -> None:
        self.observations: list[Observation] = []
        self.hang = hang
        self.fail = fail

    async def observe_usage(self, observation: Observation) -> None:
        if self.hang:
            await asyncio.sleep(10)
        if self.fail:
            raise RuntimeError("Observer database down")
        self.observations.append(observation)


def test_google_usage_zero_large_and_inconsistent_context() -> None:
    assert _tokens_from_usage({"input_tokens": 10_000_001, "output_tokens": 0}).input_tokens == 10_000_001
    measured = _context_from_payload({"context": {"occupied_tokens": 0, "capacity_tokens": 100}})
    assert measured is not None and measured.occupied_tokens == 0
    inconsistent = _context_from_payload({"context": {"occupied_tokens": 200, "capacity_tokens": 100}})
    assert inconsistent is not None and inconsistent.capacity_tokens is None


def _make_subscription_binding(
    run_id: UUID,
    attempt_id: UUID,
    task_id: UUID,
    *,
    project_id: UUID | None = None,
    model: str = "gemini-3.8-flash-medium",
    provider: str = "google",
    role: str = "routine_implementation",
    effort: str | None = "medium",
) -> WorkUnitBinding:
    cohort = RouteCohort.from_members(
        (
            RouteMember(
                role=role,
                provider=provider,
                model=model,
                ordinal=0,
                reasoning_effort=effort,
            ),
        )
    )
    return WorkUnitBinding(
        work_unit_id=uuid4(),
        admitted_phase_id="plan_and_code",
        configured_route=cohort,
        effective_route=cohort,
        subject=SubjectKey(project_id=project_id or uuid4(), kind="run", subject_id=run_id),
        source=SourceBinding(kind="subscription_attempt", source_id=attempt_id, parent_id=task_id),
    )


def _make_authoring_binding(
    job_id: UUID,
    project_id: UUID,
    attempt_id: UUID,
    *,
    model: str = "gemini-3.8-flash-medium",
    provider: str = "google",
    role: str = "authoring",
    effort: str | None = "low",
) -> WorkUnitBinding:
    cohort = RouteCohort.from_members(
        (
            RouteMember(
                role=role,
                provider=provider,
                model=model,
                ordinal=0,
                reasoning_effort=effort,
            ),
        )
    )
    return WorkUnitBinding(
        work_unit_id=uuid4(),
        admitted_phase_id="authoring_exchange",
        configured_route=cohort,
        effective_route=cohort,
        subject=SubjectKey(project_id=project_id, kind="authoring_job", subject_id=job_id),
        source=SourceBinding(kind="authoring_attempt", source_id=attempt_id, parent_id=None),
    )


def _make_authoring_job(
    job_id: UUID,
    project_id: UUID,
    *,
    max_input: int | None = None,
) -> AuthoringJobSnapshot:
    route = RouteSpec(
        provider="google",
        client="gemini_cli",
        model="scripted-brainstorm",
        effort=ReasoningEffort.LOW,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    return AuthoringJobSnapshot(
        job_id=job_id,
        epic_id=uuid4(),
        project_id=project_id,
        conversation_id=uuid4(),
        prompt_turn_id=uuid4(),
        budget=TaskBudget(
            max_duration_seconds=5,
            max_tool_calls=10,
            max_named_checks=5,
            max_provider_attempts=2,
            max_repairs=3,
            max_input_tokens=max_input,
            max_output_tokens=None,
        ),
        route=RouteBinding(requested=route, effective=route),
        input_brief_revision_id=uuid4(),
        input_brief_digest="0" * 64,
        input_draft_digest="0" * 64,
        draft_content=FrozenBriefContent(
            problem="Test problem", requirements=(), open_questions=()
        ),
        expected_epic_version=1,
        conversation_version=1,
        reservation_id=uuid4(),
    )


class MockLifecycle:
    def __init__(self, attempt_id: UUID | None = None) -> None:
        self.attempt_id = attempt_id or uuid4()
        self.launch_ids: list[str] = []
        self.receipts: list[object] = []
        self.results: list[object] = []

    async def launch_intent(self, launch_id: str) -> None:
        self.launch_ids.append(launch_id)

    async def started(self, receipt: object) -> None:
        self.receipts.append(receipt)

    async def finished(self, receipt: object, result: object) -> None:
        self.results.append((receipt, result))


# --- Protocol Conformance Tests ---

def test_gateways_implement_observable_subscription_gateway_protocol() -> None:
    assert hasattr(GeminiGateway, "execute_observed")
    assert callable(GeminiGateway.execute_observed)
    assert hasattr(AntigravityGateway, "execute_observed")
    assert callable(AntigravityGateway.execute_observed)
    assert hasattr(EpicBrainstormGateway, "execute_observed")
    assert callable(EpicBrainstormGateway.execute_observed)


# --- WorkUnitBinding Validation Tests ---

def test_subscription_binding_validation() -> None:
    run_id, attempt_id, task_id = uuid4(), uuid4(), uuid4()
    req = AsyncMock(spec=SubscriptionInvocationRequest)
    req.attempt = AttemptIdentity(run_id=run_id, task_id=task_id, attempt_id=attempt_id)
    route_spec = RouteSpec(
        provider="google",
        client="gemini_cli",
        model="gemini-3.8-flash-medium",
        effort=ReasoningEffort.MEDIUM,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    req.task = AsyncMock()
    req.task.route = RouteBinding(requested=route_spec, effective=route_spec)
    req.task.purpose = SpecialistPurpose.ROUTINE_IMPLEMENTATION

    valid_binding = _make_subscription_binding(run_id, attempt_id, task_id, model="gemini-3.8-flash-medium")
    _validate_subscription_usage_binding(valid_binding, req, "gemini-3.8-flash-medium")
    with pytest.raises(ValueError, match="route"):
        _validate_subscription_usage_binding(
            _make_subscription_binding(run_id, attempt_id, task_id, role="review"),
            req, "gemini-3.8-flash-medium",
        )
    with pytest.raises(ValueError, match="route"):
        _validate_subscription_usage_binding(
            _make_subscription_binding(run_id, attempt_id, task_id, effort="high"),
            req, "gemini-3.8-flash-medium",
        )

    # Mismatched subject kind
    with pytest.raises(ValueError, match="run subject"):
        bad = _make_authoring_binding(run_id, uuid4(), attempt_id)
        _validate_subscription_usage_binding(bad, req, "gemini-3.8-flash-medium")

    # Mismatched run id
    with pytest.raises(ValueError, match="run id"):
        bad = _make_subscription_binding(uuid4(), attempt_id, task_id)
        _validate_subscription_usage_binding(bad, req, "gemini-3.8-flash-medium")

    # Mismatched attempt id
    with pytest.raises(ValueError, match="attempt id"):
        bad = _make_subscription_binding(run_id, uuid4(), task_id)
        _validate_subscription_usage_binding(bad, req, "gemini-3.8-flash-medium")

    # Mismatched task id
    with pytest.raises(ValueError, match="task id"):
        bad = _make_subscription_binding(run_id, attempt_id, uuid4())
        _validate_subscription_usage_binding(bad, req, "gemini-3.8-flash-medium")

    # Mismatched model
    with pytest.raises(ValueError, match="route"):
        _validate_subscription_usage_binding(valid_binding, req, "other-model")


def test_authoring_binding_validation() -> None:
    job_id, project_id, attempt_id = uuid4(), uuid4(), uuid4()
    job = _make_authoring_job(job_id, project_id)

    valid_binding = _make_authoring_binding(job_id, project_id, attempt_id, model="gemini-test")
    _validate_authoring_usage_binding(valid_binding, job, attempt_id, expected_model="gemini-test")
    with pytest.raises(ValueError, match="route"):
        _validate_authoring_usage_binding(
            _make_authoring_binding(job_id, project_id, attempt_id, model="gemini-test", role="review"),
            job, attempt_id, expected_model="gemini-test",
        )
    with pytest.raises(ValueError, match="route"):
        _validate_authoring_usage_binding(
            _make_authoring_binding(job_id, project_id, attempt_id, model="gemini-test", effort="high"),
            job, attempt_id, expected_model="gemini-test",
        )

    # Wrong subject kind
    with pytest.raises(ValueError, match="authoring_job subject"):
        bad = _make_subscription_binding(job_id, attempt_id, uuid4())
        _validate_authoring_usage_binding(bad, job, attempt_id)

    # Wrong job id
    with pytest.raises(ValueError, match="job id"):
        bad = _make_authoring_binding(uuid4(), project_id, attempt_id)
        _validate_authoring_usage_binding(bad, job, attempt_id)

    # Wrong project id
    with pytest.raises(ValueError, match="project"):
        bad = _make_authoring_binding(job_id, uuid4(), attempt_id)
        _validate_authoring_usage_binding(bad, job, attempt_id)

    # Wrong attempt id
    with pytest.raises(ValueError, match="attempt id"):
        bad = _make_authoring_binding(job_id, project_id, uuid4())
        _validate_authoring_usage_binding(bad, job, attempt_id)

    # Wrong model
    with pytest.raises(ValueError, match="model"):
        _validate_authoring_usage_binding(valid_binding, job, attempt_id, expected_model="wrong-model")


# --- ObservationPublisher Unit Tests ---

@pytest.mark.asyncio
async def test_publisher_filters_duplicate_live_observations() -> None:
    sink = FakeUsageObserver()
    binding = _make_subscription_binding(uuid4(), uuid4(), uuid4())
    pub = _ObservationPublisher(binding=binding, sink=sink)

    tokens1 = TokenCounts(input_tokens=100, output_tokens=50, cached_input_tokens=10, reasoning_output_tokens=5)
    ctx1 = ContextWindow(occupied_tokens=100, capacity_tokens=1000)

    await pub.emit_live(tokens=tokens1, context=ctx1)
    assert len(sink.observations) == 1
    assert sink.observations[0].sequence == 1
    assert sink.observations[0].final is False
    assert sink.observations[0].tokens.input_tokens == 100
    assert sink.observations[0].context is not None
    assert sink.observations[0].context.occupied_tokens == 100

    # Identical emission must be filtered out
    await pub.emit_live(tokens=tokens1, context=ctx1)
    assert len(sink.observations) == 1

    # Changed tokens should emit
    tokens2 = TokenCounts(input_tokens=120, output_tokens=60, cached_input_tokens=10, reasoning_output_tokens=5)
    await pub.emit_live(tokens=tokens2, context=ctx1)
    assert len(sink.observations) == 2
    assert sink.observations[1].sequence == 2
    assert sink.observations[1].tokens.input_tokens == 120

    # Final emission reconciles last live event id
    await pub.emit_final(tokens=tokens2, status="succeeded", context=ctx1)
    assert len(sink.observations) == 3
    assert sink.observations[2].sequence == 3
    assert sink.observations[2].final is True
    assert sink.observations[2].final_status == "succeeded"
    assert sink.observations[2].reconciles_event_id == sink.observations[1].event_id

    # Post-final emissions must be ignored
    await pub.emit_live(tokens=tokens2, context=ctx1)
    await pub.emit_final(tokens=tokens2, status="failed")
    assert len(sink.observations) == 3


@pytest.mark.asyncio
async def test_publisher_handles_observer_hang_or_failure() -> None:
    failing_sink = FakeUsageObserver(fail=True)
    binding = _make_subscription_binding(uuid4(), uuid4(), uuid4())
    pub_fail = _ObservationPublisher(binding=binding, sink=failing_sink)
    tokens = TokenCounts(input_tokens=10, output_tokens=5)
    await pub_fail.emit_live(tokens=tokens)
    await pub_fail.emit_final(tokens=tokens, status="succeeded")
    assert pub_fail.delivered_final is True
    assert pub_fail.delivery_incomplete is True

    hanging_sink = FakeUsageObserver(hang=True)
    pub_hang = _ObservationPublisher(binding=binding, sink=hanging_sink)
    await pub_hang.emit_live(tokens=tokens)
    await asyncio.wait_for(pub_hang.emit_final(tokens=tokens, status="succeeded"), 2)
    assert pub_hang.delivery_incomplete is True
    assert pub_hang._worker is None


@pytest.mark.asyncio
async def test_google_publisher_cancelled_final_delivery_is_isolated() -> None:
    started = asyncio.Event()

    class SlowSink:
        async def observe_usage(self, observation: Observation) -> None:
            started.set()
            await asyncio.sleep(10)

    binding = _make_subscription_binding(uuid4(), uuid4(), uuid4())
    publisher = _ObservationPublisher(binding, SlowSink())
    task = asyncio.create_task(publisher.emit_final(
        tokens=TokenCounts(input_tokens=7, output_tokens=2), status="succeeded"
    ))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.wait_for(task, 1)
    assert publisher.delivery_incomplete
    assert publisher._worker is None


# --- Gemini Subscription execute_observed Tests ---

@pytest.mark.asyncio
async def test_gemini_execute_observed_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id, attempt_id, task_id = uuid4(), uuid4(), uuid4()
    req = AsyncMock(spec=SubscriptionInvocationRequest)
    req.attempt = AttemptIdentity(run_id=run_id, task_id=task_id, attempt_id=attempt_id)
    route_spec = RouteSpec(
        provider="google",
        client="gemini_cli",
        model="gemini-3.8-flash-medium",
        effort=ReasoningEffort.MEDIUM,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    req.task = AsyncMock()
    req.task.route = RouteBinding(requested=route_spec, effective=route_spec)
    req.task.purpose = SpecialistPurpose.ROUTINE_IMPLEMENTATION

    telemetry = AttemptTelemetry(
        duration_ms=150,
        input_tokens=42,
        output_tokens=18,
        cached_input_tokens=10,
    )
    proof = SubscriptionLaunchTerminalProof(
        launch_id="launch-1",
        pid=1234,
        process_identity="gemini-proc",
        outcome="completed",
        return_code=0,
        stop_confirmed=True,
        stdout_bytes=100,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    mock_result = SubscriptionInvocationResult(
        attempt=req.attempt,
        decision=TaskHandoff(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            status=HandoffStatus.COMPLETED,
            summary="Gemini finished",
            candidate_tree_digest="0" * 64,
            evidence_receipt_ids=("receipt-1",),
        ),
        telemetry=telemetry,
        failure=None,
        launch_proof=proof,
    )

    async def fake_execute(self: object, request: object) -> SubscriptionInvocationResult:
        return mock_result

    monkeypatch.setattr(GeminiGateway, "execute", fake_execute)

    gw = GeminiGateway(
        GeminiInstallation(
            executable=str(tmp_path / "gemini.exe"),
            cwd=str(tmp_path),
            home=str(tmp_path),
            model="gemini-3.8-flash-medium",
            account="test",
            executable_digest="a" * 64,
            effort="medium",
        ),
        verifier=AsyncMock(),
    )
    binding = _make_subscription_binding(run_id, attempt_id, task_id)
    sink = FakeUsageObserver()

    res = await gw.execute_observed(req, usage_binding=binding, usage_sink=sink)
    assert res is mock_result
    # Gemini ACP is final-only: emits 1 observation
    assert len(sink.observations) == 1
    obs = sink.observations[0]
    assert obs.final is True
    assert obs.final_status == "succeeded"
    assert obs.sequence == 1
    assert obs.reconciles_event_id is None
    assert obs.tokens.input_tokens == 42
    assert obs.tokens.output_tokens == 18
    assert obs.tokens.cached_input_tokens == 10
    assert obs.unknown_dimensions == (
        "reasoning_output_tokens", "context_occupied_tokens",
        "context_capacity_tokens", "compaction_count",
    )

    failing = FakeUsageObserver(fail=True)
    degraded = await gw.execute_observed(req, usage_binding=binding, usage_sink=failing)
    assert degraded.failure is None
    assert "usage observation delivery incomplete" in degraded.telemetry.unknown_telemetry_reasons


@pytest.mark.parametrize("failure", [SubscriptionFailure.INTERRUPTED, SubscriptionFailure.UNCERTAIN])
@pytest.mark.asyncio
async def test_gemini_execute_observed_interrupted_emits_final(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: SubscriptionFailure
) -> None:
    run_id, attempt_id, task_id = uuid4(), uuid4(), uuid4()
    req = AsyncMock(spec=SubscriptionInvocationRequest)
    req.attempt = AttemptIdentity(run_id=run_id, task_id=task_id, attempt_id=attempt_id)
    route_spec = RouteSpec(
        provider="google",
        client="gemini_cli",
        model="gemini-3.8-flash-medium",
        effort=ReasoningEffort.MEDIUM,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    req.task = AsyncMock()
    req.task.route = RouteBinding(requested=route_spec, effective=route_spec)
    req.task.purpose = SpecialistPurpose.ROUTINE_IMPLEMENTATION

    async def fake_execute(self: object, request: object) -> None:
        raise SubscriptionInterrupted(
            SubscriptionInvocationResult(
                attempt=req.attempt,
                failure=failure,
                telemetry=AttemptTelemetry(duration_ms=100),
            )
        )

    monkeypatch.setattr(GeminiGateway, "execute", fake_execute)

    gw = GeminiGateway(
        GeminiInstallation(
            executable=str(tmp_path / "gemini.exe"),
            cwd=str(tmp_path),
            home=str(tmp_path),
            model="gemini-3.8-flash-medium",
            account="test",
            executable_digest="a" * 64,
            effort="medium",
        ),
        verifier=AsyncMock(),
    )
    binding = _make_subscription_binding(run_id, attempt_id, task_id)
    sink = FakeUsageObserver()

    with pytest.raises(SubscriptionInterrupted):
        await gw.execute_observed(req, usage_binding=binding, usage_sink=sink)

    assert len(sink.observations) == 1
    obs = sink.observations[0]
    assert obs.final is True
    assert obs.final_status == failure.value


# --- Antigravity Subscription execute_observed Tests ---

@pytest.mark.asyncio
async def test_antigravity_execute_observed_live_and_final(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    run_id, attempt_id, task_id = uuid4(), uuid4(), uuid4()
    req = AsyncMock(spec=SubscriptionInvocationRequest)
    req.attempt = AttemptIdentity(run_id=run_id, task_id=task_id, attempt_id=attempt_id)
    route_spec = RouteSpec(
        provider="google",
        client="antigravity_cli",
        model="gemini-3.8-flash-medium",
        effort=ReasoningEffort.MEDIUM,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    req.task = AsyncMock()
    req.task.route = RouteBinding(requested=route_spec, effective=route_spec)
    req.task.purpose = SpecialistPurpose.ROUTINE_IMPLEMENTATION

    proof = SubscriptionLaunchTerminalProof(
        launch_id="launch-agy",
        pid=1235,
        process_identity="agy-proc",
        outcome="completed",
        return_code=0,
        stop_confirmed=True,
        stdout_bytes=100,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
    )

    # Simulate execution where execute passes publisher to session exchange
    async def fake_execute_internal(self: object, request: object, *, publisher: _ObservationPublisher | None = None) -> SubscriptionInvocationResult:
        if publisher is not None:
            # Emit live step update
            await publisher.emit_live(
                tokens=TokenCounts(input_tokens=100, output_tokens=20, cached_input_tokens=50),
                context=ContextWindow(occupied_tokens=100, capacity_tokens=32000),
            )
            # Duplicate step update (should be suppressed by publisher)
            await publisher.emit_live(
                tokens=TokenCounts(input_tokens=100, output_tokens=20, cached_input_tokens=50),
                context=ContextWindow(occupied_tokens=100, capacity_tokens=32000),
            )
            # Second distinct live step update
            await publisher.emit_live(
                tokens=TokenCounts(input_tokens=150, output_tokens=40, cached_input_tokens=50),
                context=ContextWindow(occupied_tokens=150, capacity_tokens=32000),
            )
        return SubscriptionInvocationResult(
            attempt=req.attempt,
            decision=TaskHandoff(
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                status=HandoffStatus.COMPLETED,
                summary="Antigravity done",
                candidate_tree_digest="0" * 64,
                evidence_receipt_ids=("receipt-agy",),
            ),
            telemetry=AttemptTelemetry(
                duration_ms=200,
                input_tokens=150,
                output_tokens=40,
                cached_input_tokens=50,
            ),
            failure=None,
            launch_proof=proof,
        )

    monkeypatch.setattr(AntigravityGateway, "_execute_internal", fake_execute_internal)

    gw = AntigravityGateway(
        AntigravityInstallation(
            executable=str(tmp_path / "agy.exe"),
            cwd=str(tmp_path),
            home=str(tmp_path),
            model="gemini-3.8-flash-medium",
            effort="medium",
            executable_digest="b" * 64,
        ),
        broker=AsyncMock(),
        lifecycle=MockLifecycle(),
    )
    binding = _make_subscription_binding(run_id, attempt_id, task_id)
    sink = FakeUsageObserver()

    res = await gw.execute_observed(req, usage_binding=binding, usage_sink=sink)
    assert res.failure is None
    # 2 live observations (duplicate suppressed) + 1 final observation = 3
    assert len(sink.observations) == 3
    live1, live2, final_obs = sink.observations

    assert live1.final is False
    assert live1.sequence == 1
    assert live1.tokens.input_tokens == 100
    assert live1.context is not None
    assert live1.context.occupied_tokens == 100

    assert live2.final is False
    assert live2.sequence == 2
    assert live2.tokens.input_tokens == 150

    assert final_obs.final is True
    assert final_obs.sequence == 3
    assert final_obs.final_status == "succeeded"
    assert final_obs.reconciles_event_id == live2.event_id
    assert final_obs.tokens.input_tokens == 150
    assert final_obs.tokens.output_tokens == 40
    assert project_source(tuple(sink.observations)).context_occupied_tokens == 150


# --- Authoring EpicBrainstormGateway execute_observed Tests ---

@pytest.mark.asyncio
async def test_epic_brainstorm_execute_observed_gemini(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    job_id, project_id, attempt_id = uuid4(), uuid4(), uuid4()
    job = _make_authoring_job(job_id, project_id)
    binding = _make_authoring_binding(job_id, project_id, attempt_id, model="gemini-flash")
    sink = FakeUsageObserver()

    inst = GeminiInstallationSpec(
        client="gemini_cli",
        account="dev-account",
        model="gemini-flash",
        effort="low",
        executable=str(tmp_path / "gemini.exe"),
        executable_digest="a" * 64,
        client_version="1.0.0",
        cwd=str(tmp_path),
        home=str(tmp_path),
        quota=InstallationQuota(account="dev-account", pool="default-pool"),
    )
    gw = EpicBrainstormGateway(installation=inst)

    async def fake_official(
        self: object,
        job: object,
        turns: object,
        reader: object,
        *,
        cancelled: object,
        lifecycle: object,
        publisher: _ObservationPublisher | None = None,
        attempt_id_override: UUID | None = None,
    ) -> BrainstormGatewayResult:
        if publisher is not None:
            await publisher.emit_final(
                tokens=TokenCounts(input_tokens=75, output_tokens=25),
                status="succeeded",
            )
        return BrainstormGatewayResult(
            proposal=BrainstormProposal(turn_id=job.prompt_turn_id, problem="test"),  # type: ignore[attr-defined]
            telemetry=AttemptTelemetry(duration_ms=100, input_tokens=75, output_tokens=25),
            failure=None,
        )

    monkeypatch.setattr(EpicBrainstormGateway, "_execute_official", fake_official)

    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))
    lifecycle = MockLifecycle(attempt_id=attempt_id)

    res = await gw.execute_observed(
        job,
        (),
        reader,
        cancelled=AsyncMock(return_value=False),
        lifecycle=lifecycle,
        usage_binding=binding,
        usage_sink=sink,
    )
    assert res.failure is None
    assert len(sink.observations) == 1
    obs = sink.observations[0]
    assert obs.final is True
    assert obs.final_status == "succeeded"
    assert obs.tokens.input_tokens == 75
    assert obs.tokens.output_tokens == 25


@pytest.mark.asyncio
async def test_epic_brainstorm_execute_observed_budget_exhausted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    job_id, project_id, attempt_id = uuid4(), uuid4(), uuid4()
    # Hard cap of 50 input tokens
    job = _make_authoring_job(job_id, project_id, max_input=50)
    binding = _make_authoring_binding(job_id, project_id, attempt_id, model="gemini-flash")
    sink = FakeUsageObserver()

    inst = GeminiInstallationSpec(
        client="gemini_cli",
        account="dev-account",
        model="gemini-flash",
        effort="low",
        executable=str(tmp_path / "gemini.exe"),
        executable_digest="a" * 64,
        client_version="1.0.0",
        cwd=str(tmp_path),
        home=str(tmp_path),
        quota=InstallationQuota(account="dev-account", pool="default-pool"),
    )
    gw = EpicBrainstormGateway(installation=inst)

    reader = BrainstormReadOnlyTools(RepositoryReader(root=str(tmp_path), secret_paths=()))
    lifecycle = MockLifecycle(attempt_id=attempt_id)

    async def fake_official_inner(
        self: object,
        job: object,
        turns: object,
        reader: object,
        *,
        cancelled: object,
        lifecycle: object,
        publisher: _ObservationPublisher | None = None,
        attempt_id_override: UUID | None = None,
    ) -> BrainstormGatewayResult:
        tokens = TokenCounts(input_tokens=100, output_tokens=25)
        if publisher is not None:
            await publisher.emit_final(tokens=tokens, status="budget_exhausted")
        return BrainstormGatewayResult(
            proposal=None,
            telemetry=AttemptTelemetry(duration_ms=100, input_tokens=100, output_tokens=25),
            failure="budget_exhausted",
        )

    monkeypatch.setattr(EpicBrainstormGateway, "_execute_official", fake_official_inner)

    res = await gw.execute_observed(
        job,
        (),
        reader,
        cancelled=AsyncMock(return_value=False),
        lifecycle=lifecycle,
        usage_binding=binding,
        usage_sink=sink,
    )
    assert res.failure == "budget_exhausted"
    assert res.proposal is None
    assert len(sink.observations) == 1
    assert sink.observations[0].final_status == "budget_exhausted"
    assert sink.observations[0].tokens.input_tokens == 100
