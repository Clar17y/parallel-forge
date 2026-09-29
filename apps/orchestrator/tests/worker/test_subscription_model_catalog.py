"""Model choices are bounded advisory metadata, never execution authority."""

import asyncio
import sys
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.agents.client_process import (
    ClientProcessSupervisor,
    ClientProtocolError,
    ProcessIdentityStatus,
)
from forge.worker import subscription_model_catalog
from forge.worker.subscription_model_catalog import (
    SubscriptionModelCatalogRefresher,
    parse_antigravity_models,
    parse_codex_models,
)


def test_codex_catalog_maps_efforts_deduplicates_and_rejects_untrusted_text():
    data = [
        {
            "model": "gpt-6-sol",
            "displayName": "GPT-6 Sol",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "low"},
                {"reasoningEffort": "xhigh"},
                {"reasoningEffort": "ultra"},
            ],
        },
        {"model": "gpt-6-sol", "displayName": "Duplicate", "supportedReasoningEfforts": []},
        {"model": "password=secret", "supportedReasoningEfforts": []},
        {
            "model": "gpt-6-luna",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "medium"},
            ],
        },
    ]
    assert parse_codex_models(data) == [
        {"id": "gpt-6-sol", "label": "GPT-6 Sol", "efforts": ["low", "maximum"]},
        {"id": "gpt-6-luna", "label": "gpt-6-luna", "efforts": ["medium"]},
    ]


def test_antigravity_listing_requires_strict_metadata_lines():
    assert parse_antigravity_models(
        "Fetching available models...\ngemini-3.8-flash-low\tGemini 3.8 Flash Low\n"
    ) == [{"id": "gemini-3.8-flash-low", "label": "Gemini 3.8 Flash Low", "efforts": ["low"]}]
    with pytest.raises(ValueError):
        parse_antigravity_models("Fetching available models...\nC:\\secret\tPrivate model\n")
    with pytest.raises(ValueError):
        parse_antigravity_models("Prompt: choose a model\ngemini-3.8-flash-low\tGemini\n")


@pytest.mark.asyncio
async def test_refresh_deduplicates_routes_and_retains_last_good_after_failure(monkeypatch):
    item = SimpleNamespace(
        client="codex_app_server",
        executable="client",
        cwd="workspace",
        home="home",
        model="gpt-6-sol",
        effort="medium",
    )
    reports = []

    class Store:
        async def report_catalogs(self, identity, catalogs):
            reports.append(catalogs)
            return True

    calls = 0

    async def discovery(*_):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError
        return ([{"id": "gpt-6-sol", "label": "GPT-6 Sol", "efforts": ["medium"]}], False)

    monkeypatch.setattr(subscription_model_catalog, "_codex_models", discovery)
    refresher = SubscriptionModelCatalogRefresher((item, item), Store(), "instance")
    await refresher.refresh()
    good = reports[-1][0]
    assert calls == 1 and good["source"] == "provider" and good["status"] == "available"
    await refresher.refresh()
    failed = reports[-1][0]
    assert calls == 2 and failed["status"] == "unavailable" and failed["stale"]
    assert failed["observed_at"] == good["observed_at"]
    assert failed["models"] == good["models"]


