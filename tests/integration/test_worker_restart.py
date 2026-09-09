"""Actual worker termination/restart recovery acceptance tests."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.domain.resource import ResourceState
from forge.domain.teardown import teardown_confirmation
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.models import Run, RunEvent
from forge.persistence.repositories.runs import _snapshot_from_record
from forge.tools.secrets import LocalSecretStore, SecretAlreadyExistsError
from sqlalchemy import select

from tests.acceptance.process_harness import idempotency_key
from tests.recovery_process.harness import RecoveryProcessHarness
from tests.recovery_process.worker_crash_hook import CRASH_EXIT_CODE

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _setup_fixture_repo(repo_path: Path, bare_path: Path, github_repo: str) -> str:
    repo_path.mkdir(parents=True, exist_ok=True)
    bare_path.mkdir(parents=True, exist_ok=True)
    (repo_path / "README.md").write_text("Acceptance fixture\n", encoding="utf-8")
    (repo_path / "check_readme.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "text = Path('README.md').read_text(encoding='utf-8')\n"
        "sys.exit(0 if 'Verified delivery' in text else 1)\n",
        encoding="utf-8",
    )
    (repo_path / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")

    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(bare_path), "init", "--bare", "-b", "main"],
        check=True,
        capture_output=True,
    )

    for command in (
        ("init", "-b", "main"),
        ("config", "user.name", "Forge fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        ("add", "README.md", "check_readme.py", ".gitignore"),
        ("commit", "-m", "initial"),
        ("push", str(bare_path), "main"),
        ("remote", "add", "origin", f"https://github.com/{github_repo}.git"),
    ):
        await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(repo_path), *command],
            check=True,
            capture_output=True,
        )

    base_sha = (
        await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    ).stdout.strip()
    return base_sha


async def _bootstrap_session(harness: RecoveryProcessHarness, client: httpx.AsyncClient) -> str:
    token = harness.bootstrap_token()
    resp = await client.post(
        "/api/auth/bootstrap",
        json={"token": token},
        headers={"Idempotency-Key": idempotency_key()},
    )
    assert resp.status_code == 200, resp.text
    csrf = resp.json()["csrf_token"]
    client.headers["X-CSRF-Token"] = csrf
    return csrf


async def _setup_run(
    client: httpx.AsyncClient,
    repo_dir: Path,
    github_repo: str,
    *,
    enable_database: bool = False,
) -> tuple[str, UUID]:
    proj_body: dict[str, object] = {
        "name": "Fixture project",
        "repository_path": str(repo_dir),
        "github_repository": github_repo,
        "default_branch": "main",
        "runner_mode": "trusted_host",
        "trusted_project": True,
        "commands": [
            {
                "kind": "test",
                "name": "unit",
                "argv": [sys.executable, "check_readme.py"],
                "timeout_seconds": 30,
            }
        ],
    }
    if enable_database:
        proj_body["database"] = {
            "enabled": True,
            "admin_url_secret_reference": "secret://environment/FORGE_ACCEPTANCE_DB_ADMIN",
            "injected_environment_key": "DATABASE_URL",
        }

    proj = await client.post(
        "/api/projects",
        json=proj_body,
        headers={"Idempotency-Key": idempotency_key()},
    )
    assert proj.status_code == 201, proj.text
    project_id = proj.json()["id"]

    task = await client.post(
        "/api/tasks",
        json={"project_id": project_id, "title": "Fixture task", "body": "Update README"},
        headers={"Idempotency-Key": idempotency_key()},
    )
    assert task.status_code == 201, task.text

    run = await client.post(
        "/api/runs",
        json={"task_id": task.json()["id"]},
        headers={"Idempotency-Key": idempotency_key()},
    )
    assert run.status_code == 201, run.text
    return project_id, UUID(run.json()["id"])


async def _approve_gate(
    client: httpx.AsyncClient, run_id: UUID, *, gate: str, run_version: int, evidence_digest: str
) -> None:
    """Authorize a gate through the current challenge-bound approval API."""
    body = {
        "gate": gate,
        "run_version": run_version,
        "evidence_digest": evidence_digest,
    }
    challenge = await client.post(
        f"/api/runs/{run_id}/approval-challenges",
        json=body,
        headers={"Idempotency-Key": idempotency_key()},
    )
    assert challenge.status_code == 200, challenge.text
    approval = await client.post(
        f"/api/runs/{run_id}/approvals",
        json={**body, "challenge_token": challenge.json()["token"]},
        headers={"Idempotency-Key": idempotency_key()},
    )
    assert approval.status_code == 202, approval.text


async def _enqueue_teardown(
    client: httpx.AsyncClient, run_id: UUID, *, run_version: int, confirmation: str
) -> httpx.Response:
    return await client.post(
        f"/api/runs/{run_id}/commands",
        json={
            "command_type": "teardown_run_resources",
            "expected_run_version": run_version,
            "confirm_resource_identity": confirmation,
            "delete_branch": False,
        },
        headers={"Idempotency-Key": idempotency_key()},
    )


async def _publish_green_check(
    harness: RecoveryProcessHarness, factory, run_id: UUID, github_repo: str
) -> None:
    """Supply the authoritative required check after the exact PR is recorded."""
    # Startup must wait for the crashed operation's persisted 30-second lease
    # before its observation-only reconciler can claim it.  Leave enough room
    # for that required wait plus publication finalization.
    await harness.wait_for_state(factory, run_id, "MONITORING_PR", timeout=45)
    prs = [
        pull_request
        for (repository, _), pull_request in harness.fake_github.pull_requests.items()
        if repository == github_repo.casefold()
    ]
    assert len(prs) == 1
    pull_request = prs[0]
    harness.fake_github.checks[(github_repo.casefold(), pull_request.head_sha)] = [
        CheckSnapshot(
            name="ci",
            status="completed",
            conclusion="success",
            head_sha=pull_request.head_sha,
            summary="Recovery matrix CI green",
        )
    ]
    await harness.expedite_commands(factory, run_id)


# ===========================================================================
# 1. Early Lifecycle Crashes: command_lease, provider_request, state_event_commit
# ===========================================================================


@pytest.mark.parametrize("crash_point", ["command_lease", "provider_request", "state_event_commit"])
async def test_worker_crash_and_restart_during_planning(
    tmp_path: Path, migrated_database_url: str, crash_point: str
) -> None:
    """Worker terminates at planning crash points and safely resumes upon restart."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    repo_dir = tmp_path / "repo"
    bare_dir = tmp_path / "bare.git"
    github_repo = f"example/planning-{crash_point.replace('_', '-')}"
    base_sha = await _setup_fixture_repo(repo_dir, bare_dir, github_repo=github_repo)

    with RecoveryProcessHarness(
        database_url=migrated_database_url,
        data_root=data_root,
        prompt_root=Path(__file__).resolve().parents[2] / "agents",
        bare_remote_path=bare_dir,
    ) as harness:
        engine = create_engine(migrated_database_url)
        factory = create_session_factory(engine)

        harness.fake_github.bases[(github_repo, "main")] = base_sha
        harness.fake_github.branch_shas[(github_repo, "main")] = base_sha

        harness.start_api()
        harness.start_worker(crash_point=crash_point)
        harness.assert_process_identities()
        crashed_pid = harness.worker_pid

        async with httpx.AsyncClient(
            base_url=harness.base_url, headers={"Origin": harness.base_url}
        ) as client:
            await _bootstrap_session(harness, client)
            _project_id, run_id = await _setup_run(client, repo_dir, github_repo)

            # 1. Assert worker process terminated at the exact crash point
            exit_code = await harness.wait_for_worker_crash(expected_code=CRASH_EXIT_CODE)
            assert exit_code == CRASH_EXIT_CODE

            # Expedite command lease expiration for fast pickup on restart
            await harness.expedite_commands(factory, run_id)

            # 2. Restart worker cleanly against the same durable PostgreSQL state
            new_pid = harness.restart_worker(crash_point=None)
            assert new_pid != crashed_pid
            assert new_pid != harness.api_pid

            # 3. Assert reconciled outcome:
            # - For unacknowledged provider execution: moves safely to explicit intervention
            # - For command lease / state event: safely resumes to AWAITING_PLAN_APPROVAL
            if crash_point == "provider_request":
                observed = await harness.wait_for_state(
                    factory, run_id, "AWAITING_HUMAN_INTERVENTION", timeout=30
                )
                async with factory() as session:
                    events = list(
                        await session.scalars(
                            select(RunEvent).where(RunEvent.run_id == run_id)
                        )
                    )
                    assert any(e.event_type == "run.recovery_intervention" for e in events)
            else:
                observed = await harness.wait_for_state(
                    factory, run_id, "AWAITING_PLAN_APPROVAL", timeout=30
                )
                assert observed.pending_evidence_digest

        await engine.dispose()


