"""Forge-owned Codex launch configuration, exercised only with fake clients."""

import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from forge.agents.client_process import ClientProcessSupervisor
from forge.application.ports.subscription_gateway import SubscriptionFailure
from test_codex_gateway import _Broker, _gateway, _report
from test_subscription_protocol import _request


async def capture(client, *, mutate_configuration=None):
    launches, sent = [], []

    class Session:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        async def send(self, value):
            sent.append(json.loads(json.dumps(value)))
            return await self.wrapped.send(value)

        async def receive(self):
            frame = await self.wrapped.receive()
            configuration = next(
                (value for value in sent if value.get("method") == "config/read"), None
            )
            if (
                mutate_configuration is not None
                and frame is not None
                and configuration is not None
                and frame.get("id") == configuration["id"]
            ):
                mutate_configuration(frame["result"])
            return frame

        async def close(self, **kwargs):
            return await self.wrapped.close(**kwargs)

    class Supervisor(ClientProcessSupervisor):
        async def start(self, spec, **kwargs):
            launches.append(spec)
            return Session(await super().start(spec, **kwargs))

    client._supervisor = Supervisor()
    result = await client.execute(_request())
    return result, launches, sent


async def test_launch_selects_the_trusted_home_without_inheriting_ambient_credentials(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ambient-home"))
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-untrusted-value")
    client = _gateway("success")
    result, launches, _ = await capture(client)
    assert result.failure is None
    assert len(launches) == 1
    assert launches[0].environment["CODEX_HOME"] == str(Path.cwd())
    assert set(launches[0].environment) <= {"CODEX_HOME", "SystemRoot"}
    assert "CODEX_HOME" in launches[0].allowed_environment
    assert launches[0].allowed_environment <= {"CODEX_HOME", "SystemRoot"}


async def test_server_and_thread_receive_fixed_controls_for_native_and_hosted_tools():
    client = _gateway("success")
    result, launches, sent = await capture(client)
    assert result.failure is None
    arguments = launches[0].argv[1 + len(client._installation.script) :]
    assert arguments, "server startup must carry explicit configuration controls"
    assert set(arguments[::2]) == {"-c"}
    values = tomllib.loads("\n".join(arguments[1::2]))
    assert values["model_provider"] == "openai"
    assert values["forced_login_method"] == "chatgpt"
    assert values["model"] == "gpt-5.6-luna" and values["model_reasoning_effort"] == "medium"
    assert values["web_search"] == "disabled"
    assert values["notify"] == [] and values["project_doc_max_bytes"] == 0
    assert values["features"] == {
        name: False
        for name in (
            "apps",
            "hooks",
            "plugins",
            "image_generation",
            "multi_agent",
            "multi_agent_v2",
            "code_mode",
            "in_app_browser",
            "in_app_chat",
            "memories",
            "shell_tool",
            "unified_exec",
            "view_image",
            "request_permissions_tool",
            "enable_mcp_apps",
            "psp",
            "executor_capability_discovery",
        )
    }
    thread = next(frame["params"] for frame in sent if frame.get("method") == "thread/start")
    # Per-thread dotted overrides follow the same supported ConfigManager path.
    per_thread = {
        key: value for key, value in thread["config"].items() if not key.startswith("features.")
    }
    per_thread["features"] = {
        key.removeprefix("features."): value
        for key, value in thread["config"].items()
        if key.startswith("features.")
    }
    assert per_thread == values
    assert thread["environments"] == [] and thread["allowProviderModelFallback"] is False


async def test_effective_configuration_is_verified_before_starting_thread():
    client = _gateway("success")
    result, _, sent = await capture(client)
    assert result.failure is None
    methods = [frame.get("method") for frame in sent]
    assert "config/read" in methods
    assert methods.index("config/read") < methods.index("thread/start")
    read = next(frame for frame in sent if frame.get("method") == "config/read")
    assert read["params"] == {"cwd": client._installation.cwd, "includeLayers": False}


@pytest.mark.parametrize(
    ("feature", "options"),
    [
        ("multi_agent_v2", {"max_concurrent_threads_per_session": 4}),
        ("code_mode", {"default_exec_yield_time_ms": 500}),
    ],
)
async def test_disabled_structured_features_preserve_supported_client_configuration(
    feature, options
):
    def mutate(response):
        response["config"]["features"][feature] = {"enabled": False, **options}

    client = _gateway("success", broker=_Broker())
    result, _, sent = await capture(client, mutate_configuration=mutate)
    assert result.failure is None and result.launch_proof.stop_confirmed
    thread = next(frame["params"] for frame in sent if frame.get("method") == "thread/start")
    assert thread["config"][f"features.{feature}"] is False
    assert client._broker.revoked


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("config",), None),
        (("config", "model"), "unapproved-model"),
        (("config", "model_reasoning_effort"), "high"),
        (("config", "model_provider"), "another-provider"),
        (("config", "forced_login_method"), "api"),
        (("config", "web_search"), "live"),
        (("config", "notify"), ["fixture-command-never-run"]),
        (("config", "project_doc_max_bytes"), 4096),
        (("config", "project_doc_max_bytes"), False),
        (("config", "features"), ...),
        (("config", "features", "apps"), True),
        (("config", "features", "apps"), {"enabled": False}),
        (("config", "features", "hooks"), True),
        (("config", "features", "shell_tool"), True),
        (("config", "features", "multi_agent"), 0),
        (("config", "features", "plugins"), ...),
        *[
            (("config", "features", feature), value)
            for feature in ("multi_agent_v2", "code_mode")
            for value in (
                {},
                {"enabled": True},
                {"enabled": None},
                {"enabled": 0},
                {"enabled": "false"},
            )
        ],
        (("config", "mcp_servers"), ...),
        (("config", "mcp_servers"), None),
        (("config", "mcp_servers"), {"inherited": {"enabled": True}}),
        (("config", "mcp_servers"), {"inherited": {"command": "never-run"}}),
        (("config", "mcp_servers"), {"inherited": {"enabled": "false"}}),
        (("config", "mcp_servers"), {"inherited": {"enabled": 0}}),
        (("config", "mcp_servers"), {"inherited": None}),
    ],
)
async def test_changed_or_unknown_effective_configuration_stops_before_thread(path, value):
    def mutate(response):
        parent = response
        for component in path[:-1]:
            parent = parent[component]
        if value is ...:
            parent.pop(path[-1])
        else:
            parent[path[-1]] = value

    client = _gateway("success", broker=_Broker())
    result, launches, sent = await capture(client, mutate_configuration=mutate)
    assert result.failure is SubscriptionFailure.UNAVAILABLE and result.decision is None
    assert result.quota_exhaustion is None and result.telemetry.is_quota_known is False
    assert result.launch_proof.stop_confirmed and len(launches) == 1
    assert all(frame.get("method") not in {"thread/start", "turn/start"} for frame in sent)
    assert not client._broker.calls and client._broker.revoked
    assert "fixture-command" not in result.failure_detail