@pytest.mark.asyncio
async def test_catalog_persistence_failure_does_not_stop_worker_task():
    class BrokenStore:
        async def report_catalogs(self, *_):
            raise RuntimeError("fixture failure")

    stop = asyncio.Event()
    refresher = SubscriptionModelCatalogRefresher((), BrokenStore(), uuid4())
    running = asyncio.create_task(refresher.run(stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(running, 1)


@pytest.mark.asyncio
async def test_refresh_replaces_previous_success_and_recovers_after_failure(monkeypatch):
    item = SimpleNamespace(
        client="codex_app_server",
        executable="client",
        cwd="work",
        home="home",
        model="old",
        effort="low",
    )
    reports = []

    class Store:
        async def report_catalogs(self, _, catalogs):
            reports.append(catalogs[0])

    outcomes = iter(
        [
            ([{"id": "old", "label": "Old", "efforts": ["low"]}], False),
            ([{"id": "new", "label": "New", "efforts": ["low"]}], False),
            TimeoutError(),
            ([{"id": "latest", "label": "Latest", "efforts": ["low"]}], False),
        ]
    )

    async def discover(*_):
        value = next(outcomes)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(subscription_model_catalog, "_codex_models", discover)
    refresher = SubscriptionModelCatalogRefresher((item,), Store(), uuid4())
    for _ in range(4):
        await refresher.refresh()
    assert [[m["id"] for m in r["models"]] for r in reports] == [
        ["old"],
        ["new"],
        ["new"],
        ["latest"],
    ]
    assert (
        reports[2]["status"] == "unavailable"
        and reports[2]["observed_at"] == reports[1]["observed_at"]
    )
    assert reports[3]["status"] == "available" and not reports[3]["stale"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_first", [True, False])
async def test_refresh_merges_only_current_successes_with_cap_and_partial(
    monkeypatch, failure_first
):
    first = SimpleNamespace(
        client="codex_app_server",
        executable="first",
        cwd="work",
        home="home",
        model="old",
        effort="low",
    )
    second = SimpleNamespace(
        client="codex_app_server",
        executable="second",
        cwd="work",
        home="home",
        model="old",
        effort="low",
    )
    reports = []

    class Store:
        async def report_catalogs(self, _, catalogs):
            reports.append(catalogs[0])

    cycle = 0

    async def discover(item, _):
        if cycle == 0:
            return ([{"id": "old", "label": "Old", "efforts": ["low"]}], False)
        if cycle == 1 and item.executable == "first":
            raise TimeoutError()
        prefix = "a" if item.executable == "first" else "b"
        return (
            [
                {"id": f"{prefix}{n}", "label": f"{prefix}{n}", "efforts": ["low"]}
                for n in range(150)
            ],
            False,
        )

    monkeypatch.setattr(subscription_model_catalog, "_codex_models", discover)
    items = (first, second) if failure_first else (second, first)
    refresher = SubscriptionModelCatalogRefresher(items, Store(), uuid4())
    await refresher.refresh()
    cycle = 1
    await refresher.refresh()
    assert reports[-1]["status"] == "available"
    assert len(reports[-1]["models"]) == 150
    assert "old" not in {m["id"] for m in reports[-1]["models"]}
    cycle = 2
    await refresher.refresh()
    assert len(reports[-1]["models"]) == 200
    assert "first 200" in reports[-1]["message"]


@pytest.mark.asyncio
async def test_codex_metadata_exchange_uses_protocol_and_settles(monkeypatch):
    script = """import json,sys
for expected in ('initialize','initialized','model/list'):
 frame=json.loads(sys.stdin.readline())
 assert frame['method']==expected
 if expected=='initialize': print(json.dumps({'id':frame['id'],'result':{}}),flush=True)
 if expected=='model/list':
  assert frame['params']=={'includeHidden':False,'limit':200}
  print(json.dumps({'id':frame['id'],'result':{'data':[{'model':'gpt-6-sol','supportedReasoningEfforts':[{'reasoningEffort':'xhigh'}]}]}}),flush=True)
"""

    class ScriptSupervisor(ClientProcessSupervisor):
        def __init__(self):
            self.sessions = []

        async def start(self, spec, **kwargs):
            session = await super().start(
                replace(spec, argv=(sys.executable, "-c", script)), **kwargs
            )
            self.sessions.append(session)
            return session

    item = SimpleNamespace(executable=sys.executable, cwd=".", home=".")
    supervisor = ScriptSupervisor()
    models, partial = await subscription_model_catalog._codex_models(item, supervisor)
    assert models == [{"id": "gpt-6-sol", "label": "gpt-6-sol", "efforts": ["maximum"]}]
    assert not partial
    assert supervisor.identity_status(supervisor.sessions[0].receipt) is ProcessIdentityStatus.GONE
    bad = ScriptSupervisor()
    script = "print('not-json',flush=True)"
    with pytest.raises(ClientProtocolError):
        await subscription_model_catalog._codex_models(item, bad)
    assert bad.identity_status(bad.sessions[0].receipt) is ProcessIdentityStatus.GONE