# ===========================================================================
# 2. Preparation / Worktree / Database Crashes
# ===========================================================================


@pytest.mark.parametrize(
    "crash_point",
    ["worktree_intent", "worktree_creation", "database_creation", "database_disabled_no_intent"],
)
async def test_worker_crash_and_restart_during_preparation(
    tmp_path: Path, migrated_database_url: str, crash_point: str
) -> None:
    """Worker terminates during resource preparation and reconciles without duplicate resources."""
    enable_database = crash_point == "database_creation"
    data_root = tmp_path / "data"
    data_root.mkdir()
    repo_dir = tmp_path / "repo"
    bare_dir = tmp_path / "bare.git"
    github_repo = f"example/prep-{crash_point.replace('_', '-')}"
    base_sha = await _setup_fixture_repo(repo_dir, bare_dir, github_repo=github_repo)

    store = LocalSecretStore(data_root)
    try:
        store.create("acceptance-db-admin", migrated_database_url.encode("utf-8"))
    except SecretAlreadyExistsError:
        pass

    with RecoveryProcessHarness(
        database_url=migrated_database_url,
        data_root=data_root,
        prompt_root=Path(__file__).resolve().parents[2] / "agents",
        bare_remote_path=bare_dir,
    ) as harness:
        engine = create_engine(migrated_database_url)
        factory = create_session_factory(engine)

        harness.fake_github.bases[(github_repo, "main")] = base_sha
        harness.fake_github.branch_shas[(github_repo, "main")] = base_sha

        harness.start_api()
        # Start worker initially without crash to complete planning
        harness.start_worker(crash_point=None)
        harness.assert_process_identities()

        async with httpx.AsyncClient(
            base_url=harness.base_url, headers={"Origin": harness.base_url}
        ) as client:
            await _bootstrap_session(harness, client)
            _project_id, run_id = await _setup_run(
                client, repo_dir, github_repo, enable_database=enable_database
            )

            # Wait for plan approval gate
            observed = await harness.wait_for_state(
                factory, run_id, "AWAITING_PLAN_APPROVAL", timeout=30
            )

            # Restart worker with targeted preparation crash point
            harness.restart_worker(crash_point=crash_point)
            crashed_pid = harness.worker_pid

            # Approve plan to trigger worktree/database preparation
            await _approve_gate(client, run_id, gate="plan", run_version=observed.version,
                                evidence_digest=observed.pending_evidence_digest)

            # 1. Assert worker process terminated at the exact crash point
            exit_code = await harness.wait_for_worker_crash(expected_code=CRASH_EXIT_CODE)
            assert exit_code == CRASH_EXIT_CODE

            # Expedite command lease expiration for fast pickup on restart
            await harness.expedite_commands(factory, run_id)

            # 2. Restart worker cleanly against the same durable state
            new_pid = harness.restart_worker(crash_point=None)
            assert new_pid != crashed_pid

            # 3. Assert one reconciled outcome. An intent-only crash has no
            # authoritative external result and must stop for intervention;
            # completed resource operations resume into implementation.
            if crash_point == "worktree_intent":
                await harness.wait_for_state(
                    factory, run_id, "AWAITING_HUMAN_INTERVENTION", timeout=35
                )
                async with factory() as session:
                    events = list(
                        await session.scalars(
                            select(RunEvent).where(RunEvent.run_id == run_id)
                        )
                    )
                assert any(event.event_type == "run.recovery_intervention" for event in events)
            else:
                observed_impl = await harness.wait_for_state(
                    factory, run_id, "IMPLEMENTING", timeout=35
                )
                assert observed_impl.worktree_path is not None
                assert Path(observed_impl.worktree_path).exists()

                if enable_database:
                    assert observed_impl.database_state == ResourceState.ACTIVE.value
                else:
                    assert observed_impl.database_state == ResourceState.DISABLED.value

        await engine.dispose()