async def test_explicitly_disabled_mcp_entries_do_not_grant_a_tool():
    def mutate(response):
        response["config"]["mcp_servers"] = {"disabled": {"enabled": False}}

    client = _gateway("success", broker=_Broker())
    result, _, sent = await capture(client, mutate_configuration=mutate)
    assert result.failure is None and result.launch_proof.stop_confirmed
    thread = next(frame["params"] for frame in sent if frame.get("method") == "thread/start")
    assert thread["environments"] == []
    assert not client._broker.calls


async def test_launch_disables_named_inherited_mcp_servers_without_rewriting_home(tmp_path):
    home_config = tmp_path / "config.toml"
    original = '[mcp_servers.inherited]\ncommand = "never-run"\nenabled = true\n'
    home_config.write_text(original, encoding="utf-8")
    client = _gateway("success", report=_report(client_home=str(tmp_path)), broker=_Broker())
    client._installation = replace(
        client._installation,
        client_home=str(tmp_path),
        disabled_mcp_servers=("inherited", "another_1"),
    )

    def mutate(response):
        response["config"]["mcp_servers"] = {
            name: {"enabled": False, "command": "never-run"} for name in ("inherited", "another_1")
        }

    result, launches, sent = await capture(client, mutate_configuration=mutate)
    assert result.failure is None and result.launch_proof.stop_confirmed
    arguments = launches[0].argv[1 + len(client._installation.script) :]
    values = tomllib.loads("\n".join(arguments[1::2]))
    assert values["mcp_servers"] == {
        "inherited": {"enabled": False},
        "another_1": {"enabled": False},
    }
    thread = next(frame["params"] for frame in sent if frame.get("method") == "thread/start")
    assert thread["config"]["mcp_servers.inherited.enabled"] is False
    assert thread["config"]["mcp_servers.another_1.enabled"] is False
    assert home_config.read_text(encoding="utf-8") == original
    assert not client._broker.calls and client._broker.revoked


