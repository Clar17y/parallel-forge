"""Opt-in A8 browser acceptance against public, keyless Forge processes."""

import asyncio
import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from alembic import command
from forge.evaluations.credentials import assert_credential_free

from scripts.dev import DefaultCommandRunner
from tests.acceptance.process_harness import ForgeProcessHarness, free_loopback_port
from tests.acceptance.test_subscription_operator_process import _retain_evidence
from tests.acceptance.test_subscription_profile_process import _repository, _stored

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("FORGE_SUBSCRIPTION_BROWSER_TEST") != "1",
        reason="requires explicit local browser acceptance setup (Node 24 and Playwright Chromium)",
    ),
]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


def _wait_web(process, origin):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        assert process.poll() is None, "Next.js exited before browser acceptance"
        try:
            if httpx.get(origin, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError("Next.js did not become ready")


def test_subscription_profiles_and_controls_across_browser_tabs(
    test_database_url, alembic_config_factory, tmp_path
):
    command.upgrade(alembic_config_factory(test_database_url), "head")
    root = Path.cwd()
    web_root = root / "apps" / "web"
    node = shutil.which("node")
    assert node is not None, "Node 24 is required"
    runner = DefaultCommandRunner()
    version = runner.run([node, "--version"], timeout=5)
    assert version.returncode == 0 and version.stdout.startswith("v24.")
    next_cli = runner.run(
        [node, "-p", "require.resolve('next/dist/bin/next')"], cwd=web_root, timeout=5
    )
    assert next_cli.returncode == 0, "install locked web dependencies before acceptance"
    web_origin = f"http://127.0.0.1:{free_loopback_port()}"
    data_root, repository = tmp_path / "data", tmp_path / "repository"
    data_root.mkdir()
    _repository(repository)
    # Next reads its cwd's dotenv files. Do not import local operator configuration.
    assert not list(web_root.glob(".env*")), "browser acceptance needs a web cwd without dotenv"
    with ForgeProcessHarness(
        database_url=test_database_url,
        data_root=data_root,
        prompt_root=root / "agents",
        web_origin=web_origin,
        subscription_only=True,
    ) as harness:
        harness.start_api()
        harness.start_worker()

        class Control(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/configuration":
                    self.reply(
                        {
                            "bootstrap": harness.bootstrap_token(),
                            "repository": str(repository),
                        }
                    )
                elif self.path == "/snapshot":
                    self.reply(asyncio.run(_stored(test_database_url)))
                else:
                    self.reply({}, status=404)

            def do_POST(self):
                if self.path == "/worker/stop":
                    harness.stop_worker()
                elif self.path == "/worker/restart":
                    harness.restart_worker()
                elif self.path == "/api/restart":
                    harness.restart_api()
                else:
                    self.reply({}, status=404)
                    return
                self.reply({"ok": True})

            def reply(self, body, *, status=200):
                content = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Control)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        # The browser uses its own temporary profile. No provider environment,
        # database URL or session credential is passed to the Node processes.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "PATH",
                "PATHEXT",
                "SYSTEMROOT",
                "WINDIR",
                "TEMP",
                "TMP",
                "HOME",
                "USERPROFILE",
                "LOCALAPPDATA",
                "APPDATA",
                "PROGRAMFILES",
                "PROGRAMFILES(X86)",
            }
        } | {
            "FORGE_WEB_ORIGIN": web_origin,
            "FORGE_API_INTERNAL_ORIGIN": harness.base_url,
            "FORGE_E2E_WEB_ORIGIN": web_origin,
            "FORGE_E2E_CONTROL_ORIGIN": f"http://127.0.0.1:{server.server_port}",
            "NEXT_TELEMETRY_DISABLED": "1",
        }
        web_process = None
        next_types_path = web_root / "next-env.d.ts"
        next_types_before = next_types_path.read_bytes()
        try:
            web_process = runner.spawn(
                "subscription-browser-web",
                [
                    node,
                    next_cli.stdout.strip(),
                    "dev",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    web_origin.rsplit(":", 1)[1],
                ],
                cwd=web_root,
                env=environment,
            )
            _wait_web(web_process, web_origin)
            result = runner.run(
                [node, str(root / "tests" / "acceptance" / "subscription_browser.mjs")],
                cwd=web_root,
                env=environment,
                timeout=210,
            )
            assert_credential_free(result.stdout + result.stderr)
            assert result.returncode == 0, result.stderr[-4000:]
            evidence = json.loads(result.stdout)
            assert evidence["provider_calls"] is False
            settled = asyncio.run(_stored(test_database_url))
            assert evidence["after_resume"] == settled
        finally:
            try:
                if web_process is not None:
                    web_process.kill_tree()
                    deadline = time.monotonic() + 5
                    while web_process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    assert web_process.poll() is not None, "Next.js termination is unconfirmed"
                # Next dev regenerates these two type paths. Restore only that
                # recognized change after its writer stops; preserve unexpected edits.
                current_types = next_types_path.read_bytes()
                if current_types != next_types_before:
                    expected = next_types_before.replace(b"\r\n", b"\n").replace(
                        b'"./.next/types/', b'"./.next/dev/types/'
                    )
                    assert current_types.replace(b"\r\n", b"\n") == expected
                    next_types_path.write_bytes(next_types_before)
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)
                assert not server_thread.is_alive()
    assert all(process.poll() is not None for process in harness._processes)
    assert asyncio.run(_stored(test_database_url)) == settled
    evidence["terminal_public_processes"] = [
        {"pid": process.pid, "return_code": process.returncode} for process in harness._processes
    ]
    evidence["terminal_web_process"] = {"pid": web_process.pid, "return_code": web_process.poll()}
    _retain_evidence(evidence)
