"""Hosted-only smoke acceptance for the real development supervisor."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
from forge.tools.secrets import LocalSecretStore


def _redact(text: str) -> str:
    return re.sub(r"(bootstrap=)[A-Za-z0-9._~+/-]+", r"\1[REDACTED]", text)


def _descendants(pid: int) -> set[int]:
    """Return the current Linux process descendants without shell expansion."""
    rows = subprocess.run(
        ["ps", "-eo", "pid=,ppid="], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    children: dict[int, set[int]] = {}
    for row in rows:
        fields = row.split()
        if len(fields) == 2:
            children.setdefault(int(fields[1]), set()).add(int(fields[0]))
    result: set[int] = set()
    pending = list(children.get(pid, ()))
    while pending:
        child = pending.pop()
        if child not in result:
            result.add(child)
            pending.extend(children.get(child, ()))
    return result


def _wait_for_ready(process: subprocess.Popen[str], log_path: Path, timeout: float = 600) -> None:
    deadline = time.monotonic() + timeout
    missing_steps: list[str] = []
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = _redact(log_path.read_text(encoding="utf-8", errors="replace")[-6000:])
            raise AssertionError(
                f"development supervisor exited early ({process.returncode}): {output}"
            )
        try:
            api = httpx.get("http://127.0.0.1:8000/api/health", timeout=0.5)
            web = httpx.get("http://127.0.0.1:3000", timeout=0.5)
            output = log_path.read_text(encoding="utf-8", errors="replace")
            required_steps = (
                "Syncing Python dependencies with uv sync --frozen --extra dev",
                "Installing Node dependencies with npm ci",
                "Applying database migrations with Alembic",
                "Runner image built with immutable ID: sha256:",
                "Forge Operator Bootstrap URL:",
                "Forge worker recovered and is polling",
            )
            missing_steps = [step for step in required_steps if step not in output]
            if (
                api.json() == {"status": "ok", "role": "api"}
                and web.status_code == 200
                and not missing_steps
            ):
                return
        except httpx.HTTPError, ValueError:
            pass
        time.sleep(0.25)
    output = _redact(log_path.read_text(encoding="utf-8", errors="replace")[-6000:])
    raise AssertionError(f"development supervisor did not become ready; missing steps={missing_steps}: {output}")


def main() -> None:
    """Run only in hosted Linux CI; it starts no task or external provider call."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or sys.platform != "linux":
        raise SystemExit("supervisor smoke is hosted Linux CI only")
    root = Path(__file__).resolve().parents[2]
    scratch = Path(os.environ["RUNNER_TEMP"]) / "forge-supervisor-smoke"
    scratch.mkdir(exist_ok=True)
    data_root = scratch / "data"
    data_root.mkdir(exist_ok=True)
    catalog = scratch / "pricing.json"
    catalog.write_text(
        json.dumps(
            {
                "version": "supervisor-smoke",
                "entries": {
                    "google:gemini-2.5-pro": {
                        "input_per_million": "1",
                        "output_per_million": "1",
                        "cached_input_per_million": "1",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    LocalSecretStore(data_root).create("supervisor-provider", b"hosted-smoke-only")
    environment = {
        **os.environ,
        "FORGE_DATABASE_URL": "postgresql+asyncpg://forge:forge@127.0.0.1:5435/forge_supervisor_smoke",
        "FORGE_DATA_ROOT": str(data_root),
        "FORGE_PROMPT_ROOT": str(root / "agents"),
        "FORGE_PROVIDER_SECRET_REFERENCE": "secret://forge/supervisor-provider",
        "FORGE_PRICING_CATALOG_PATH": str(catalog),
        "FORGE_API_PORT": "8000",
        "FORGE_WEB_ORIGIN": "http://127.0.0.1:3000",
        "FORGE_API_INTERNAL_ORIGIN": "http://127.0.0.1:8000",
        "NEXT_TELEMETRY_DISABLED": "1",
        "PYTHONUNBUFFERED": "1",
    }
    log_path = scratch / "supervisor.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "scripts/dev.py"],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        descendants: set[int] = set()
        try:
            _wait_for_ready(process, log_path)
            descendants = _descendants(process.pid)
            assert len(descendants) >= 3, "supervisor did not own API, worker, and web children"
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=30) == 0
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and any(
                Path(f"/proc/{pid}").exists() for pid in descendants
            ):
                time.sleep(0.1)
            assert not any(Path(f"/proc/{pid}").exists() for pid in descendants), (
                "supervisor left owned child processes running"
            )
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    # Discover ownership while the supervisor is still alive;
                    # avoid leaving its children behind on a readiness failure.
                    for child in _descendants(process.pid):
                        try:
                            os.kill(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    process.kill()
                    process.wait(timeout=10)
            # Diagnostics are redacted before assertion and the raw supervisor
            # output (which includes the one-time bootstrap URL) never survives.
            log_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