@pytest.mark.parametrize(
    "servers",
    [
        {},
        {"inherited": {}},
        {"inherited": {"enabled": True}},
        {"inherited": {"enabled": 0}},
        {"inherited": {"enabled": "false"}},
        {"inherited": {"enabled": False}, "unexpected": {"enabled": True}},
    ],
)
async def test_mcp_disable_request_never_replaces_effective_configuration_proof(servers):
    client = _gateway("success", broker=_Broker())
    client._installation = replace(client._installation, disabled_mcp_servers=("inherited",))

    def mutate(response):
        response["config"]["mcp_servers"] = servers

    result, _, sent = await capture(client, mutate_configuration=mutate)
    assert result.failure is SubscriptionFailure.UNAVAILABLE and result.launch_proof.stop_confirmed
    assert result.quota_exhaustion is None and not result.telemetry.is_quota_known
    assert all(frame.get("method") not in {"thread/start", "turn/start"} for frame in sent)
    assert not client._broker.calls and client._broker.revoked


@pytest.mark.parametrize(
    "names",
    [
        None,
        "inherited",
        {"inherited": False},
        ("",),
        ("inherited", "inherited"),
        ("inherited.enabled",),
        ('"quoted"',),
        ("has space",),
        ("server\nfeatures.apps",),
        ("server=false",),
        ("a" * 129,),
        tuple(f"server-{index}" for index in range(65)),
        (False,),
        (["inherited"],),
    ],
)
def test_mcp_disable_names_cannot_inject_configuration_or_unbounded_arguments(names):
    with pytest.raises(ValueError, match="unique bounded names"):
        replace(_gateway("success")._installation, disabled_mcp_servers=names)


def test_mcp_disable_names_are_frozen_and_omitted_from_installation_representation():
    names = ["inherited-private-name"]
    installation = replace(_gateway("success")._installation, disabled_mcp_servers=names)
    names.clear()
    assert installation.disabled_mcp_servers == ("inherited-private-name",)
    assert "inherited-private-name" not in repr(installation)


@pytest.mark.parametrize("home", [None, "", ".", "relative/client-home", 123])
def test_client_home_cannot_fall_back_to_an_ambient_location(home):
    with pytest.raises(ValueError, match="explicit existing absolute"):
        replace(_gateway("success")._installation, client_home=home)


def test_missing_or_non_directory_home_is_rejected(tmp_path):
    installation = _gateway("success")._installation
    with pytest.raises(ValueError, match="explicit existing absolute"):
        replace(installation, client_home=str(tmp_path / "absent"))
    marker = tmp_path / "file"
    marker.write_text("fixture", encoding="utf-8")
    with pytest.raises(ValueError, match="explicit existing absolute"):
        replace(installation, client_home=str(marker))


@pytest.mark.parametrize("missing", [True, False])
async def test_home_verification_must_match_before_launch(tmp_path, missing):
    report = _report(client_home=None if missing else str(tmp_path))
    client = _gateway("success", report=report)
    result, launches, _ = await capture(client)
    assert result.failure is SubscriptionFailure.UNAVAILABLE
    assert result.launch_proof is None and launches == []


@pytest.mark.parametrize("gate", ["billing_allowance_enforced", "native_tools_isolated"])
async def test_configuration_does_not_replace_billing_or_isolation_proof(gate):
    client = _gateway("success", report=_report(**{gate: False}))
    client._installation = replace(client._installation, disabled_mcp_servers=("inherited",))
    result, launches, _ = await capture(client)
    assert result.failure is SubscriptionFailure.UNAVAILABLE and launches == []


def test_configuration_values_are_individual_toml_arguments_and_fresh_per_request():
    client = _gateway("success")
    model = 'fixture"\nfeatures.apps=true\n'
    client._installation = replace(client._installation, model=model)
    arguments = client._command()[len(client._installation.script) :]
    parsed = tomllib.loads("\n".join(arguments[1::2]))
    assert parsed["model"] == model and parsed["features"]["apps"] is False
    value = client._configuration()
    value["features.apps"] = True
    value["notify"].append("untrusted-command")
    assert client._configuration()["features.apps"] is False
    assert client._configuration()["notify"] == []
