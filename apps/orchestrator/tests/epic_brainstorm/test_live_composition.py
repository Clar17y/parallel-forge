"""Live composition test for normal composed API and separate worker running durable brainstorming."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from forge.agents.client_process import ClientProcessSession
from forge.agents.codex_gateway import CodexGateway, codex_account_identity
from forge.api.app import create_app
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brainstorm import EpicBriefBrainstormAdapter
from forge.domain.epic_brainstorm import (
    BrainstormProposal,
)
from forge.domain.epic_brief import BriefContent, BriefRequirement
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    OperatorProfile,
    ReasoningEffort,
    RolePreference,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
)
from forge.persistence.models.epic_brainstorm import (
    BrainstormAttemptRow,
    BrainstormJobRow,
)
from forge.persistence.models.project import Project
from forge.persistence.models.subscription import ProjectSubscriptionProfile
from forge.persistence.repositories.epic_brief import PostgresEpicBriefRepository
from forge.persistence.repositories.subscription import PostgresSubscriptionRepository
from forge.settings import Settings
from forge.tools.epic_brainstorm import BrainstormReadOnlyTools
from forge.worker.epic_brainstorm_composition import (
    make_brainstorm_reader_factory,
)
from forge.worker.main import run_worker
from sqlalchemy import select


def test_reader_factory_loads_policy_asynchronously(session_factory) -> None:
    factory = make_brainstorm_reader_factory(session_factory)
    pending = factory(SimpleNamespace(project_id=uuid4()))
    assert inspect.isawaitable(pending)
    pending.close()


@pytest.fixture
def migrated_database_url(test_database_url, alembic_config_factory) -> Iterator[str]:
    # The data-protecting migration intentionally refuses downgrade with records.
    command.upgrade(alembic_config_factory(test_database_url), "head")
    yield test_database_url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest_stale,long_lived", ((False, False), (True, False), (False, True))
)
async def test_live_composition_claims_queued_job_and_adopts_revision(
    session_factory,
    test_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_stale: bool,
    long_lived: bool,
) -> None:
    """A queued brainstorm is claimed by normally composed worker, persisted, and adopted into #69 brief."""
    project_id = uuid4()
    epic_id = uuid4()
    requirement_id = uuid4()

    # 1. Seed project with canonical path and initial brief draft (#69)
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")

    initial_draft = BriefContent(
        problem="Initial Problem Statement",
        requirements=[
            BriefRequirement(
                requirement_id=requirement_id,
                text="Initial requirement",
                acceptance_criteria=["Criterion 1"],
            )
        ],
        open_questions=["Should we proceed?"],
    )

    route = RouteSpec(
        provider="openai",
        client="codex_app_server",
        model="gpt-5.6-luna",
        effort=ReasoningEffort.MEDIUM,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    profile = OperatorProfile(
        profile_id=uuid4(),
        version=1,
        preferences=(RolePreference(purpose=SpecialistPurpose.EXPLORATION, preferred_route=route),),
    )
    async with session_factory() as session, session.begin():
        session.add(
            Project(
                id=project_id,
                canonical_path=str(project_dir),
                github_repository="test/repo",
                default_branch="main",
            )
        )
        await session.flush()
        brief_repo = PostgresEpicBriefRepository(session)
        await brief_repo.create(
            epic_id=epic_id,
            project_id=project_id,
            title="Discovery Epic",
            draft=initial_draft,
        )
        await PostgresSubscriptionRepository(session).store_profile(profile)
        session.add(
            ProjectSubscriptionProfile(
                project_id=project_id, profile_id=profile.profile_id, profile_version=1
            )
        )

    # 2. Configure scripted provider transport / subprocess
    script_code = (
        "import json,sys\n"
        "def recv(method):\n f=json.loads(sys.stdin.readline()); assert f['method']==method, f; return f\n"
        "def send(f): print(json.dumps(f),flush=True)\n"
        "f=recv('initialize'); send({'id':f['id'],'result':{}})\n"
        "recv('initialized')\n"
        "f=recv('account/read'); send({'id':f['id'],'result':{'account':{'type':'chatgpt','email':'codex@example.invalid'}}})\n"
        "f=recv('model/list'); send({'id':f['id'],'result':{'data':[{'id':'gpt-5.6-luna','supportedReasoningEfforts':[{'reasoningEffort':'medium'}]}]}})\n"
        "f=recv('config/read'); send({'id':f['id'],'result':{'config':{}}})\n"
        "f=recv('thread/start'); assert f['params']['model']=='gpt-5.6-luna'; send({'id':f['id'],'result':{'thread':{'id':'thread-1'},'model':'gpt-5.6-luna'}})\n"
        "f=recv('turn/start'); p=f['params']; assert p['effort']=='medium'; context=json.loads(p['input'][0]['text'].split('\\n')[-1]); send({'id':f['id'],'result':{'turn':{'id':'turn-1'}}})\n"
        "proposal={'proposal':{'turn_id':context['prompt_turn_id'],'problem':'Scripted brainstorm refinement','requirements':['Refined requirement'],'requirement_criteria':{'Refined requirement':['Refined check']}}}\n"
        "send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread-1','turnId':'turn-1','tokenUsage':{'total':{'inputTokens':15,'outputTokens':25,'cachedInputTokens':0}}}})\n"
        "send({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-1','item':{'id':'item-1','type':'agentMessage','text':json.dumps(proposal)}}})\n"
        "send({'method':'turn/completed','params':{'threadId':'thread-1','turn':{'id':'turn-1','status':'completed'}}})\n"
        + ("import time; time.sleep(30)\n" if long_lived else "")
    )
    script_path = tmp_path / "provider_client.py"
    script_path.write_text(script_code, encoding="utf-8")

    monkeypatch.setattr(CodexGateway, "_command", lambda self: ("-u", str(script_path)))
    executable = Path(sys.executable)
    manifest = tmp_path / "installations.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "installations": [
                    {
                        "client": "codex_app_server",
                        "account": codex_account_identity("codex@example.invalid"),
                        "model": route.model,
                        "effort": "medium",
                        "executable": str(executable),
                        "executable_digest": (
                            "0" * 64
                            if manifest_stale
                            else hashlib.sha256(executable.read_bytes()).hexdigest()
                        ),
                        "client_version": "0.153.4",
                        "cwd": str(project_dir),
                        "home": str(tmp_path),
                        "quota": {"account": "dev-account", "pool": "default-pool"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        database_url=test_database_url,
        data_root=tmp_path / "forge-data",
        subscription_installations_path=manifest,
    )

    # 3. Create conversation and submit brainstorm turn to queue the job
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    app = create_app(settings, session_factory=session_factory)
    service = app.state.epic_brainstorm_service
    assert service is not None

    conversation_id, version = await service.create(
        epic_id=epic_id,
        project_id=project_id,
        actor=actor,
        key="create-conv",
        text="Brainstorm topic",
    )
    turns = await service.turns(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id
    )
    prompt_turn_id = turns[0].turn_id

    receipt = await service.submit(
        epic_id=epic_id,
        project_id=project_id,
        conversation_id=conversation_id,
        prompt_turn_id=prompt_turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key="submit-job",
    )
    assert receipt.job_id is not None

    async with session_factory() as session:
        job = await session.get(BrainstormJobRow, receipt.job_id)
        assert job is not None
        assert job.state == "queued"

    # 4. Start the normal separate worker composition; no injected gateway or reader.
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_worker(
            settings.model_copy(update={"process_role": "worker"}),
            stop_event=stop,
            poll_interval=0.05,
        )
    )
    try:
        for _ in range(100):
            async with session_factory() as session:
                state = await session.get(BrainstormJobRow, receipt.job_id)
                if state is not None and state.state in {"proposed", "failed", "quota_wait"}:
                    break
            await asyncio.sleep(0.1)
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)

    # 6. Verify proposal was persisted and attempt settled
    async with session_factory() as session:
        job = await session.get(BrainstormJobRow, receipt.job_id)
        assert job is not None
        attempt = (
            await session.scalars(
                select(BrainstormAttemptRow).where(BrainstormAttemptRow.job_id == receipt.job_id)
            )
        ).first()
        assert job.state == "proposed", (
            f"job failed with {job.failure}, attempt={attempt.failure if attempt else None}, attempt_state={attempt.state if attempt else None}"
        )
        assert job.proposal is not None
        assert job.proposal["problem"] == "Scripted brainstorm refinement"

        assert attempt is not None
        assert attempt.process_started
        assert attempt.process_settled
        assert attempt.terminal_proof is not None
        assert attempt.terminal_proof["outcome"] in ("exited", "completed")
        outcome = await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id
        )
        assert outcome.diagnostics is not None
        assert outcome.diagnostics.attempt_id == attempt.id
        kinds = [event.kind for event in outcome.diagnostics.events]
        assert kinds[0] == "launch"
        assert "provider_wait" in kinds
        assert "proposal_received" in kinds
        assert "settlement_started" in kinds
        assert "process_settled" in kinds
        assert kinds.index("proposal_received") < kinds.index("attempt_stopped")
        assert outcome.diagnostics.dropped_events == 0
        if long_lived:
            assert attempt.terminal_proof["outcome"] == "completed"

    # 7. Human adopts proposal into actual #69 brief revision
    proposal = BrainstormProposal.model_validate(job.proposal)
    async with session_factory() as session, session.begin():
        adapter = EpicBriefBrainstormAdapter(session)
        revision_id = await adapter.save_proposal_revision(
            epic_id=epic_id,
            expected_version=1,
            source_job_id=receipt.job_id,
            proposal=proposal,
        )
        brief_repo = PostgresEpicBriefRepository(session)
        revision = await brief_repo.get_revision(epic_id, revision_id)
        assert revision.source_job_id == receipt.job_id
        assert revision.content.requirements[1].text == "Refined requirement"
        assert revision.content.requirements[1].acceptance_criteria == ["Refined check"]

        # Human operator adopts revision
        await brief_repo.adopt_revision(epic_id, version=2, revision=revision)
        epic = await brief_repo.get(epic_id)
        assert epic.accepted_brief_revision_id == revision_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ("stuck_setup", "provider_wait", "stuck_tool", "tool_reply_stall", "cancel_late", "owner_retry")
)
async def test_live_composition_faults_preserve_durable_diagnosis_and_settlement(
    session_factory, test_database_url: str, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    """Real worker, PostgreSQL and supervised peer retain a truthful fault boundary."""
    project_id, epic_id, requirement_id = uuid4(), uuid4(), uuid4()
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("Safe read fixture", encoding="utf-8")
    route = RouteSpec(
        provider="openai", client="codex_app_server", model="gpt-5.6-luna",
        effort=ReasoningEffort.MEDIUM, auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    profile = OperatorProfile(
        profile_id=uuid4(), version=1,
        preferences=(RolePreference(purpose=SpecialistPurpose.EXPLORATION, preferred_route=route),),
    )
    async with session_factory() as session, session.begin():
        session.add(Project(
            id=project_id, canonical_path=str(project_dir),
            github_repository="test/repo", default_branch="main",
        ))
        await session.flush()
        await PostgresEpicBriefRepository(session).create(
            epic_id=epic_id, project_id=project_id, title="Discovery Epic",
            draft=BriefContent(
                problem="Initial problem",
                requirements=[BriefRequirement(
                    requirement_id=requirement_id, text="Initial requirement",
                    acceptance_criteria=["Initial check"],
                )],
                open_questions=["What next?"],
            ),
        )
        await PostgresSubscriptionRepository(session).store_profile(profile)
        session.add(ProjectSubscriptionProfile(
            project_id=project_id, profile_id=profile.profile_id, profile_version=1,
        ))

    retry_marker = tmp_path / "first-attempt"
    script = tmp_path / "fault_peer.py"
    script.write_text(
        "import json,sys,time,pathlib\n"
        f"fault={fault!r}; marker=pathlib.Path({str(retry_marker)!r})\n"
        "def recv(method):\n f=json.loads(sys.stdin.readline()); assert f['method']==method, f; return f\n"
        "def send(f): print(json.dumps(f),flush=True)\n"
        "f=recv('initialize'); send({'id':f['id'],'result':{}})\n"
        "recv('initialized')\n"
        "f=recv('account/read'); send({'id':f['id'],'result':{'account':{'type':'chatgpt','email':'codex@example.invalid'}}})\n"
        "f=recv('model/list'); send({'id':f['id'],'result':{'data':[{'id':'gpt-5.6-luna','supportedReasoningEfforts':[{'reasoningEffort':'medium'}]}]}})\n"
        "f=recv('config/read')\n"
        "if fault=='stuck_setup': time.sleep(30)\n"
        "send({'id':f['id'],'result':{'config':{}}})\n"
        "f=recv('thread/start'); send({'id':f['id'],'result':{'thread':{'id':'thread-1'},'model':'gpt-5.6-luna'}})\n"
        "f=recv('turn/start'); context=json.loads(f['params']['input'][0]['text'].split('\\n')[-1]); send({'id':f['id'],'result':{'turn':{'id':'turn-1'}}})\n"
        "if fault=='provider_wait': time.sleep(30)\n"
        "if fault=='owner_retry' and not marker.exists(): marker.write_text('first'); sys.exit(7)\n"
        "if fault in {'stuck_tool','tool_reply_stall'}:\n"
        " send({'id':80,'method':'item/tool/call','params':{'callId':'read-1','threadId':'thread-1','turnId':'turn-1','namespace':'forge','tool':'forge_read_file','arguments':{'path':'README.md'}}}); recv('tool-result-never-arrives')\n"
        "proposal={'proposal':{'turn_id':context['prompt_turn_id'],'problem':'Scripted retry success','requirements':['Refined requirement'],'requirement_criteria':{'Refined requirement':['Refined check']}}}\n"
        "send({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn-1','item':{'id':'item-1','type':'agentMessage','text':json.dumps(proposal)}}})\n"
        "if fault=='cancel_late': time.sleep(30)\n"
        "send({'method':'turn/completed','params':{'threadId':'thread-1','turn':{'id':'turn-1','status':'completed'}}})\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(CodexGateway, "_command", lambda self: ("-u", str(script)))
    if fault == "stuck_tool":
        async def stuck_read(_self, _path):
            await asyncio.Event().wait()

        monkeypatch.setattr(BrainstormReadOnlyTools, "read_file", stuck_read)
    if fault == "tool_reply_stall":
        original_send = ClientProcessSession.send

        async def blocked_reply(self, frame):
            if frame.get("id") == 80 and "result" in frame:
                await asyncio.Event().wait()
            return await original_send(self, frame)

        monkeypatch.setattr(ClientProcessSession, "send", blocked_reply)

    executable = Path(sys.executable)
    manifest = tmp_path / "installations.json"
    manifest.write_text(json.dumps({
        "version": 2,
        "installations": [{
            "client": "codex_app_server", "account": codex_account_identity("codex@example.invalid"),
            "model": route.model, "effort": "medium", "executable": str(executable),
            "executable_digest": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "client_version": "0.153.4", "cwd": str(project_dir), "home": str(tmp_path),
            "quota": {"account": "dev-account", "pool": "default-pool"},
        }],
    }), encoding="utf-8")
    settings = Settings(
        database_url=test_database_url, data_root=tmp_path / "forge-data",
        subscription_installations_path=manifest,
        epic_brainstorm_budget=TaskBudget(max_duration_seconds=2, max_provider_attempts=2),
    )
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = create_app(settings, session_factory=session_factory).state.epic_brainstorm_service
    assert service is not None
    conversation_id, version = await service.create(
        epic_id=epic_id, project_id=project_id, actor=actor,
        key="create-conv", text="Brainstorm topic",
    )
    prompt_turn_id = (await service.turns(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
    ))[0].turn_id
    receipt = await service.submit(
        epic_id=epic_id, project_id=project_id, conversation_id=conversation_id,
        prompt_turn_id=prompt_turn_id, expected_epic_version=1,
        expected_conversation_version=version, actor=actor, key="submit-job",
    )
    assert receipt.job_id is not None
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(
        settings.model_copy(update={"process_role": "worker"}),
        stop_event=stop, poll_interval=0.05,
    ))
    try:
        for _ in range(150):
            async with session_factory() as session:
                job = await session.get(BrainstormJobRow, receipt.job_id)
                assert job is not None
                attempt = await session.get(BrainstormAttemptRow, job.current_attempt_id) if job.current_attempt_id else None
                events = (attempt.diagnostics or {}).get("events", []) if attempt else []
                if fault == "cancel_late" and any(e["kind"] == "proposal_received" for e in events):
                    await service.cancel(
                        epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
                        expected_job_version=job.version, actor=actor, key="cancel-job",
                    )
                    break
                if fault != "cancel_late" and job.state == "failed":
                    break
            await asyncio.sleep(0.1)
        else:
            pytest.fail(f"{fault}: worker did not reach first boundary")
        if fault == "owner_retry":
            async with session_factory() as session:
                job = await session.get(BrainstormJobRow, receipt.job_id)
                assert job is not None and job.state == "failed"
                first_attempt_id = job.current_attempt_id
                assert first_attempt_id is not None
                await service.retry(
                    epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
                    expected_job_version=job.version, actor=actor, key="retry-job",
                    owner_override=True,
                )
        for _ in range(150):
            async with session_factory() as session:
                job = await session.get(BrainstormJobRow, receipt.job_id)
                assert job is not None
                if (
                    job.state in ({"proposed", "failed"} if fault == "owner_retry" else {"cancelled", "failed"})
                    and (fault != "owner_retry" or job.current_attempt_id != first_attempt_id)
                ):
                    break
            await asyncio.sleep(0.1)
        else:
            pytest.fail(f"{fault}: worker did not reach terminal boundary")
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)

    async with session_factory() as session:
        job = await session.get(BrainstormJobRow, receipt.job_id)
        assert job is not None
        attempts = (await session.scalars(
            select(BrainstormAttemptRow).where(BrainstormAttemptRow.job_id == receipt.job_id)
            .order_by(BrainstormAttemptRow.created_at)
        )).all()
        assert len(attempts) == (2 if fault == "owner_retry" else 1)
        assert all(a.process_started and a.process_settled and a.terminal_proof for a in attempts)
        assert all(any(e["kind"] == "process_settled" for e in a.diagnostics["events"]) for a in attempts)
        assert all(any(e["kind"] == "attempt_stopped" for e in a.diagnostics["events"]) for a in attempts)
        if fault == "owner_retry":
            assert attempts[0].id != attempts[1].id
            assert attempts[0].diagnostics != attempts[1].diagnostics
            assert job.state == "proposed" and job.proposal is not None
            assert job.proposal["problem"] == "Scripted retry success"
        else:
            assert job.state == ("cancelled" if fault == "cancel_late" else "failed")
            assert job.failure == ("cancelled" if fault == "cancel_late" else "timeout")
            assert job.proposal is None
            wait = (
                "config_read" if fault == "stuck_setup" else
                "repository_read" if fault == "stuck_tool" else
                "tool_reply" if fault == "tool_reply_stall" else "provider_receive"
            )
            assert any(
                e["kind"] == ("tool_started" if fault == "stuck_tool" else "provider_wait")
                and e.get("operation") == wait for e in attempts[0].diagnostics["events"]
            )
            if fault == "cancel_late":
                assert any(e["kind"] == "proposal_received" for e in attempts[0].diagnostics["events"])
        outcome = await service.observe(
            epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
        )
        assert outcome.diagnostics is not None
        assert outcome.diagnostics.attempt_id == attempts[-1].id
