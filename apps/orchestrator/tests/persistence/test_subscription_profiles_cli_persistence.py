"""Real PostgreSQL composition checks for independently invoked profile CLI commands."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from forge.application.services.subscription_profiles import SubscriptionProfileService
from forge.domain.subscription import ExecutionEnvelope, RouteBinding, SpecialistPurpose
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import text


@pytest.mark.integration
def test_cli_independent_invocations_replay_and_conflict(
    session_factory, persisted_run, tmp_path: Path
) -> None:
    payload = {
        "preferences": [{
            "purpose": "primary",
            "preferred_route": {"provider": "openai", "client": "codex", "model": "cli-one"},
        }]
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    database_url = session_factory.kw["bind"].url.render_as_string(hide_password=False)

    def new_service() -> tuple[SubscriptionProfileService, object]:
        engine = create_engine(database_url)
        independent_factory = create_session_factory(engine)
        return SubscriptionProfileService(lambda: PostgresUnitOfWork(independent_factory)), engine

    args = ["profile", "create", "--file", str(path), "--idempotency-key", "cli-pg-replay"]

    def invoke(command: list[str]) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["FORGE_DATABASE_URL"] = database_url
        environment["PYTHONPATH"] = str(Path.cwd() / "apps" / "orchestrator" / "src")
        return subprocess.run(
            [sys.executable, "-m", "forge.cli.main", *command],
            capture_output=True,
            text=True,
            cwd=Path.cwd(),
            env=environment,
            check=False,
        )

    first = invoke(args)
    replay = invoke(args)
    payload["preferences"][0]["preferred_route"]["model"] = "cli-two"
    path.write_text(json.dumps(payload), encoding="utf-8")
    conflict = invoke(args)

    assert first.returncode == 0, f"{first.stdout} {first.stderr}"
    assert replay.returncode == 0, replay.stdout
    assert json.loads(first.stdout) == json.loads(replay.stdout)
    assert conflict.returncode != 0
    assert "cli-two" not in conflict.stdout
    profile_id = UUID(json.loads(first.stdout)["profile_id"])

    async def prepare_frozen() -> object:
        service, engine = new_service()
        profile = await service.get(profile_id, 1)
        async with service._unit_of_work_factory() as work:
            repository = work.subscription
            route = profile.preferences[0].preferred_route
            envelope = ExecutionEnvelope(
                run_id=persisted_run.id,
                profile_id=profile.profile_id,
                profile_version=profile.version,
                safety_policy_version=1,
                routes=((SpecialistPurpose.PRIMARY, RouteBinding(requested=route, effective=route)),),
            )
            await repository.freeze_envelope(envelope)
            await work.commit()
        await engine.dispose()
        return profile

    import asyncio
    asyncio.run(prepare_frozen())
    append_path = tmp_path / "profile-v2.json"
    append_path.write_text(json.dumps({
        "expected_current_version": 1,
        "preferences": [{
            "purpose": "primary",
            "preferred_route": {"provider": "openai", "client": "codex", "model": "cli-two"},
        }],
    }), encoding="utf-8")
    appended = invoke([
        "profile", "append", "--profile-id", str(profile_id), "--file", str(append_path),
        "--idempotency-key", "cli-pg-append",
    ])
    assert appended.returncode == 0, appended.stdout
    appended_replay = invoke([
        "profile", "append", "--profile-id", str(profile_id), "--file", str(append_path),
        "--idempotency-key", "cli-pg-append",
    ])
    assert appended_replay.returncode == 0, appended_replay.stdout
    assert json.loads(appended.stdout) == json.loads(appended_replay.stdout)
    append_path.write_text(append_path.read_text(encoding="utf-8").replace("cli-two", "cli-three"), encoding="utf-8")
    append_conflict = invoke([
        "profile", "append", "--profile-id", str(profile_id), "--file", str(append_path),
        "--idempotency-key", "cli-pg-append",
    ])
    assert append_conflict.returncode != 0
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps({
        "profile_id": str(profile_id), "profile_version": 2,
    }), encoding="utf-8")
    selected = invoke([
        "profile", "project-select", "--project-id", str(persisted_run.project_id),
        "--file", str(selection_path), "--idempotency-key", "cli-pg-select",
    ])
    assert selected.returncode == 0, selected.stdout
    selected_replay = invoke([
        "profile", "project-select", "--project-id", str(persisted_run.project_id),
        "--file", str(selection_path), "--idempotency-key", "cli-pg-select",
    ])
    assert selected_replay.returncode == 0, selected_replay.stdout
    selection_path.write_text(json.dumps({
        "profile_id": str(profile_id), "profile_version": 1,
    }), encoding="utf-8")
    select_conflict = invoke([
        "profile", "project-select", "--project-id", str(persisted_run.project_id),
        "--file", str(selection_path), "--idempotency-key", "cli-pg-select",
    ])
    assert select_conflict.returncode != 0
    async def verify_frozen() -> None:
        service, engine = new_service()
        async with service._unit_of_work_factory() as work:
            frozen = await work.subscription.envelope_for_run(persisted_run.id)
            assert frozen is not None
            assert frozen.profile_version == 1
        await engine.dispose()
    asyncio.run(verify_frozen())

    async def cleanup() -> None:
        engine = create_engine(database_url)
        cleanup_factory = create_session_factory(engine)
        async with cleanup_factory() as session:
            await session.execute(
                text("DELETE FROM project_subscription_profiles WHERE project_id = :project_id"),
                {"project_id": persisted_run.project_id},
            )
            await session.execute(
                text("DELETE FROM subscription_envelopes WHERE run_id = :run_id"),
                {"run_id": persisted_run.id},
            )
            await session.execute(
                text("DELETE FROM subscription_profile_versions WHERE profile_id = :profile_id"),
                {"profile_id": profile_id},
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(cleanup())