# ===========================================================================
# 3. Development / Implementation Crashes: file_write, local_commit
# ===========================================================================


@pytest.mark.parametrize("crash_point", ["file_write", "local_commit"])
async def test_worker_crash_and_restart_during_development(
    tmp_path: Path, migrated_database_url: str, crash_point: str
) -> None:
    """Worker terminates during file write or local commit and safely reconciles candidate."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    repo_dir = tmp_path / "repo"
    bare_dir = tmp_path / "bare.git"
    github_repo = f"example/dev-{crash_point.replace('_', '-')}"
    base_sha = await _setup_fixture_repo(repo_dir, bare_dir, github_repo=github_repo)

    with RecoveryProcessHarness(
        database_url=migrated_database_url,
        data_root=data_root,
        prompt_root=Path(__file__).resolve().parents[2] / "agents",
        bare_remote_path=bare_dir,
    ) as harness:
        engine = create_engine(migrated_database_url)
        factory = create_session_factory(engine)

        harness.fake_github.bases[(github_repo, "main")] = base_sha
        harness.fake_github.branch_shas[(github_repo, "main")] = base_sha

        harness.start_api()
        harness.start_worker(crash_point=None)
        harness.assert_process_identities()

        async with httpx.AsyncClient(
            base_url=harness.base_url, headers={"Origin": harness.base_url}
        ) as client:
            await _bootstrap_session(harness, client)
            _project_id, run_id = await _setup_run(client, repo_dir, github_repo)

            observed = await harness.wait_for_state(
                factory, run_id, "AWAITING_PLAN_APPROVAL", timeout=30
            )

            # Switch worker to targeted development crash point
            harness.restart_worker(crash_point=crash_point)
            crashed_pid = harness.worker_pid

            # Approve plan to trigger preparation and implementation
            await _approve_gate(client, run_id, gate="plan", run_version=observed.version,
                                evidence_digest=observed.pending_evidence_digest)

            # 1. Assert worker terminates at the write or commit boundary
            exit_code = await harness.wait_for_worker_crash(expected_code=CRASH_EXIT_CODE)
            assert exit_code == CRASH_EXIT_CODE

            await harness.expedite_commands(factory, run_id)

            # 2. Restart worker cleanly against same state
            new_pid = harness.restart_worker(crash_point=None)
            assert new_pid != crashed_pid

            # 3. An interrupted developer execution has no authoritative
            # completion receipt, so restart must stop for intervention.
            await harness.wait_for_state(
                factory, run_id, "AWAITING_HUMAN_INTERVENTION", timeout=45
            )
            async with factory() as session:
                events = list(
                    await session.scalars(
                        select(RunEvent).where(RunEvent.run_id == run_id)
                    )
                )
            assert any(event.event_type == "run.recovery_intervention" for event in events)

        await engine.dispose()


# ===========================================================================
# 4. Release / Publication / Merge Crashes: push, pr_creation, merge_request
# ===========================================================================


@pytest.mark.parametrize("crash_point", ["push", "pr_creation", "merge_request"])
async def test_worker_crash_and_restart_during_release(
    tmp_path: Path, migrated_database_url: str, crash_point: str
) -> None:
    """Worker terminates during release mutations and reconciles without duplicate remote effects."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    repo_dir = tmp_path / "repo"
    bare_dir = tmp_path / "bare.git"
    github_repo = f"example/rel-{crash_point.replace('_', '-')}"
    base_sha = await _setup_fixture_repo(repo_dir, bare_dir, github_repo=github_repo)

    with RecoveryProcessHarness(
        database_url=migrated_database_url,
        data_root=data_root,
        prompt_root=Path(__file__).resolve().parents[2] / "agents",
        bare_remote_path=bare_dir,
    ) as harness:
        engine = create_engine(migrated_database_url)
        factory = create_session_factory(engine)

        harness.fake_github.bases[(github_repo, "main")] = base_sha
        harness.fake_github.branch_shas[(github_repo, "main")] = base_sha
        # The release path must observe a policy that is safe for a managed
        # merge.  An unsafe fixture correctly produces an intervention, which
        # would prevent this matrix from reaching its push/PR/merge boundaries.
        harness.fake_github.merge_protections[(github_repo, "main")] = MergeProtection(
            strict_required_checks=True,
            merge_queue_enabled=False,
            actor_can_bypass=False,
            evidence_source="classic",
            verified=True,
            required_check_names=("ci",),
        )

        harness.start_api()
        harness.start_worker(crash_point=None)
        harness.assert_process_identities()

        async with httpx.AsyncClient(
            base_url=harness.base_url, headers={"Origin": harness.base_url}
        ) as client:
            await _bootstrap_session(harness, client)
            _project_id, run_id = await _setup_run(client, repo_dir, github_repo)

            # Reach AWAITING_PLAN_APPROVAL
            plan_gate = await harness.wait_for_state(
                factory, run_id, "AWAITING_PLAN_APPROVAL", timeout=30
            )
            await _approve_gate(client, run_id, gate="plan", run_version=plan_gate.version,
                                evidence_digest=plan_gate.pending_evidence_digest)

            # Reach AWAITING_PR_APPROVAL
            pr_gate = await harness.wait_for_state(
                factory, run_id, "AWAITING_PR_APPROVAL", timeout=45
            )

            if crash_point in {"push", "pr_creation"}:
                harness.restart_worker(crash_point=crash_point)
                crashed_pid = harness.worker_pid

                # Approve PR to trigger publication
                await _approve_gate(client, run_id, gate="pr", run_version=pr_gate.version,
                                    evidence_digest=pr_gate.pending_evidence_digest)

                # Assert crash at push or pr_creation
                exit_code = await harness.wait_for_worker_crash(expected_code=CRASH_EXIT_CODE)
                assert exit_code == CRASH_EXIT_CODE

                await harness.expedite_commands(factory, run_id)
                new_pid = harness.restart_worker(crash_point=None)
                assert new_pid != crashed_pid

                await _publish_green_check(harness, factory, run_id, github_repo)

                # Reconciles publication through the recorded PR and green check.
                observed_rel = await harness.wait_for_state(
                    factory, run_id, "AWAITING_MERGE_APPROVAL", timeout=45
                )
                assert observed_rel.pending_evidence_digest

                # Verify exactly one PR was created on fake GitHub (no duplicate)
                prs = [
                    pull_request
                    for (repository, _), pull_request in harness.fake_github.pull_requests.items()
                    if repository == github_repo.casefold()
                ]
                assert len(prs) == 1
            else:
                # For merge_request: approve PR normally, reach merge gate
                await _approve_gate(client, run_id, gate="pr", run_version=pr_gate.version,
                                    evidence_digest=pr_gate.pending_evidence_digest)
                await _publish_green_check(harness, factory, run_id, github_repo)
                merge_gate = await harness.wait_for_state(
                    factory, run_id, "AWAITING_MERGE_APPROVAL", timeout=45
                )

                harness.restart_worker(crash_point="merge_request")
                crashed_pid = harness.worker_pid

                await _approve_gate(client, run_id, gate="merge", run_version=merge_gate.version,
                                    evidence_digest=merge_gate.pending_evidence_digest)

                exit_code = await harness.wait_for_worker_crash(expected_code=CRASH_EXIT_CODE)
                assert exit_code == CRASH_EXIT_CODE

                await harness.expedite_commands(factory, run_id)
                new_pid = harness.restart_worker(crash_point=None)
                assert new_pid != crashed_pid

                # Terminal merge recovery discovers merged PR and marks run COMPLETED
                observed_done = await harness.wait_for_state(
                    factory, run_id, "COMPLETED", timeout=45
                )
                assert observed_done.state == "COMPLETED"

        await engine.dispose()


