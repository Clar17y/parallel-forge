"""Providerless epic dispatch through the public API and separate worker."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
import pytest
from forge.application.services.auth import AuthService
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.observability.redaction import Redactor
from forge.persistence.unit_of_work import PostgresUnitOfWork

from scripts.dev import DefaultCommandRunner, WebConfig
from tests.acceptance.process_harness import (
    ForgeProcessHarness,
    free_loopback_port,
    idempotency_key,
)
from tests.integration.test_worker_restart import (
    _approve_gate,
    _bootstrap_session,
    _setup_fixture_repo,
)

pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


class EpicProcessHarness(ForgeProcessHarness):
    def start_worker(self) -> None:
        log = open(self._data_root / "worker.log", "a", encoding="utf-8")  # noqa: SIM115
        self._log_handles.append(log)
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "tests.epic_acceptance.worker_process"],
            cwd=self._cwd, env=self._env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, text=True,
        )
        self._processes.append(process)
        self._worker_process = process


@contextmanager
def _web_server(*, origin: str, api_origin: str):
    root = Path.cwd() / "apps/web"
    assert not list(root.glob(".env*")), "browser fixture needs no web dotenv"
    node = shutil.which("node")
    assert node, "Node 24 is required"
    next_cli = subprocess.check_output(
        [node, "-p", "require.resolve('next/dist/bin/next')"],
        cwd=root, text=True,
    ).strip()
    process = DefaultCommandRunner().spawn(
        "epic-acceptance-web",
        [node, next_cli, "start", "--hostname", "127.0.0.1", "--port", origin.rsplit(":", 1)[1]],
        cwd=root,
        env={**WebConfig(
            web_origin=origin, api_internal_origin=api_origin,
            web_port=int(origin.rsplit(":", 1)[1]),
        ).to_web_env(dict(os.environ)), "NEXT_TELEMETRY_DISABLED": "1"},
    )
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            assert process.poll() is None, "Next exited before browser acceptance"
            try:
                if httpx.get(origin, timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            raise AssertionError("Next did not become ready")
        yield
    finally:
        process.kill_tree()
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process.poll() is not None, "Next cleanup was not confirmed"


async def _browser_probe(
    *, database_url: str, origin: str, api_origin: str,
    epic_id: str, execution_id: str, phase: str,
) -> None:
    __tracebackhide__ = True
    from forge.persistence.database import create_engine, create_session_factory

    engine = create_engine(database_url)
    try:
        factory = create_session_factory(engine)
        token = await AuthService(lambda: PostgresUnitOfWork(factory)).issue_bootstrap()
    finally:
        await engine.dispose()
    node = shutil.which("node")
    assert node
    process = await asyncio.create_subprocess_exec(
        node, str(Path.cwd() / "tests/epic_acceptance/epic_browser.mjs"),
        cwd=Path.cwd() / "apps/web", env={**WebConfig(
            web_origin=origin, api_internal_origin=api_origin,
            web_port=int(origin.rsplit(":", 1)[1]),
        ).to_web_env(dict(os.environ)), "NEXT_TELEMETRY_DISABLED": "1"},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    request_bytes = json.dumps({
            "origin": origin, "token": token, "epicId": epic_id,
            "executionId": execution_id, "phase": phase,
        }).encode()
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(request_bytes), 75)
    except TimeoutError:
        await _report_browser_timeout(process)
    _assert_browser_result(process.returncode, stdout, stderr, token)


async def _report_browser_timeout(process) -> None:
    __tracebackhide__ = True
    try:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.communicate(), 5)
    except (OSError, TimeoutError, RuntimeError):
        raise AssertionError("browser probe timed out; child settlement failed") from None
    raise AssertionError("browser probe timed out") from None


def _assert_browser_result(
    returncode: int | None, stdout: bytes, stderr: bytes, token: str,
) -> None:
    __tracebackhide__ = True
    if returncode != 0:
        safe_stderr = Redactor(secrets=(token, quote(token, safe=""))).redact(
            stderr.decode(errors="replace")
        )
        raise AssertionError(f"browser probe exited {returncode}: {str(safe_stderr)[-2500:]}")
    try:
        result = json.loads(stdout)
    except (UnicodeError, json.JSONDecodeError):
        raise AssertionError("browser probe returned malformed evidence") from None
    assert isinstance(result, dict) and result.get("observed") is True, (
        "browser probe did not confirm observation"
    )


def test_browser_probe_diagnostics_redact_token_and_reject_malformed_output() -> None:
    token = f"dummy+{uuid4().hex}/bootstrap-token"
    encoded = quote(token, safe="")
    with pytest.raises(AssertionError) as failure:
        _assert_browser_result(
            1, b"", f"page.goto failed at /epics/x#bootstrap={encoded} raw={token}".encode(),
            token,
        )
    rendered_failure = str(failure.getrepr(funcargs=True))
    assert token not in rendered_failure
    assert encoded not in rendered_failure
    assert "[REDACTED]" in rendered_failure
    with pytest.raises(AssertionError, match="malformed evidence") as malformed:
        _assert_browser_result(0, f"{{bad:{token}".encode(), b"", token)
    assert token not in str(malformed.getrepr(funcargs=True))


@pytest.mark.asyncio
async def test_browser_timeout_settles_child_without_leaking_chained_error() -> None:
    token = f"dummy+{uuid4().hex}/bootstrap-token"

    class FakeProcess:
        returncode = None
        killed = False

        def kill(self) -> None:
            self.killed = True

        async def communicate(self):
            assert self.killed
            raise RuntimeError(f"child cleanup error contained {token}")

    process = FakeProcess()
    with pytest.raises(AssertionError, match="child settlement failed") as failure:
        await _report_browser_timeout(process)
    assert process.killed
    assert token not in str(failure.getrepr(funcargs=True))

    class SettledProcess:
        returncode = None
        killed = False

        def kill(self) -> None:
            self.killed = True

        async def communicate(self):
            assert self.killed
            self.returncode = -9
            return b"", b""

    settled = SettledProcess()
    with pytest.raises(AssertionError, match="browser probe timed out$"):
        await _report_browser_timeout(settled)
    assert settled.killed and settled.returncode == -9


def _real_bare_merge_with_stale_local_baseline(harness: EpicProcessHarness, bare: Path) -> None:
    """Script the remote merge; local canonical main changes only by owner sync."""
    state = harness.fake_github
    original = state.merge_pr

    def merge(repository: str, number: int, head_sha: str, method: str):
        base_sha = state.branch_shas[(repository.casefold(), "main")]
        ancestor = subprocess.run(
            ["git", "-C", str(bare), "merge-base", "--is-ancestor", base_sha, head_sha],
            capture_output=True, check=False,
        )
        assert ancestor.returncode == 0, "PR head is not based on the remote integration branch"
        result = original(repository, number, head_sha, method)
        subprocess.run(
            ["git", "-C", str(bare), "update-ref", "refs/heads/main", head_sha, base_sha],
            capture_output=True, check=True,
        )
        merged = replace(result, merge_sha=head_sha)
        key = (repository.casefold(), number)
        with state._lock:
            state.pull_requests[key] = merged
            state.branch_shas[(repository.casefold(), "main")] = head_sha
            state.bases[(repository.casefold(), "main")] = head_sha
        return merged

    state.merge_pr = merge


def _owner_sync_local_baseline(repo: Path, bare: Path) -> str:
    subprocess.run(
        ["git", "-C", str(repo), "fetch", str(bare), "main"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--ff-only", "FETCH_HEAD"],
        capture_output=True, check=True,
    )
    return subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()


async def _publish_green_check_for_run(
    harness: EpicProcessHarness, factory, run_id: UUID, github_repo: str,
) -> None:
    await harness.wait_for_state(factory, run_id, "MONITORING_PR", timeout=45)
    branch = f"forge/run/{run_id.hex}"
    prs = [
        pull for (repository, _), pull in harness.fake_github.pull_requests.items()
        if repository == github_repo.casefold()
        and pull.head_ref == branch
        and pull.state == "open"
    ]
    assert len(prs) == 1, f"expected one open PR for run {run_id}, found {len(prs)}"
    head_sha = prs[0].head_sha
    harness.fake_github.checks[(github_repo.casefold(), head_sha)] = [CheckSnapshot(
        name="ci", status="completed", conclusion="success",
        head_sha=head_sha, summary="Epic acceptance CI green",
    )]
    await harness.expedite_commands(factory, run_id)


async def _post(client: httpx.AsyncClient, path: str, body: dict, status: int = 201) -> dict:
    response = await client.post(
        path, json=body, headers={"Idempotency-Key": idempotency_key()}
    )
    assert response.status_code == status, f"{path}: {response.text}"
    return response.json()


async def _accepted_three_item_execution(
    client: httpx.AsyncClient, repo: Path
) -> tuple[str, str, list[str], list[str]]:
    project = await _post(client, "/api/projects", {
        "name": "Three item acceptance",
        "repository_path": str(repo),
        "github_repository": "example/epic-three-item",
        "default_branch": "main",
        "runner_mode": "trusted_host",
        "trusted_project": True,
        "commands": [{
            "kind": "test", "name": "unit",
            "argv": [sys.executable, "check_readme.py"], "timeout_seconds": 30,
        }],
    })
    requirement_ids = [str(uuid4()) for _ in range(3)]
    brief = {
        "schema_version": 1,
        "problem": "Deliver three integrated changes in order",
        "outcomes": ["Each required criterion has a verified child handoff"],
        "scope": ["The temporary acceptance repository"],
        "exclusions": ["Automatic human approval"],
        "requirements": [{
            "requirement_id": requirement_id,
            "text": f"Required deliverable {number}",
            "acceptance_criteria": [f"Deliverable {number} is integrated"],
        } for number, requirement_id in enumerate(requirement_ids, 1)],
        "decisions": [], "assumptions": [], "open_questions": [],
    }
    epic = await _post(client, "/api/epics", {
        "schema_version": 1, "project_id": project["id"],
        "title": "Three item chain", "draft": brief,
    })
    epic_id = epic["epic_id"]
    prefix = f"/api/epics/{epic_id}"
    saved = await _post(client, f"{prefix}/brief-revisions", {
        "schema_version": 1, "expected_epic_version": 1, "content": brief,
    })
    await _post(client, f"{prefix}/brief-adoptions", {
        "schema_version": 1, "expected_epic_version": 2,
        "brief_revision_id": saved["brief_revision_id"],
        "brief_digest": saved["content_digest"],
    }, 200)
    item_ids = [str(uuid4()) for _ in range(3)]
    graph = await _post(client, f"{prefix}/graph-revisions", {
        "schema_version": 1, "expected_epic_version": 3,
        "brief_revision_id": saved["brief_revision_id"],
        "brief_digest": saved["content_digest"],
        "items": [{
            "item_id": item_id,
            "disposition": "required", "ordinal": number,
            "title": f"Deliver item {number}",
            "outcome": f"Item {number} is integrated",
            "acceptance_criteria": [f"Deliverable {number} is integrated"],
            "source_requirement_ids": [requirement_ids[number - 1]],
            "dependency_item_ids": item_ids[number - 2:number - 1] if number > 1 else [],
        } for number, item_id in enumerate(item_ids, 1)],
    })
    await _post(client, f"{prefix}/graph-adoptions", {
        "schema_version": 1, "expected_epic_version": 4,
        "graph_revision_id": graph["graph_revision_id"],
        "graph_digest": graph["graph_digest"],
    }, 200)
    execution = await _post(client, f"{prefix}/executions", {
        "schema_version": 1, "expected_epic_version": 5,
    })
    assert execution["brief_revision_id"] == saved["brief_revision_id"]
    assert execution["graph_revision_id"] == graph["graph_revision_id"]
    return epic_id, execution["execution_id"], item_ids, requirement_ids


async def _wait_for_control(client: httpx.AsyncClient, prefix: str, expected: str, harness) -> dict:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        response = await client.get(prefix)
        response.raise_for_status()
        projection = response.json()
        if projection["control_state"] == expected:
            return projection
        assert harness._worker_process.poll() is None, "worker exited during aggregate control"
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"execution did not reach {expected}; last state={projection['control_state']} "
        f"blocker={projection['blocker_code']}"
    )


@pytest.mark.integration
async def test_three_item_dispatch_through_owner_gates_and_verified_git_handoffs(
    migrated_database_url, tmp_path
) -> None:
    repo, bare = tmp_path / "repo", tmp_path / "bare.git"
    base_sha = await _setup_fixture_repo(repo, bare, "example/epic-three-item")
    browser = os.environ.get("FORGE_EPIC_BROWSER_TEST") == "1"
    web_origin = f"http://127.0.0.1:{free_loopback_port()}" if browser else None
    data_root = tmp_path / "data"
    data_root.mkdir()
    with ExitStack() as stack:
        harness = stack.enter_context(EpicProcessHarness(
            database_url=migrated_database_url,
            data_root=data_root,
            prompt_root=Path.cwd() / "agents",
            bare_remote_path=bare,
            web_origin=web_origin,
        ))
        github = harness.fake_github
        _real_bare_merge_with_stale_local_baseline(harness, bare)
        github.bases[("example/epic-three-item", "main")] = base_sha
        github.branch_shas[("example/epic-three-item", "main")] = base_sha
        github.merge_protections[("example/epic-three-item", "main")] = MergeProtection(
            strict_required_checks=True, merge_queue_enabled=False,
            actor_can_bypass=False, evidence_source="classic", verified=True,
            required_check_names=("ci",),
        )
        harness.start_api()
        if browser:
            assert web_origin is not None
            stack.enter_context(_web_server(origin=web_origin, api_origin=harness.base_url))
        headers = {"Origin": web_origin or harness.base_url}
        if web_origin is not None:
            headers["Host"] = web_origin.split("//", 1)[1]
        async with httpx.AsyncClient(
            base_url=harness.base_url, headers=headers, timeout=10,
        ) as client:
            await _bootstrap_session(harness, client)
            epic_id, execution_id, item_ids, requirement_ids = await _accepted_three_item_execution(
                client, repo
            )
            prefix = f"/api/epics/{epic_id}/executions/{execution_id}"
            before = await client.get(prefix)
            assert before.status_code == 200, before.text
            assert [entry["item_id"] for entry in before.json()["items"]] == item_ids
            assert [entry["status"] for entry in before.json()["items"]] == [
                "ready", "blocked", "blocked"
            ]
            assert before.json()["dispatch"]["enabled"] is False
            if browser:
                assert web_origin is not None
                await _browser_probe(
                    database_url=migrated_database_url, origin=web_origin,
                    api_origin=harness.base_url,
                    epic_id=epic_id, execution_id=execution_id, phase="enable",
                )
            else:
                enabled = await client.put(prefix + "/dispatch", json={
                    "schema_version": 1, "expected_dispatch_version": 0,
                    "enabled": True, "profile_id": None, "profile_version": None,
                }, headers={"Idempotency-Key": idempotency_key()})
                assert enabled.status_code == 200, enabled.text
            enabled = await client.get(prefix)
            assert enabled.status_code == 200, enabled.text
            assert enabled.json()["dispatch"]["enabled"] is True
            harness.start_worker()
            harness.assert_process_identities()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                attempts = await client.get(f"/api/epics/{epic_id}/work-item-runs")
                assert attempts.status_code == 200, attempts.text
                if attempts.json():
                    break
                assert harness._worker_process.poll() is None, "worker exited during dispatch"
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("enabled dispatch did not admit the first item")
            assert len(attempts.json()) == 1
            assert attempts.json()[0]["item_id"] == item_ids[0]
            from forge.persistence.database import create_engine, create_session_factory

            engine = create_engine(migrated_database_url)
            try:
                factory = create_session_factory(engine)
                for index, item_id in enumerate(item_ids):
                    if index:
                        deadline = time.monotonic() + 45
                        while time.monotonic() < deadline:
                            attempts = await client.get(f"/api/epics/{epic_id}/work-item-runs")
                            attempts.raise_for_status()
                            if len(attempts.json()) >= index + 1:
                                break
                            await asyncio.sleep(0.1)
                        if len(attempts.json()) != index + 1:
                            observed = await client.get(prefix)
                            observed.raise_for_status()
                            raise AssertionError({
                                "missing_successor": index + 1,
                                "attempt_count": len(attempts.json()),
                                "dispatch": observed.json()["dispatch"],
                                "items": observed.json()["items"],
                                "children": observed.json()["children"],
                                "worker_tail": (data_root / "worker.log").read_text(
                                    encoding="utf-8"
                                )[-700:],
                            })
                    attempt = attempts.json()[index]
                    assert attempt["item_id"] == item_id
                    run_id = UUID(attempt["run_id"])
                    source = await client.get(f"/api/tasks/{attempt['task_id']}")
                    source.raise_for_status()
                    frozen = json.loads(source.json()["body"])
                    assert frozen["item_id"] == item_id
                    assert frozen["requirements"][0]["id"] == requirement_ids[index]
                    assert frozen["requirements"][0]["acceptance_criteria"] == [
                        f"Deliverable {index + 1} is integrated"
                    ]
                    if index:
                        predecessor, = frozen["predecessors"]
                        assert predecessor["status"] == "verified"
                        assert predecessor["predecessor_run_id"] == attempts.json()[index - 1]["run_id"]
                        assert predecessor["handoff_id"]

                    # Dispatch retains each ordinary human gate and the same
                    # run identity across an API/worker restart at plan gate.
                    plan = await harness.wait_for_state(
                        factory, run_id, "AWAITING_PLAN_APPROVAL", timeout=45,
                    )
                    assert plan.pending_gate == "plan" and plan.pending_evidence_digest
                    if index == 0 and browser:
                        assert web_origin is not None
                        await _browser_probe(
                            database_url=migrated_database_url, origin=web_origin,
                            api_origin=harness.base_url,
                            epic_id=epic_id, execution_id=execution_id, phase="plan",
                        )
                    if index == 0:
                        harness.restart_api()
                        harness.restart_worker()
                        retained = await harness.wait_for_state(
                            factory, run_id, "AWAITING_PLAN_APPROVAL", timeout=20,
                        )
                        assert retained.id == plan.id
                    if index == 1 and not browser:
                        from forge.persistence.models import RunCommand
                        from sqlalchemy import select

                        before_control = (await client.get(prefix)).json()
                        historical_child = before_control["children"][0]
                        historical_proof = before_control["items"][0]["completion_evidence"]
                        assert historical_child["run_state"] == "COMPLETED"
                        assert historical_child["effects_settled"] is True
                        assert historical_proof["status"] == "verified"
                        pause = await client.post(
                            prefix + "/commands",
                            json={"expected_execution_version": 1, "action": "pause"},
                            headers={"Idempotency-Key": idempotency_key()},
                        )
                        assert pause.status_code == 202, pause.text
                        assert pause.json()["state"] == "PAUSE_REQUESTED"
                        paused = await _wait_for_control(client, prefix, "PAUSED", harness)
                        assert paused["children"][0] == historical_child
                        assert paused["items"][0]["completion_evidence"] == historical_proof
                        assert paused["children"][1]["attempt"]["run_id"] == str(run_id)
                        assert paused["children"][1]["run_state"] == "PAUSED"
                        assert paused["children"][1]["pending_gate"] is None
                        assert paused["children"][1]["retained_gate"] == "plan"
                        assert len((await client.get(
                            f"/api/epics/{epic_id}/work-item-runs"
                        )).json()) == 2, "pause must fence successor admission"
                        resume = await client.post(
                            prefix + "/commands",
                            json={"expected_execution_version": 2, "action": "resume"},
                            headers={"Idempotency-Key": idempotency_key()},
                        )
                        assert resume.status_code == 202, resume.text
                        assert resume.json()["state"] == "RESUME_REQUESTED"
                        active = await _wait_for_control(client, prefix, "ACTIVE", harness)
                        assert active["children"][0] == historical_child
                        assert active["items"][0]["completion_evidence"] == historical_proof
                        assert active["children"][1]["attempt"]["run_id"] == str(run_id)
                        assert active["children"][1]["pending_gate"] == "plan"
                        assert active["children"][1]["retained_gate"] is None
                        predecessor_run_id = UUID(attempts.json()[0]["run_id"])
                        async with factory() as control_session:
                            commands = (await control_session.scalars(
                                select(RunCommand).where(
                                    RunCommand.run_id.in_((predecessor_run_id, run_id))
                                )
                            )).all()
                        assert not [
                            command for command in commands
                            if command.run_id == predecessor_run_id
                            and command.command_type in {"pause", "resume"}
                        ], "completed predecessor must not receive physical controls"
                        for action in ("pause", "resume"):
                            observed = [
                                command for command in commands
                                if command.run_id == run_id and command.command_type == action
                            ]
                            assert len(observed) == 1, f"live child must receive one {action} command"
                            assert observed[0].status == "COMPLETED", (
                                f"live child {action} command must physically settle"
                            )
                        plan = await harness.wait_for_state(
                            factory, run_id, "AWAITING_PLAN_APPROVAL", timeout=20,
                        )
                        assert plan.pending_gate == "plan" and plan.pending_evidence_digest
                    assert len((await client.get(f"/api/epics/{epic_id}/work-item-runs")).json()) == index + 1
                    await _approve_gate(
                        client, run_id, gate="plan", run_version=plan.version,
                        evidence_digest=plan.pending_evidence_digest,
                    )
                    pr = await harness.wait_for_state(
                        factory, run_id, "AWAITING_PR_APPROVAL", timeout=90,
                    )
                    assert pr.pending_gate == "pr" and pr.pending_evidence_digest
                    await _approve_gate(
                        client, run_id, gate="pr", run_version=pr.version,
                        evidence_digest=pr.pending_evidence_digest,
                    )
                    await _publish_green_check_for_run(
                        harness, factory, run_id, "example/epic-three-item"
                    )
                    merge = await harness.wait_for_state(
                        factory, run_id, "AWAITING_MERGE_APPROVAL", timeout=90,
                    )
                    assert merge.pending_gate == "merge" and merge.pending_evidence_digest
                    await _approve_gate(
                        client, run_id, gate="merge", run_version=merge.version,
                        evidence_digest=merge.pending_evidence_digest,
                    )
                    await harness.wait_for_state(factory, run_id, "COMPLETED", timeout=90)
                    local_before_sync = (await asyncio.to_thread(
                        subprocess.check_output,
                        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
                    )).strip()
                    remote_merged = github.branch_shas[("example/epic-three-item", "main")]
                    assert local_before_sync != remote_merged, (
                        "the fake remote merge must not silently advance the project baseline"
                    )
                    before_sync = await client.get(prefix)
                    before_sync.raise_for_status()
                    assert before_sync.json()["items"][index]["status"] != "verified"
                    assert before_sync.json()["items"][index]["completion_evidence"]["blocker_code"] == (
                        "predecessor_integration_unverified"
                    )
                    assert len((await client.get(f"/api/epics/{epic_id}/work-item-runs")).json()) == index + 1
                    merged_sha = _owner_sync_local_baseline(repo, bare)
                    assert merged_sha == remote_merged
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        integrated = await client.get(prefix)
                        integrated.raise_for_status()
                        proof = integrated.json()["items"][index]["completion_evidence"]
                        if proof["status"] == "verified":
                            break
                        await asyncio.sleep(0.1)
                    if proof["status"] != "verified":
                        from forge.persistence.models.execution import OperationIntent, RunEvent
                        from forge.persistence.models.release import PullRequest
                        from forge.persistence.repositories.runs import PostgresRunRepository
                        from sqlalchemy import select

                        async with factory() as diagnostic_session:
                            pull = await diagnostic_session.scalar(
                                select(PullRequest).where(PullRequest.run_id == run_id)
                            )
                            intent = (
                                await diagnostic_session.get(OperationIntent, pull.merge_intent_id)
                                if pull and pull.merge_intent_id else None
                            )
                            events = (await diagnostic_session.scalars(
                                select(RunEvent).where(
                                    RunEvent.run_id == run_id,
                                    RunEvent.event_type == "run.merge_completed",
                                )
                            )).all()
                            quiescence = await PostgresRunRepository(
                                diagnostic_session
                            ).prove_quiescent(run_id)
                        raise AssertionError({
                            "proof": proof,
                            "pull": None if pull is None else {
                                "state": pull.state, "base_ref": pull.base_ref,
                                "merge_sha": pull.merge_sha,
                                "merge_intent_id": str(pull.merge_intent_id),
                            },
                            "intent": None if intent is None else {
                                "kind": intent.operation_kind, "status": intent.status,
                                "outcome": intent.outcome_payload,
                            },
                            "merge_events": [
                                {"actor": event.actor_class, "payload": event.payload,
                                 "version": event.run_version} for event in events
                            ],
                            "quiescence": str(quiescence),
                            "local_sha": merged_sha,
                        })
                    assert proof["integrated_sha"] == merged_sha
                    assert proof["predecessor_run_id"] == str(run_id)
                    assert proof["handoff_id"]
                    criterion_file = repo / f"item-{index + 1}-criterion.txt"
                    criterion = criterion_file.read_text(encoding="utf-8")
                    assert requirement_ids[index] in criterion
                    assert f"Deliverable {index + 1} is integrated" in criterion
                    assert item_id in criterion
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    final = await client.get(prefix)
                    final.raise_for_status()
                    if final.json()["control_state"] == "SUCCEEDED":
                        break
                    await asyncio.sleep(0.1)
                if final.json()["control_state"] != "SUCCEEDED":
                    from datetime import UTC, datetime, timedelta

                    from forge.application.services.epic_dispatch_worker import EpicDispatchWorker
                    from forge.application.services.epic_eligibility import EpicEligibilityService
                    from forge.persistence.models.epic_dispatch import EpicDispatchSetting
                    from forge.persistence.models.epic_run_bridge import EpicChildBudgetHold
                    from sqlalchemy import select

                    harness.stop_worker()
                    async with factory() as diagnostic_session:
                        setting = await diagnostic_session.get(EpicDispatchSetting, UUID(execution_id))
                        holds = (await diagnostic_session.scalars(
                            select(EpicChildBudgetHold).where(EpicChildBudgetHold.epic_id == UUID(epic_id))
                        )).all()
                        before = {
                            "checked_at": setting.checked_at.isoformat() if setting and setting.checked_at else None,
                            "enabled": setting.enabled if setting else None,
                            "blocker_code": setting.blocker_code if setting else None,
                            "claim_item_id": str(setting.claim_item_id) if setting and setting.claim_item_id else None,
                            "claim_expires_at": setting.claim_expires_at.isoformat() if setting and setting.claim_expires_at else None,
                            "holds": [{
                                "attempt_id": str(hold.attempt_id),
                                "run_id": str(hold.run_id),
                                "effects_settled": hold.effects_settled,
                                "checked_at": hold.checked_at.isoformat() if hold.checked_at else None,
                            } for hold in holds],
                        }
                    assert setting is not None and setting.enabled, before
                    if setting.checked_at is not None:
                        due_at = setting.checked_at + timedelta(seconds=1)
                        await asyncio.sleep(max(0.0, (due_at - datetime.now(UTC)).total_seconds()) + 0.2)
                    warnings = [
                        line for line in (data_root / "worker.log").read_text(encoding="utf-8").splitlines()
                        if "Epic dispatch polling failed" in line
                    ][-10:]
                    safe_warnings = Redactor(secrets=(migrated_database_url,)).redact("\n".join(warnings))
                    work = lambda: PostgresUnitOfWork(factory)
                    diagnostic = EpicDispatchWorker(
                        work, bridge=None,
                        eligibility=EpicEligibilityService(work, data_root=str(data_root)),
                    )
                    try:
                        direct_result = await diagnostic.run_once()
                    except Exception as error:  # noqa: BLE001 - diagnostic on failed terminal state
                        safe_traceback = Redactor(
                            secrets=(migrated_database_url,)
                        ).redact(traceback.format_exc())
                        raise AssertionError(
                            f"terminal dispatch pre-state {before}; "
                            f"worker warnings {safe_warnings}; "
                            f"direct exception {type(error).__name__}: "
                            f"{str(safe_traceback)[-4000:]}"
                        ) from None
                    after = (await client.get(prefix)).json()
                    raise AssertionError({
                        "before": before,
                        "worker_warnings": safe_warnings,
                        "direct_result": str(direct_result),
                        "after_control": after["control_state"],
                        "after_dispatch": after["dispatch"],
                        "after_items": [{
                            "status": item["status"], "blocker_code": item["blocker_code"],
                        } for item in after["items"]],
                        "worker_exit": harness._worker_process.returncode,
                        "api_pid": harness.api_pid,
                    })
                assert final.json()["control_state"] == "SUCCEEDED", final.text
                assert [item["status"] for item in final.json()["items"]] == [
                    "verified", "verified", "verified"
                ]
                assert len(final.json()["children"]) == 3
                assert all(child["effects_settled"] for child in final.json()["children"])
                if browser:
                    assert web_origin is not None
                    await _browser_probe(
                        database_url=migrated_database_url, origin=web_origin,
                        api_origin=harness.base_url,
                        epic_id=epic_id, execution_id=execution_id, phase="finished",
                    )
            finally:
                await engine.dispose()
    assert all(process.poll() is not None for process in harness._processes)
