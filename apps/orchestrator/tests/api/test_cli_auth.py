"""CLI authentication output contract."""

from __future__ import annotations

import pytest
from forge.cli import main as cli_main
from forge.cli.main import app
from typer.testing import CliRunner


def test_operator_rotate_prints_only_bootstrap_fragment(monkeypatch) -> None:
    async def fake_rotate(_settings) -> str:
        return "bootstrap-raw"

    monkeypatch.setattr(cli_main, "_rotate", fake_rotate)
    result = CliRunner().invoke(app, ["operator", "rotate"])

    assert result.exit_code == 0
    assert result.stdout == "http://127.0.0.1:3000/#bootstrap=bootstrap-raw\n"
    assert "session" not in result.stdout
    assert "csrf" not in result.stdout
    assert "challenge" not in result.stdout


def test_operator_open_prints_only_bootstrap_fragment_with_print_url(monkeypatch) -> None:
    async def fake_issue_bootstrap(_settings) -> str:
        return "bootstrap-raw"

    monkeypatch.setattr(cli_main, "_issue_bootstrap", fake_issue_bootstrap)
    result = CliRunner().invoke(app, ["operator", "open", "--print-url", "--no-wait"])

    assert result.exit_code == 0
    assert result.stdout == "http://127.0.0.1:3000/#bootstrap=bootstrap-raw\n"
    assert "session" not in result.stdout
    assert "csrf" not in result.stdout
    assert "challenge" not in result.stdout


def test_operator_open_headless_alias(monkeypatch) -> None:
    async def fake_issue_bootstrap(_settings) -> str:
        return "bootstrap-raw"

    monkeypatch.setattr(cli_main, "_issue_bootstrap", fake_issue_bootstrap)
    result = CliRunner().invoke(app, ["operator", "open", "--headless", "--no-wait"])

    assert result.exit_code == 0
    assert result.stdout == "http://127.0.0.1:3000/#bootstrap=bootstrap-raw\n"


def test_operator_open_successful_browser_open(monkeypatch) -> None:
    opened_urls: list[str] = []

    async def fake_issue_bootstrap(_settings) -> str:
        return "bootstrap-test-token"

    def fake_wait(_settings, timeout=30.0) -> None:
        pass

    def fake_open(url: str, new: int = 0) -> bool:
        opened_urls.append(url)
        return True

    monkeypatch.setattr(cli_main, "_issue_bootstrap", fake_issue_bootstrap)
    monkeypatch.setattr(cli_main, "wait_for_dashboard", fake_wait)
    monkeypatch.setattr(cli_main.webbrowser, "open", fake_open)

    result = CliRunner().invoke(app, ["operator", "open"])

    assert result.exit_code == 0
    assert (
        "Opened Forge with a fresh sign-in link. Existing sessions remain valid." in result.stdout
    )
    assert opened_urls == ["http://127.0.0.1:3000/#bootstrap=bootstrap-test-token"]


def test_operator_open_browser_failure(monkeypatch) -> None:
    async def fake_issue_bootstrap(_settings) -> str:
        return "bootstrap-test-token"

    def fake_wait(_settings, timeout=30.0) -> None:
        pass

    def fake_open(_url: str, new: int = 0) -> bool:
        return False

    monkeypatch.setattr(cli_main, "_issue_bootstrap", fake_issue_bootstrap)
    monkeypatch.setattr(cli_main, "wait_for_dashboard", fake_wait)
    monkeypatch.setattr(cli_main.webbrowser, "open", fake_open)

    result = CliRunner().invoke(app, ["operator", "open"])

    assert result.exit_code == 1
    assert (
        "The browser could not open. Run this helper with --print-url to get a five-minute sign-in link."
        in result.stderr
    )


def test_operator_open_browser_exception(monkeypatch) -> None:
    async def fake_issue_bootstrap(_settings) -> str:
        return "bootstrap-test-token"

    def fake_wait(_settings, timeout=30.0) -> None:
        pass

    def fake_open(_url: str, new: int = 0) -> bool:
        raise OSError("failed to invoke browser")

    monkeypatch.setattr(cli_main, "_issue_bootstrap", fake_issue_bootstrap)
    monkeypatch.setattr(cli_main, "wait_for_dashboard", fake_wait)
    monkeypatch.setattr(cli_main.webbrowser, "open", fake_open)

    result = CliRunner().invoke(app, ["operator", "open"])

    assert result.exit_code == 1
    assert (
        "The browser could not open. Run this helper with --print-url to get a five-minute sign-in link."
        in result.stderr
    )


