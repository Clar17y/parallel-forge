"""Loopback-only control bridge for hosted browser acceptance."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import httpx
from forge.application.services.auth import AuthService
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.domain.teardown import teardown_confirmation
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.models import Project, Run, RunCommand, Task
from forge.persistence.repositories.runs import _snapshot_from_record
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.tools.secrets import LocalSecretStore, SecretAlreadyExistsError
from sqlalchemy import select

from tests.acceptance.process_harness import ForgeProcessHarness, idempotency_key


class Bridge:
    """Test-only adapter between hosted Playwright and real Forge processes."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="forge-browser-acceptance-"))
        self.github_repository = f"example/browser-acceptance-{uuid4().hex[:10]}"
        self.repository = self.root / "repository"
        self.bare = self.root / "remote.git"
        self._create_repository()
        data_root = self.root / "data"
        data_root.mkdir()
        self.harness = ForgeProcessHarness(
            database_url=os.environ["FORGE_DATABASE_URL"],
            data_root=data_root,
            prompt_root=Path.cwd() / "agents",
            bare_remote_path=self.bare,
            api_port=(
                int(os.environ["FORGE_E2E_BRIDGE_API_PORT"])
                if "FORGE_E2E_BRIDGE_API_PORT" in os.environ
                else None
            ),
            web_origin=os.environ.get("FORGE_E2E_WEB_ORIGIN", "http://127.0.0.1:3000"),
        )
        self._configure_github(self.github_repository)
        self.harness.start_api()
        self.harness.start_worker()
        web_origin = self.harness._env["FORGE_WEB_ORIGIN"]
        bootstrap_client = httpx.Client(
            base_url=self.harness.base_url,
            headers={
                "Origin": web_origin,
                "Host": web_origin.split("//", 1)[1],
            },
        )
        response = bootstrap_client.post(
            "/api/auth/bootstrap",
            json={"token": self._issue_browser_bootstrap()},
            headers={"Idempotency-Key": idempotency_key()},
        )
        response.raise_for_status()
        # The bridge connects to the API port while asserting the configured
        # web origin.  Preserve the API's HttpOnly cookie explicitly because
        # httpx keys its jar to the connection host rather than the supplied
        # Host header.
        self._client = httpx.Client(
            base_url=self.harness.base_url,
            headers={
                "Origin": web_origin,
                "Host": web_origin.split("//", 1)[1],
                "Cookie": response.headers["set-cookie"].split(";", 1)[0],
                "X-CSRF-Token": response.json()["csrf_token"],
            },
        )
        bootstrap_client.close()
        self._auth_headers = dict(self._client.headers)
        session = self._client.get("/api/auth/session")
        if session.status_code != 200:
            raise RuntimeError(f"bridge session bootstrap failed ({session.status_code})")

    def _mutation_headers(self) -> dict[str, str]:
        return {**self._auth_headers, "Idempotency-Key": idempotency_key()}

    def _issue_browser_bootstrap(self) -> str:
        """Issue a one-time browser token without revoking the control session."""

        async def issue() -> str:
            engine = create_engine(os.environ["FORGE_DATABASE_URL"])
            try:
                factory = create_session_factory(engine)
                return await AuthService(lambda: PostgresUnitOfWork(factory)).issue_bootstrap()
            finally:
                await engine.dispose()

        return asyncio.run(issue())

    def exchange_browser_bootstrap(self, token: str) -> httpx.Response:
        """Exchange an opaque scenario token through the real browser auth route."""
        web_origin = self.harness._env["FORGE_WEB_ORIGIN"]
        with httpx.Client(
            base_url=self.harness.base_url,
            headers={"Origin": web_origin, "Host": web_origin.split("//", 1)[1]},
        ) as client:
            return client.post(
                "/api/auth/bootstrap",
                json={"token": token},
                headers={"Idempotency-Key": idempotency_key()},
            )

    def _create_repository(self) -> None:
        self.repository.mkdir()
        (self.repository / "README.md").write_text("Acceptance fixture\n", encoding="utf-8")
        (self.repository / "check_readme.py").write_text(
            "from pathlib import Path\nassert Path('README.md').exists()\n", encoding="utf-8"
        )
        (self.repository / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
        for command in (
            ("init", "-b", "main"),
            ("config", "user.email", "acceptance@example.invalid"),
            ("config", "user.name", "Forge Acceptance"),
            ("add", "."),
            ("commit", "-m", "initial"),
        ):
            subprocess.run(["git", *command], cwd=self.repository, check=True, capture_output=True)
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.bare)], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "push", str(self.bare), "main"],
            cwd=self.repository,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", f"https://github.com/{self.github_repository}.git"],
            cwd=self.repository,
            check=True,
        )

    def _configure_github(self, repository: str) -> None:
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.harness.fake_github.bases[(repository, "main")] = base_sha
        self.harness.fake_github.branch_shas[(repository, "main")] = base_sha
        self.harness.fake_github.merge_protections[(repository, "main")] = MergeProtection(
            strict_required_checks=True,
            merge_queue_enabled=False,
            actor_can_bypass=False,
            evidence_source="classic",
            verified=True,
            required_check_names=("ci",),
        )

    def browser_scenario(self) -> dict[str, str]:
        return {
            "controlOrigin": "http://127.0.0.1:8765",
            "bootstrapToken": self._issue_browser_bootstrap(),
            "uiRepositoryPath": str(self.repository),
            "uiGithubRepository": self.github_repository,
        }

    def _create_run(self, *, repository: str, database: bool) -> str:
        fixture_repository = self.root / f"repository-{uuid4().hex[:8]}"
        subprocess.run(
            ["git", "clone", str(self.repository), str(fixture_repository)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "set-url", "origin", f"https://github.com/{repository}.git"],
            cwd=fixture_repository,
            check=True,
        )
        self._configure_github(repository)
        if database:
            try:
                LocalSecretStore(self.root / "data").create(
                    "acceptance-db-admin", os.environ["FORGE_DATABASE_URL"].encode()
                )
            except SecretAlreadyExistsError:
                pass
        project_body: dict[str, object] = {
            "name": f"Browser fixture {uuid4().hex[:8]}",
            "repository_path": str(fixture_repository),
            "github_repository": repository,
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
        if database:
            project_body["database"] = {
                "enabled": True,
                "admin_url_secret_reference": "secret://environment/FORGE_ACCEPTANCE_DB_ADMIN",
                "injected_environment_key": "DATABASE_URL",
            }
        project = self._client.post(
            "/api/projects", json=project_body, headers=self._mutation_headers()
        )
        if project.status_code >= 400:
            raise RuntimeError(
                f"project setup failed ({project.status_code}): sent_cookie={project.request.headers.get('cookie')!r}; {project.text}"
            )
        task = self._client.post(
            "/api/tasks",
            json={
                "project_id": project.json()["id"],
                "title": "Update README",
                "body": "Acceptance task",
            },
            headers=self._mutation_headers(),
        )
        task.raise_for_status()
        run = self._client.post(
            "/api/runs", json={"task_id": task.json()["id"]}, headers=self._mutation_headers()
        )
        run.raise_for_status()
        return run.json()["id"]

    def restart_scenario(self) -> dict[str, str]:
        return {
            "runId": self._create_run(
                repository=f"example/restart-{uuid4().hex[:10]}", database=True
            ),
            "bootstrapToken": self._issue_browser_bootstrap(),
        }

    def _read_run(self, run_id: UUID) -> dict[str, object] | None:
        async def read() -> dict[str, object] | None:
            engine = create_engine(os.environ["FORGE_DATABASE_URL"])
            try:
                async with create_session_factory(engine)() as session:
                    run = await session.get(Run, run_id)
                    if run is None:
                        return None
                    project = await session.execute(
                        select(Project.github_repository, Project.canonical_path)
                        .join(Task)
                        .where(Task.id == run.task_id)
                    )
                    github_repository, repository_path = project.one()
                    return {
                        "state": run.state,
                        "version": run.version,
                        "evidenceDigest": run.pending_evidence_digest or "",
                        "worktree_path": run.worktree_path,
                        "branch_name": run.branch_name,
                        "database_state": run.database_state,
                        "github_repository": github_repository,
                        "repository_path": repository_path,
                        "snapshot": _snapshot_from_record(run),
                    }
            finally:
                await engine.dispose()

        return asyncio.run(read())

    def _expedite(self, run_id: UUID) -> None:
        async def expedite() -> None:
            engine = create_engine(os.environ["FORGE_DATABASE_URL"])
            try:
                await self.harness.expedite_commands(create_session_factory(engine), run_id)
            finally:
                await engine.dispose()

        asyncio.run(expedite())

    def _progress_remote_ci(self, run_id: UUID) -> None:
        observed = self._read_run(run_id)
        if observed is None or observed["state"] != "MONITORING_PR":
            return
        repository = str(observed["github_repository"])
        pr = self.harness.fake_github.pull_requests.get((repository, 1))
        if pr is None or (repository, pr.head_sha) in self.harness.fake_github.checks:
            return
        self.harness.fake_github.checks[(repository, pr.head_sha)] = [
            CheckSnapshot(
                name="ci",
                status="completed",
                conclusion="success",
                head_sha=pr.head_sha,
                summary="Browser acceptance CI green",
            )
        ]
        self._expedite(run_id)

    def register_run(self, run_id: str) -> dict[str, str]:
        observed = self._read_run(UUID(run_id))
        if observed is None or observed["github_repository"] != self.github_repository:
            raise RuntimeError("browser run is not bound to the bridge repository")
        return {"runId": run_id}

    def approve(self, run_id: str, gate: str) -> None:
        observed = self._read_run(UUID(run_id))
        if observed is None or not observed["evidenceDigest"]:
            raise RuntimeError(f"{gate} approval lacks current evidence")
        body = {
            "gate": gate,
            "run_version": observed["version"],
            "evidence_digest": observed["evidenceDigest"],
        }
        challenge = self._client.post(
            f"/api/runs/{run_id}/approval-challenges", json=body, headers=self._mutation_headers()
        )
        challenge.raise_for_status()
        approval = self._client.post(
            f"/api/runs/{run_id}/approvals",
            json={**body, "challenge_token": challenge.json()["token"]},
            headers=self._mutation_headers(),
        )
        approval.raise_for_status()
        self._expedite(UUID(run_id))

    def enqueue_cancel(self, run_id: str) -> None:
        observed = self._read_run(UUID(run_id))
        if observed is None:
            raise RuntimeError(f"unknown browser run {run_id}")
        response = self._client.post(
            f"/api/runs/{run_id}/commands",
            json={"command_type": "cancel", "expected_run_version": observed["version"]},
            headers=self._mutation_headers(),
        )
        response.raise_for_status()

    def cancel_command(self, run_id: str) -> dict[str, str]:
        async def read() -> dict[str, str]:
            engine = create_engine(os.environ["FORGE_DATABASE_URL"])
            try:
                async with create_session_factory(engine)() as session:
                    command = await session.scalar(
                        select(RunCommand)
                        .where(
                            RunCommand.run_id == UUID(run_id), RunCommand.command_type == "cancel"
                        )
                        .order_by(RunCommand.created_at.desc())
                    )
                    return {"status": command.status if command else "MISSING"}
            finally:
                await engine.dispose()

        return asyncio.run(read())

    def teardown(self, run_id: str) -> None:
        observed = self._read_run(UUID(run_id))
        if observed is None:
            raise RuntimeError(f"unknown browser run {run_id}")
        response = self._client.post(
            f"/api/runs/{run_id}/commands",
            json={
                "command_type": "teardown_run_resources",
                "expected_run_version": observed["version"],
                "confirm_resource_identity": teardown_confirmation(observed["snapshot"]),
                "delete_branch": False,
            },
            headers=self._mutation_headers(),
        )
        response.raise_for_status()
        self._expedite(UUID(run_id))

    def state(self, desired: str, run_id: str) -> dict[str, str]:
        target = UUID(run_id)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            self._progress_remote_ci(target)
            observed = self._read_run(target)
            if observed is not None and observed["state"] == desired:
                return {
                    "state": str(observed["state"]),
                    "evidenceDigest": str(observed["evidenceDigest"]),
                }
            time.sleep(0.2)
        observed = self._read_run(target)
        raise RuntimeError(
            f"run did not reach {desired}: {None if observed is None else observed['state']}"
        )

    def stop_worker(self) -> None:
        self.harness.stop_worker()


class Handler(BaseHTTPRequestHandler):
    bridge: Bridge

    def do_POST(self) -> None:
        try:
            if self.path == "/scenario/forge":
                result = self.bridge.browser_scenario()
            elif self.path == "/scenario/restart-cancel":
                result = self.bridge.restart_scenario()
            elif self.path == "/worker/restart":
                old = self.bridge.harness.worker_pid
                result = {"oldPid": old, "newPid": self.bridge.harness.restart_worker()}
            elif self.path.startswith("/runs/") and self.path.endswith("/expedite"):
                self.bridge._expedite(UUID(self.path.split("/")[2]))
                result = {"ok": True}
            elif self.path == "/worker/stop":
                self.bridge.stop_worker()
                result = {"ok": True}
            elif self.path.startswith("/runs/") and self.path.endswith("/register"):
                result = self.bridge.register_run(self.path.split("/")[2])
            elif self.path.startswith("/runs/") and self.path.endswith("/cancel"):
                self.bridge.enqueue_cancel(self.path.split("/")[2])
                result = {"ok": True}
            elif self.path.startswith("/runs/") and self.path.endswith("/teardown"):
                self.bridge.teardown(self.path.split("/")[2])
                result = {"ok": True}
            else:
                self._json({"error": "unknown control path"}, 404)
                return
            self._json(result)
        except Exception as error:  # noqa: BLE001 - return bounded control errors to hosted tests
            self._json({"error": str(error)}, 500)

    def do_GET(self) -> None:
        try:
            if self.path == "/health":
                self._json({"status": "ok"})
                return
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path.startswith("/runs/") and parsed.path.endswith("/cancel-command"):
                self._json(self.bridge.cancel_command(parsed.path.split("/")[2]))
                return
            self._json(self.bridge.state(query["state"][0], query["runId"][0]))
        except Exception as error:  # noqa: BLE001 - return bounded control errors to hosted tests
            self._json({"error": str(error)}, 500)

    def _json(self, value: object, status: int = 200) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


def main() -> None:
    bridge = Bridge()
    Handler.bridge = bridge
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    try:
        server.serve_forever()
    finally:
        bridge.harness.close()


if __name__ == "__main__":
    main()
