"""Browser-free proof for the hosted browser acceptance control bridge."""

from __future__ import annotations

import time
from pathlib import Path
from uuid import UUID

import pytest

from tests.acceptance.browser_control import Bridge

pytestmark = pytest.mark.integration
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


def test_bridge_drives_real_completion_and_cancelled_teardown(
    migrated_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_DATABASE_URL", migrated_database_url)
    bridge = Bridge()
    try:
        run_id = bridge._create_run(repository=bridge.github_repository, database=False)
        bridge.register_run(run_id)
        for state, gate in (
            ("AWAITING_PLAN_APPROVAL", "plan"),
            ("AWAITING_PR_APPROVAL", "pr"),
            ("AWAITING_MERGE_APPROVAL", "merge"),
        ):
            assert len(bridge.state(state, run_id)["evidenceDigest"]) == 64
            bridge.approve(run_id, gate)
        assert bridge.state("COMPLETED", run_id)["state"] == "COMPLETED"
        assert bridge.harness.fake_github.effect_counts["prs_created"] == 1
        assert bridge.harness.fake_github.effect_counts["merges"] == 1

        cancelled_run = bridge._create_run(repository="example/bridge-restart", database=True)
        bridge.state("AWAITING_PLAN_APPROVAL", cancelled_run)
        bridge.approve(cancelled_run, "plan")
        bridge.state("AWAITING_PR_APPROVAL", cancelled_run)
        before = bridge._read_run(UUID(cancelled_run))
        assert before is not None and before["worktree_path"]
        assert Path(str(before["worktree_path"])).exists()
        branch = str(before["branch_name"])
        old_pid = bridge.harness.worker_pid
        bridge.stop_worker()
        bridge.enqueue_cancel(cancelled_run)
        assert bridge.cancel_command(cancelled_run)["status"] == "PENDING"
        assert bridge.harness.restart_worker() != old_pid
        bridge.state("CANCELLED", cancelled_run)
        retained = bridge._read_run(UUID(cancelled_run))
        assert retained is not None and Path(str(retained["worktree_path"])).exists()
        bridge.teardown(cancelled_run)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            settled = bridge._read_run(UUID(cancelled_run))
            if settled is not None and settled["database_state"] == "REMOVED":
                break
            time.sleep(0.1)
        else:
            pytest.fail("cancelled run teardown did not remove its database resource")
        assert not Path(str(retained["worktree_path"])).exists()
        assert (
            __import__("subprocess")
            .run(
                [
                    "git",
                    "-C",
                    str(retained["repository_path"]),
                    "show-ref",
                    f"refs/heads/{branch}",
                ],
                capture_output=True,
            )
            .returncode
            == 0
        )
    finally:
        bridge.harness.close()