def test_operator_open_unavailable_dashboard(monkeypatch) -> None:
    def fake_wait(_settings, timeout=30.0) -> None:
        raise TimeoutError("Forge dashboard is not ready")

    monkeypatch.setattr(cli_main, "wait_for_dashboard", fake_wait)

    result = CliRunner().invoke(app, ["operator", "open"])

    assert result.exit_code == 1
    assert (
        "Forge is not ready. Start its services and check the local service logs." in result.stderr
    )
    assert "TimeoutError" not in result.stderr


def test_operator_open_database_failure_sanitizes_output(monkeypatch) -> None:
    async def fake_issue_bootstrap(_settings) -> str:
        raise RuntimeError("postgresql+asyncpg://leakuser:secretpwd@127.0.0.1:5432/leakdb failed")

    monkeypatch.setattr(cli_main, "_issue_bootstrap", fake_issue_bootstrap)

    result = CliRunner().invoke(app, ["operator", "open", "--no-wait"])

    assert result.exit_code == 1
    assert (
        "Could not create the local sign-in link. Check the Forge database connection."
        in result.stderr
    )
    assert "secretpwd" not in result.stderr
    assert "leakuser" not in result.stderr
    assert "postgresql" not in result.stderr


def test_operator_rotate_database_failure_sanitizes_output(monkeypatch) -> None:
    async def fake_rotate(_settings) -> str:
        raise RuntimeError("postgresql+asyncpg://leakuser:secretpwd@127.0.0.1:5432/leakdb failed")

    monkeypatch.setattr(cli_main, "_rotate", fake_rotate)

    result = CliRunner().invoke(app, ["operator", "rotate"])

    assert result.exit_code == 1
    assert (
        "Could not rotate operator credentials. Check the Forge database connection."
        in result.stderr
    )
    assert "secretpwd" not in result.stderr
    assert "leakuser" not in result.stderr
    assert "postgresql" not in result.stderr


