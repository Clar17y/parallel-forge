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
@pytest.mark.parametrize("manifest_stale", (False, True))
async def test_live_composition_claims_queued_job_and_adopts_revision(
    session_factory,
    test_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_stale: bool,
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