# ===========================================================================
# 5. Teardown Crashes: worktree_removal, database_drop, database_disabled_teardown
# ===========================================================================


@pytest.mark.parametrize(
    "crash_point",
    ["worktree_removal", "database_drop", "database_disabled_teardown"],
)
async def test_worker_crash_and_restart_during_teardown(
    tmp_path: Path, migrated_database_url: str, crash_point: str
) -> None:
    """Worker terminates during resource teardown and safely reconciles resource absence."""
    enable_database = crash_point == "database_drop"
    data_root = tmp_path / "data"
    data_root.mkdir()
    repo_dir = tmp_path / "repo"
    bare_dir = tmp_path / "bare.git"
    github_repo = f"example/tear-{crash_point.replace('_', '-')}"
    base_sha = await _setup_fixture_repo(repo_dir, bare_dir, github_repo=github_repo)

    store = LocalSecretStore(data_root)
    try:
        store.create("acceptance-db-admin", migrated_database_url.encode("utf-8"))
    except SecretAlreadyExistsError:
        pass

    with RecoveryProcessHarness(
        database_url=migrated_database_url,
        data_root=data_root,
        prompt_root=Path(__file__).resolve().parents[2] / "agents",
        bare_remote_path=bare_dir,
    ) as harness:
        engine = create_engine(migrated_database_url)
        factory = create_session_factory(engine)

        harness.fake_github.bases[(github_repo, "main")] = base_sha
        harness.fake_github.branch_shas[(github_repo, "main")] = base_sha
        harness.fake_github.merge_protections[(github_repo, "main")] = MergeProtection(
            strict_required_checks=True,
            merge_queue_enabled=False,
            actor_can_bypass=False,
            evidence_source="classic",
            verified=True,
            required_check_names=("ci",),
        )

        harness.start_api()
        harness.start_worker(crash_point=None)
        harness.assert_process_identities()

        async with httpx.AsyncClient(
            base_url=harness.base_url, headers={"Origin": harness.base_url}
        ) as client:
            await _bootstrap_session(harness, client)
            _project_id, run_id = await _setup_run(
                client, repo_dir, github_repo, enable_database=enable_database
            )

            # Complete flow to COMPLETED
            plan_gate = await harness.wait_for_state(
                factory, run_id, "AWAITING_PLAN_APPROVAL", timeout=30
            )
            await _approve_gate(client, run_id, gate="plan", run_version=plan_gate.version,
                                evidence_digest=plan_gate.pending_evidence_digest)
            pr_gate = await harness.wait_for_state(
                factory, run_id, "AWAITING_PR_APPROVAL", timeout=45
            )
            await _approve_gate(client, run_id, gate="pr", run_version=pr_gate.version,
                                evidence_digest=pr_gate.pending_evidence_digest)
            await _publish_green_check(harness, factory, run_id, github_repo)
            merge_gate = await harness.wait_for_state(
                factory, run_id, "AWAITING_MERGE_APPROVAL", timeout=45
            )
            await _approve_gate(client, run_id, gate="merge", run_version=merge_gate.version,
                                evidence_digest=merge_gate.pending_evidence_digest)
            completed_run = await harness.wait_for_state(
                factory, run_id, "COMPLETED", timeout=45
            )

            # Switch worker to targeted teardown crash point
            harness.restart_worker(crash_point=crash_point)
            crashed_pid = harness.worker_pid

            # Request teardown
            teardown_resp = await _enqueue_teardown(
                client,
                run_id,
                run_version=completed_run.version,
                confirmation=teardown_confirmation(_snapshot_from_record(completed_run)),
            )
            # Teardown is queued durably; the command endpoint acknowledges
            # admission rather than completing resource removal synchronously.
            assert teardown_resp.status_code == 202, teardown_resp.text

            exit_code = await harness.wait_for_worker_crash(expected_code=CRASH_EXIT_CODE)
            assert exit_code == CRASH_EXIT_CODE

            await harness.expedite_commands(factory, run_id)
            new_pid = harness.restart_worker(crash_point=None)
            assert new_pid != crashed_pid

            # Reconciles teardown to completion.
            for _ in range(120):
                async with factory() as session:
                    run_row = await session.get(Run, run_id)
                if (
                    run_row
                    and run_row.worktree_path is None
                    and run_row.database_state
                    == (
                        ResourceState.REMOVED.value
                        if enable_database
                        else ResourceState.DISABLED.value
                    )
                ):
                    break
                await asyncio.sleep(0.5)
            assert run_row is not None
            assert run_row.worktree_path is None
            if enable_database:
                assert run_row.database_state == ResourceState.REMOVED.value
            else:
                assert run_row.database_state == ResourceState.DISABLED.value

        await engine.dispose()
