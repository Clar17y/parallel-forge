"""Process-level HTTP bootstrap and full vertical slice acceptance tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.domain.teardown import teardown_confirmation
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.models import OperationIntent, Run, RunEvent
from forge.persistence.repositories.runs import _snapshot_from_record
from forge.tools.secrets import LocalSecretStore, SecretAlreadyExistsError
from sqlalchemy import select

from tests.acceptance.process_harness import ForgeProcessHarness, idempotency_key

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)

_GIT_IDENTITY_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Forge fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Forge fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}


async def test_vertical_slice_bootstraps_operator_over_real_api_process(
    tmp_path, migrated_database_url
):
    """A process boundary remains real before the longer worker flow is exercised."""

    data_root = tmp_path / "data"
    data_root.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("Acceptance fixture\n", encoding="utf-8")
    (repository / "check_readme.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    for command in (
        ("init", "-b", "main"),
        ("config", "user.name", "Forge fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        ("add", "README.md", "check_readme.py"),
        ("commit", "-m", "initial"),
        ("remote", "add", "origin", "https://github.com/example/acceptance.git"),
    ):
        await asyncio.to_thread(
            subprocess.run, ["git", "-C", str(repository), *command], check=True, capture_output=True
        )
    with ForgeProcessHarness(
        database_url=migrated_database_url,
        data_root=data_root,
        prompt_root=Path(__file__).resolve().parents[2] / "agents",
    ) as harness:
        engine = create_engine(migrated_database_url)
        factory = create_session_factory(engine)
        harness.start_api()
        harness.start_worker()
        harness.assert_process_identities()
        token = harness.bootstrap_token()
        async with httpx.AsyncClient(base_url=harness.base_url, headers={"Origin": harness.base_url}) as client:
            response = await client.post(
                "/api/auth/bootstrap", json={"token": token}, headers={"Idempotency-Key": idempotency_key()}
            )
            assert response.status_code == 200, response.text
            csrf = response.json()["csrf_token"]
            session = response.cookies.get("forge_session")
            assert session and csrf
            health = await client.get("/api/health")
            assert health.json() == {"status": "ok", "role": "api"}
            client.headers["X-CSRF-Token"] = csrf
            project = await client.post(
                "/api/projects",
                json={
                    "name": "Acceptance fixture",
                    "repository_path": str(repository),
                    "github_repository": "example/acceptance",
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
                },
                headers={"Idempotency-Key": idempotency_key()},
            )
            assert project.status_code == 201, project.text
            task = await client.post(
                "/api/tasks",
                json={
                    "project_id": project.json()["id"],
                    "title": "Update README",
                    "body": "Acceptance task",
                },
                headers={"Idempotency-Key": idempotency_key()},
            )
            assert task.status_code == 201, task.text
            run = await client.post(
                "/api/runs",
                json={"task_id": task.json()["id"]},
                headers={"Idempotency-Key": idempotency_key()},
            )
            assert run.status_code == 201, run.text
            observed = await harness.wait_for_state(
                factory, UUID(run.json()["id"]), "AWAITING_PLAN_APPROVAL"
            )
            assert observed.pending_evidence_digest
        await engine.dispose()


async def run_vertical_slice_process_acceptance(
    tmp_path, migrated_database_url, *, race_mode: str | None = None
):
    """Complete 13-step vertical slice across actual API/worker processes with PostgreSQL.

    1. bootstrap operator session;
    2. register project/policy with per-worktree DB test admin reference;
    3. create task and run;
    4. wait for plan gate and approve exact plan;
    5. observe worktree/database preparation;
    6. simulate failed local check, one remediation, passing validation, and independent review;
    7. approve exact PR candidate;
    8. observe managed push and one PR;
    9. simulate failed remote CI, one remediation push, then green checks;
    10. observe merge gate;
    11. approve exact head/base and merge;
    12. verify COMPLETED while worktree/database remain;
    13. explicitly tear down and verify branch remains.
    """

    data_root = tmp_path / "data"
    data_root.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    bare_remote = tmp_path / "bare.git"
    bare_remote.mkdir()

    # Create check script that fails on 'Needs repair' and passes on 'Verified delivery'
    check_script = (
        "import sys\n"
        "from pathlib import Path\n"
        "text = Path('README.md').read_text(encoding='utf-8')\n"
        "sys.exit(0 if 'Verified delivery' in text else 1)\n"
    )
    (repository / "README.md").write_text("Acceptance fixture\n", encoding="utf-8")
    (repository / "check_readme.py").write_text(check_script, encoding="utf-8")
    (repository / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    await asyncio.to_thread(
        subprocess.run, ["git", "-C", str(bare_remote), "init", "--bare", "-b", "main"], check=True
    )
    for command in (
        ("init", "-b", "main"),
        ("config", "user.name", "Forge fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        ("add", "README.md", "check_readme.py", ".gitignore"),
        ("commit", "-m", "initial"),
        ("push", str(bare_remote), "main"),
        ("remote", "add", "origin", "https://github.com/example/acceptance.git"),
    ):
        await asyncio.to_thread(
            subprocess.run, ["git", "-C", str(repository), *command], check=True, capture_output=True
        )

    base_sha = (
        await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    ).stdout.strip()

    # Register admin secret for database provisioning
    store = LocalSecretStore(data_root)
    try:
        store.create("acceptance-db-admin", migrated_database_url.encode("utf-8"))
    except SecretAlreadyExistsError:
        pass

    with ForgeProcessHarness(
        database_url=migrated_database_url,
        data_root=data_root,
        prompt_root=Path(__file__).resolve().parents[2] / "agents",
        bare_remote_path=bare_remote,
    ) as harness:
        engine = create_engine(migrated_database_url)
        factory = create_session_factory(engine)

        harness.fake_github.bases[("example/acceptance", "main")] = base_sha
        harness.fake_github.merge_protections[("example/acceptance", "main")] = MergeProtection(
            strict_required_checks=True,
            merge_queue_enabled=False,
            actor_can_bypass=False,
            evidence_source="classic",
            verified=True,
            required_check_names=("ci",),
        )
        harness.fake_github.branch_shas[("example/acceptance", "main")] = base_sha

        # Step 1: Start processes and bootstrap operator session
        harness.start_api()
        harness.start_worker()
        harness.assert_process_identities()

        token = harness.bootstrap_token()
        async with httpx.AsyncClient(base_url=harness.base_url, headers={"Origin": harness.base_url}) as client:
            resp = await client.post(
                "/api/auth/bootstrap", json={"token": token}, headers={"Idempotency-Key": idempotency_key()}
            )
            assert resp.status_code == 200, resp.text
            csrf = resp.json()["csrf_token"]
            client.headers["X-CSRF-Token"] = csrf

            # Step 2: Register project with per-worktree database provisioning
            proj_resp = await client.post(
                "/api/projects",
                json={
                    "name": "Acceptance fixture",
                    "repository_path": str(repository),
                    "github_repository": "example/acceptance",
                    "default_branch": "main",
                    "runner_mode": "trusted_host",
                    "trusted_project": True,
                    "database": {
                        "enabled": True,
                        "admin_url_secret_reference": "secret://environment/FORGE_ACCEPTANCE_DB_ADMIN",
                        "injected_environment_key": "DATABASE_URL",
                    },
                    "commands": [
                        {
                            "kind": "test",
                            "name": "unit",
                            "argv": [sys.executable, "check_readme.py"],
                            "timeout_seconds": 30,
                        }
                    ],
                },
                headers={"Idempotency-Key": idempotency_key()},
            )
            assert proj_resp.status_code == 201, proj_resp.text
            project_id = proj_resp.json()["id"]

            # Step 3: Create plain-text task and run
            task_resp = await client.post(
                "/api/tasks",
                json={"project_id": project_id, "title": "Update README", "body": "Acceptance task"},
                headers={"Idempotency-Key": idempotency_key()},
            )
            assert task_resp.status_code == 201, task_resp.text
            task_id = task_resp.json()["id"]

            run_resp = await client.post(
                "/api/runs",
                json={"task_id": task_id},
                headers={"Idempotency-Key": idempotency_key()},
            )
            assert run_resp.status_code == 201, run_resp.text
            run_id = UUID(run_resp.json()["id"])

            # Step 4: Wait for plan gate and approve exact plan
            plan_run = await harness.wait_for_state(factory, run_id, "AWAITING_PLAN_APPROVAL")
            assert plan_run.pending_evidence_digest

            challenge = await client.post(
                f"/api/runs/{run_id}/approval-challenges",
                json={
                    "gate": "plan",
                    "run_version": plan_run.version,
                    "evidence_digest": plan_run.pending_evidence_digest,
                },
                headers={"Idempotency-Key": idempotency_key()},
            )
            assert challenge.status_code == 200, challenge.text

            approval_resp = await client.post(
                f"/api/runs/{run_id}/approvals",
                json={
                    "gate": "plan",
                    "run_version": plan_run.version,
                    "evidence_digest": plan_run.pending_evidence_digest,
                    "challenge_token": challenge.json()["token"],
                },
                headers={"Idempotency-Key": idempotency_key()},
            )
            assert approval_resp.status_code == 202, approval_resp.text

            # Step 5 & 6: Observe preparation, failed local check, remediation, passing validation and review
            pr_gate_run = await harness.wait_for_state(factory, run_id, "AWAITING_PR_APPROVAL")
            assert pr_gate_run.worktree_path and Path(pr_gate_run.worktree_path).exists()
            assert pr_gate_run.database_name and pr_gate_run.database_state == "ACTIVE"
            assert pr_gate_run.local_remediation_count == 1
            assert (Path(pr_gate_run.worktree_path) / "README.md").read_text(
                encoding="utf-8"
            ) == "Verified delivery\n"
            assert pr_gate_run.pending_evidence_digest

            # Step 7: Approve exact PR candidate
            challenge = await client.post(
                f"/api/runs/{run_id}/approval-challenges",
                json={
                    "gate": "pr",
                    "run_version": pr_gate_run.version,
                    "evidence_digest": pr_gate_run.pending_evidence_digest,
                },
                headers={"Idempotency-Key": idempotency_key()},
            )
            assert challenge.status_code == 200, challenge.text

            approval_resp = await client.post(
                f"/api/runs/{run_id}/approvals",
                json={
                    "gate": "pr",
                    "run_version": pr_gate_run.version,
                    "evidence_digest": pr_gate_run.pending_evidence_digest,
                    "challenge_token": challenge.json()["token"],
                },
                headers={"Idempotency-Key": idempotency_key()},
            )
            assert approval_resp.status_code == 202, approval_resp.text

            # Step 8: Observe managed push and one PR
            await harness.wait_for_state(factory, run_id, "MONITORING_PR")
            assert harness.fake_github.effect_counts["prs_created"] == 1
            pr = harness.fake_github.pull_requests[("example/acceptance", 1)]
            assert pr.state == "open" and not pr.merged

            # Step 9: Simulate failed remote CI, one remediation push, then green checks
            harness.fake_github.checks[("example/acceptance", pr.head_sha)] = [
                CheckSnapshot(
                    name="ci",
                    status="completed",
                    conclusion="failure",
                    head_sha=pr.head_sha,
                    summary="Remote CI failed",
                )
            ]
            await harness.expedite_commands(factory, run_id)

            initial_pr_head = pr.head_sha
            # Wait for remediation push to complete and PR head to be updated
            deadline = asyncio.get_running_loop().time() + 60
            while asyncio.get_running_loop().time() < deadline:
                current_pr = harness.fake_github.pull_requests.get(("example/acceptance", 1))
                if current_pr is not None and current_pr.head_sha != initial_pr_head:
                    break
                await asyncio.sleep(0.1)

            updated_pr = harness.fake_github.pull_requests[("example/acceptance", 1)]
            assert updated_pr.head_sha != initial_pr_head

            async with factory() as session:
                current_run = await session.get(Run, run_id)
                assert current_run is not None and current_run.remote_remediation_count == 1

            # Mark checks green for new candidate head
            harness.fake_github.checks[("example/acceptance", updated_pr.head_sha)] = [
                CheckSnapshot(
                    name="ci",
                    status="completed",
                    conclusion="success",
                    head_sha=updated_pr.head_sha,
                    summary="Remote CI green",
                )
            ]
            await harness.expedite_commands(factory, run_id)

            # Step 10: Observe merge gate
            merge_run = await harness.wait_for_state(factory, run_id, "AWAITING_MERGE_APPROVAL")
            assert merge_run.pending_evidence_digest

            # Step 11: Approve exact head/base and merge
            pr = harness.fake_github.pull_requests[("example/acceptance", 1)]
            original_head_sha = pr.head_sha
            if race_mode == "head":
                tree_sha = (
                    await asyncio.to_thread(
                        subprocess.run,
                        ["git", "-C", str(bare_remote), "rev-parse", f"{original_head_sha}^{{tree}}"],
                        check=True, capture_output=True, text=True,
                        env=_GIT_IDENTITY_ENV,
                    )
                ).stdout.strip()
                raced_head_sha = (
                    await asyncio.to_thread(
                        subprocess.run,
                        ["git", "-C", str(bare_remote), "commit-tree", tree_sha,
                         "-p", original_head_sha, "-m", "acceptance head race"],
                        check=True, capture_output=True, text=True,
                        env=_GIT_IDENTITY_ENV,
                    )
                ).stdout.strip()
                await asyncio.to_thread(
                    subprocess.run,
                    ["git", "-C", str(bare_remote), "update-ref", f"refs/heads/{pr.head_ref}", raced_head_sha],
                    check=True,
                )
                harness.fake_github.update_branch_sha(
                    "example/acceptance", pr.head_ref, raced_head_sha
                )
            elif race_mode == "base":
                base_tree = (
                    await asyncio.to_thread(
                        subprocess.run,
                        ["git", "-C", str(bare_remote), "rev-parse", f"{base_sha}^{{tree}}"],
                        check=True, capture_output=True, text=True,
                        env=_GIT_IDENTITY_ENV,
                    )
                ).stdout.strip()
                raced_base_sha = (
                    await asyncio.to_thread(
                        subprocess.run,
                        ["git", "-C", str(bare_remote), "commit-tree", base_tree,
                         "-p", base_sha, "-m", "acceptance base race"],
                        check=True, capture_output=True, text=True, env=_GIT_IDENTITY_ENV,
                    )
                ).stdout.strip()
                await asyncio.to_thread(
                    subprocess.run,
                    ["git", "-C", str(bare_remote), "update-ref", "refs/heads/main", raced_base_sha],
                    check=True,
                )
                harness.fake_github.bases[("example/acceptance", "main")] = raced_base_sha
                harness.fake_github.branch_shas[("example/acceptance", "main")] = raced_base_sha
            elif race_mode is not None:
                raise AssertionError(f"unknown race mode: {race_mode}")
            challenge = await client.post(
                f"/api/runs/{run_id}/approval-challenges",
                json={
                    "gate": "merge",
                    "run_version": merge_run.version,
                    "evidence_digest": merge_run.pending_evidence_digest,
                },
                headers={"Idempotency-Key": idempotency_key()},
            )
            assert challenge.status_code == 200, challenge.text

            approval_resp = await client.post(
                f"/api/runs/{run_id}/approvals",
                json={
                    "gate": "merge",
                    "run_version": merge_run.version,
                    "evidence_digest": merge_run.pending_evidence_digest,
                    "challenge_token": challenge.json()["token"],
                },
                headers={"Idempotency-Key": idempotency_key()},
            )
            assert approval_resp.status_code == 202, approval_resp.text

            if race_mode is not None:
                if race_mode in {"head", "base"}:
                    deadline = asyncio.get_running_loop().time() + 30
                    while asyncio.get_running_loop().time() < deadline:
                        async with factory() as session:
                            stale = await session.scalar(
                                select(RunEvent).where(
                                    RunEvent.run_id == run_id,
                                    RunEvent.event_type == "approval.stale",
                                )
                            )
                        if stale is not None:
                            break
                        await asyncio.sleep(0.1)
                    else:
                        raise AssertionError("release race did not persist approval.stale")
                    if race_mode == "head":
                        harness.fake_github.update_branch_sha(
                            "example/acceptance", pr.head_ref, original_head_sha
                        )
                    else:
                        harness.fake_github.bases[("example/acceptance", "main")] = base_sha
                        harness.fake_github.branch_shas[("example/acceptance", "main")] = base_sha
                    await harness.wait_for_state(factory, run_id, "MONITORING_PR")
                    if race_mode == "head":
                        assert harness.fake_github.effect_counts["merges"] == 0
                        assert harness.fake_github.effect_counts["merge_conflicts"] == 0
                else:
                    await harness.wait_for_state(factory, run_id, "MONITORING_PR")
                raced_pr = harness.fake_github.pull_requests[("example/acceptance", 1)]
                harness.fake_github.checks[("example/acceptance", raced_pr.head_sha)] = [
                    CheckSnapshot(
                        name="ci",
                        status="completed",
                        conclusion="success",
                        head_sha=raced_pr.head_sha,
                        summary="Race reconciliation green",
                    )
                ]
                await harness.expedite_commands(factory, run_id)
                merge_run = await harness.wait_for_state(
                    factory, run_id, "AWAITING_MERGE_APPROVAL"
                )
                harness.restart_worker()
                merge_run = await harness.wait_for_state(
                    factory, run_id, "AWAITING_MERGE_APPROVAL"
                )
                challenge = await client.post(
                    f"/api/runs/{run_id}/approval-challenges",
                    json={
                        "gate": "merge",
                        "run_version": merge_run.version,
                        "evidence_digest": merge_run.pending_evidence_digest,
                    },
                    headers={"Idempotency-Key": idempotency_key()},
                )
                assert challenge.status_code == 200, challenge.text
                approval_resp = await client.post(
                    f"/api/runs/{run_id}/approvals",
                    json={
                        "gate": "merge",
                        "run_version": merge_run.version,
                        "evidence_digest": merge_run.pending_evidence_digest,
                        "challenge_token": challenge.json()["token"],
                    },
                    headers={"Idempotency-Key": idempotency_key()},
                )
                assert approval_resp.status_code == 202, approval_resp.text

            # Step 12: Verify COMPLETED while worktree/database remain
            completed_run = await harness.wait_for_state(factory, run_id, "COMPLETED")
            assert harness.fake_github.effect_counts["merges"] == 1
            final_pr = harness.fake_github.pull_requests[("example/acceptance", 1)]
            assert final_pr.merged and final_pr.state == "closed"
            assert Path(completed_run.worktree_path).exists()
            assert completed_run.database_state == "ACTIVE"

            # Step 13: Explicitly tear down and verify branch remains
            # ``Run`` is the persistence record; teardown confirmation is bound to
            # the domain snapshot so enum-valued resource state is preserved.
            confirm = teardown_confirmation(_snapshot_from_record(completed_run))
            teardown_resp = await client.post(
                f"/api/runs/{run_id}/commands",
                json={
                    "command_type": "teardown_run_resources",
                    "expected_run_version": completed_run.version,
                    "confirm_resource_identity": confirm,
                    "delete_branch": False,
                },
                headers={"Idempotency-Key": idempotency_key()},
            )
            assert teardown_resp.status_code == 202, teardown_resp.text
            await harness.expedite_commands(factory, run_id)

            # Wait for teardown completion in database
            deadline = asyncio.get_running_loop().time() + 30
            while asyncio.get_running_loop().time() < deadline:
                async with factory() as session:
                    current_run = await session.get(Run, run_id)
                    if current_run.database_state == "REMOVED":
                        break
                await asyncio.sleep(0.1)

            async with factory() as session:
                settled_run = await session.get(Run, run_id)
                if settled_run.database_state != "REMOVED":
                    intents = list(
                        await session.scalars(
                            select(OperationIntent).where(OperationIntent.run_id == run_id)
                        )
                    )
                    events = list(
                        await session.scalars(
                            select(RunEvent)
                            .where(RunEvent.run_id == run_id)
                            .order_by(RunEvent.sequence.desc())
                            .limit(8)
                        )
                    )
                    diagnostics = {
                        "run": {
                            "state": settled_run.state,
                            "database_state": settled_run.database_state,
                            "version": settled_run.version,
                        },
                        "teardown_operations": [
                            {
                                "kind": item.operation_kind,
                                "status": item.status,
                                "attempts": item.attempt_count,
                                "last_error": item.last_error,
                            }
                            for item in intents
                            if "teardown" in item.operation_kind
                        ],
                        "recent_events": [
                            {"sequence": event.sequence, "type": event.event_type}
                            for event in events
                        ],
                    }
                    pytest.fail(f"teardown did not settle: {diagnostics!r}")
            assert not Path(completed_run.worktree_path).exists()

            # Verify branch remains in git repository
            branch_check = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(repository), "show-ref", f"refs/heads/{completed_run.branch_name}"],
                capture_output=True,
                text=True,
            )
            assert branch_check.returncode == 0, f"Branch {completed_run.branch_name} was removed"

        await engine.dispose()


async def test_vertical_slice_complete_process_acceptance(tmp_path, migrated_database_url):
    await run_vertical_slice_process_acceptance(tmp_path, migrated_database_url)