def test_operator_open_non_revocation_semantics(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_issue_bootstrap(_settings) -> str:
        calls.append("issue_bootstrap")
        return "open-token"

    async def fake_rotate(_settings) -> str:
        calls.append("rotate")
        return "rotate-token"

    monkeypatch.setattr(cli_main, "_issue_bootstrap", fake_issue_bootstrap)
    monkeypatch.setattr(cli_main, "_rotate", fake_rotate)

    res_open = CliRunner().invoke(app, ["operator", "open", "--print-url", "--no-wait"])
    assert res_open.exit_code == 0
    assert calls == ["issue_bootstrap"]

    res_rotate = CliRunner().invoke(app, ["operator", "rotate"])
    assert res_rotate.exit_code == 0
    assert calls == ["issue_bootstrap", "rotate"]


def test_wait_for_dashboard_success(monkeypatch) -> None:
    from forge.settings import Settings

    class FakeResponse:
        status = 200

        def read(self, _size: int = 4096) -> bytes:
            return b'{"status": "ok", "role": "api"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class FakeOpener:
        def open(self, url: str, timeout: float = 0):
            return FakeResponse()

    monkeypatch.setattr(cli_main.urllib.request, "build_opener", lambda *args: FakeOpener())
    settings = Settings(process_role="cli")
    # Should complete without exception
    cli_main.wait_for_dashboard(settings, timeout=2.0)


def test_wait_for_dashboard_timeout(monkeypatch) -> None:
    from forge.settings import Settings

    class FakeOpener:
        def open(self, url: str, timeout: float = 0):
            raise OSError("Connection refused")

    monkeypatch.setattr(cli_main.urllib.request, "build_opener", lambda *args: FakeOpener())
    settings = Settings(process_role="cli")
    with pytest.raises(TimeoutError, match="Forge dashboard is not ready"):
        cli_main.wait_for_dashboard(settings, timeout=0.1)


@pytest.mark.parametrize(
    "invalid_timeout",
    ["inf", "-inf", "nan", "0", "-1", "-5.0"],
)
def test_operator_open_rejects_nonfinite_or_nonpositive_timeout(
    monkeypatch, invalid_timeout: str
) -> None:
    issue_called = False

    async def fake_issue_bootstrap(_settings) -> str:
        nonlocal issue_called
        issue_called = True
        return "token"

    monkeypatch.setattr(cli_main, "_issue_bootstrap", fake_issue_bootstrap)

    result = CliRunner().invoke(app, ["operator", "open", "--timeout", invalid_timeout])
    assert result.exit_code != 0
    assert not issue_called
    assert (
        "timeout must be a finite positive number" in result.output.lower()
        or "invalid value" in result.output.lower()
    )


@pytest.mark.parametrize(
    "invalid_timeout",
    ["inf", "-inf", "nan", "0", "-1"],
)
def test_operator_open_rejects_timeout_even_with_no_wait(monkeypatch, invalid_timeout: str) -> None:
    issue_called = False

    async def fake_issue_bootstrap(_settings) -> str:
        nonlocal issue_called
        issue_called = True
        return "token"

    monkeypatch.setattr(cli_main, "_issue_bootstrap", fake_issue_bootstrap)

    result = CliRunner().invoke(
        app, ["operator", "open", "--no-wait", "--timeout", invalid_timeout]
    )
    assert result.exit_code != 0
    assert not issue_called


@pytest.mark.parametrize("invalid_timeout", [float("inf"), float("-inf"), float("nan"), 0.0, -1.0])
def test_wait_for_dashboard_rejects_invalid_timeout(invalid_timeout: float) -> None:
    from forge.settings import Settings

    settings = Settings(process_role="cli")
    with pytest.raises(ValueError, match="timeout must be a finite positive number"):
        cli_main.wait_for_dashboard(settings, timeout=invalid_timeout)


def test_operator_open_settings_construction_failure_sanitized(monkeypatch) -> None:
    def fake_settings(**_kwargs):
        raise RuntimeError("postgresql+asyncpg://leakuser:secretpwd@127.0.0.1:5432/leakdb failed")

    monkeypatch.setattr(cli_main, "Settings", fake_settings)

    result = CliRunner().invoke(app, ["operator", "open"])
    assert result.exit_code == 1
    assert (
        "Could not load Forge configuration. Check the local environment settings." in result.stderr
    )
    assert "Forge is not ready" not in result.stderr
    assert "secretpwd" not in result.stderr
    assert "leakuser" not in result.stderr
    assert "postgresql" not in result.stderr


def test_operator_rotate_settings_construction_failure_sanitized(monkeypatch) -> None:
    def fake_settings(**_kwargs):
        raise RuntimeError("postgresql+asyncpg://leakuser:secretpwd@127.0.0.1:5432/leakdb failed")

    monkeypatch.setattr(cli_main, "Settings", fake_settings)

    result = CliRunner().invoke(app, ["operator", "rotate"])
    assert result.exit_code == 1
    assert (
        "Could not load Forge configuration. Check the local environment settings." in result.stderr
    )
    assert "secretpwd" not in result.stderr
    assert "leakuser" not in result.stderr
    assert "postgresql" not in result.stderr


def test_wait_for_dashboard_bypasses_proxies(monkeypatch) -> None:
    from forge.settings import Settings

    opener_built = False
    proxy_handler_checked = False

    class FakeResponse:
        status = 200

        def read(self, _size: int = 4096) -> bytes:
            return b'{"status": "ok", "role": "api"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class FakeOpener:
        def open(self, url: str, timeout: float = 0):
            return FakeResponse()

    def fake_build_opener(*handlers):
        nonlocal opener_built, proxy_handler_checked
        opener_built = True
        for h in handlers:
            if isinstance(h, cli_main.urllib.request.ProxyHandler):
                assert h.proxies == {}
                proxy_handler_checked = True
        return FakeOpener()

    monkeypatch.setattr(cli_main.urllib.request, "build_opener", fake_build_opener)

    settings = Settings(process_role="cli")
    cli_main.wait_for_dashboard(settings, timeout=2.0)
    assert opener_built
    assert proxy_handler_checked


@pytest.mark.parametrize(("timeout_seconds", "probe_duration"), [(1.0, 0.9), (0.05, 0.0)])
def test_wait_for_dashboard_caps_sleep_and_avoids_probing_after_expiry(
    monkeypatch, timeout_seconds: float, probe_duration: float
) -> None:
    from forge.settings import Settings

    simulated_time = 100.0
    sleeps: list[float] = []
    probe_calls = 0
    probe_timeouts: list[float] = []

    def fake_monotonic() -> float:
        return simulated_time

    def fake_sleep(secs: float) -> None:
        nonlocal simulated_time
        sleeps.append(secs)
        simulated_time += secs

    class FailingOpener:
        def open(self, url: str, timeout: float = 0):
            nonlocal probe_calls, simulated_time
            probe_calls += 1
            probe_timeouts.append(timeout)
            simulated_time += probe_duration
            raise OSError("Connection refused")

    monkeypatch.setattr(cli_main.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(cli_main.time, "sleep", fake_sleep)
    monkeypatch.setattr(cli_main.urllib.request, "build_opener", lambda *args: FailingOpener())

    settings = Settings(process_role="cli")
    with pytest.raises(TimeoutError, match="Forge dashboard is not ready"):
        cli_main.wait_for_dashboard(settings, timeout=timeout_seconds)

    assert probe_calls == 1
    assert probe_timeouts == pytest.approx([timeout_seconds])
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(timeout_seconds - probe_duration)


@pytest.mark.asyncio
async def test_with_auth_service_disposes_engine_on_success_and_failure(monkeypatch) -> None:
    from forge.settings import Settings

    disposed = 0

    class FakeEngine:
        async def dispose(self) -> None:
            nonlocal disposed
            disposed += 1

    monkeypatch.setattr(cli_main, "create_engine", lambda _url: FakeEngine())
    monkeypatch.setattr(cli_main, "create_session_factory", lambda _engine: lambda: None)
    monkeypatch.setattr(cli_main, "PostgresUnitOfWork", lambda _sf: None)

    settings = Settings(process_role="cli")

    async def ok_action(_service) -> str:
        return "success-result"

    res = await cli_main._with_auth_service(settings, ok_action)
    assert res == "success-result"
    assert disposed == 1

    async def fail_action(_service) -> str:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await cli_main._with_auth_service(settings, fail_action)
    assert disposed == 2


@pytest.mark.parametrize(
    "args",
    [
        ["operator", "open"],
        ["operator", "open", "--no-wait"],
        ["operator", "open", "--no-wait", "--print-url"],
        ["operator", "open", "--no-wait", "--headless"],
        ["operator", "rotate"],
    ],
)
@pytest.mark.parametrize(
    "origin",
    [
        "https://external.example",
        "http://127.0.0.1:3000/unexpected-path",
        "http://operator:fake-password@127.0.0.1:3000",
    ],
)
def test_operator_rejects_unsafe_origin_before_any_side_effect(monkeypatch, args, origin):
    calls: list[str] = []

    async def fake_token(_settings):
        calls.append("credential")
        return "credential-must-not-escape"

    monkeypatch.setenv("FORGE_WEB_ORIGIN", origin)
    monkeypatch.setattr(cli_main, "_issue_bootstrap", fake_token)
    monkeypatch.setattr(cli_main, "_rotate", fake_token)
    monkeypatch.setattr(cli_main, "wait_for_dashboard", lambda *a, **kw: calls.append("probe"))
    monkeypatch.setattr(cli_main.webbrowser, "open", lambda *a, **kw: calls.append("browser"))

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 1
    assert calls == []
    assert "Could not load Forge configuration" in result.stderr
    assert origin not in result.output
    assert "credential-must-not-escape" not in result.output
    assert "fake-password" not in result.output


@pytest.mark.parametrize(
    "args", [["operator", "open", "--no-wait", "--print-url"], ["operator", "rotate"]]
)
@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:3000", "http://localhost:3000", "https://[::1]:3000"]
)
def test_operator_accepts_canonical_loopback_origins(monkeypatch, args, origin):
    async def fake_token(_settings):
        return "local-bootstrap"

    monkeypatch.setenv("FORGE_WEB_ORIGIN", origin)
    monkeypatch.setattr(cli_main, "_issue_bootstrap", fake_token)
    monkeypatch.setattr(cli_main, "_rotate", fake_token)

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0
    assert result.stdout == f"{origin}/#bootstrap=local-bootstrap\n"
