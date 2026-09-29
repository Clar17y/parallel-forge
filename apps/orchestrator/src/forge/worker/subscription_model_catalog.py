"""Worker-only, bounded metadata discovery for advisory subscription choices."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from uuid import UUID

from forge.agents.antigravity_configuration import antigravity_launch_environment
from forge.agents.client_process import ClientLaunchSpec, ClientProcessSupervisor
from forge.domain.subscription_installations import SubscriptionInstallationSpec
from forge.persistence.repositories.subscription_runtime_status import (
    SubscriptionRuntimeStatusStore,
)

REFRESH_SECONDS = 600
logger = logging.getLogger(__name__)
_MODEL_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.+-]{0,127}\Z")
_SAFE_LABEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9 ._+()-]{0,127}\Z")
_EFFORTS = {
    "none": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "maximum",
    "max": "maximum",
}
_PROVIDERS = {
    "codex_app_server": "openai",
    "claude_code": "anthropic",
    "gemini_cli": "google",
    "antigravity_cli": "google",
}
_AGY_PREFIX = "Fetching available models..."


def parse_codex_models(data: object) -> list[dict[str, object]]:
    if not isinstance(data, list) or len(data) > 200:
        raise ValueError("invalid model catalog")
    result: dict[str, dict[str, object]] = {}
    for item in data:
        if not isinstance(item, Mapping):
            continue
        model = item.get("model", item.get("id"))
        if not isinstance(model, str) or not _MODEL_ID.fullmatch(model):
            continue
        if model in result:
            continue
        raw_efforts = item.get("supportedReasoningEfforts")
        if not isinstance(raw_efforts, list) or len(raw_efforts) > 16:
            continue
        efforts = list(
            dict.fromkeys(
                _EFFORTS[value["reasoningEffort"]]
                for value in raw_efforts
                if isinstance(value, Mapping) and value.get("reasoningEffort") in _EFFORTS
            )
        )
        if not efforts:
            continue
        label = item.get("displayName")
        if not isinstance(label, str) or not _SAFE_LABEL.fullmatch(label):
            label = model
        result[model] = {"id": model, "label": label, "efforts": efforts}
    return list(result.values())


def parse_antigravity_models(text: object) -> list[dict[str, object]]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > 64 * 1024:
        raise ValueError("invalid Antigravity model metadata")
    lines = text.strip().splitlines()
    if not 1 < len(lines) <= 201 or lines[0].strip() != _AGY_PREFIX:
        raise ValueError("invalid Antigravity model metadata")
    result: dict[str, dict[str, object]] = {}
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != 2:
            raise ValueError("invalid Antigravity model metadata")
        model, label = (part.strip() for part in parts)
        if not _MODEL_ID.fullmatch(model) or not _SAFE_LABEL.fullmatch(label):
            raise ValueError("invalid Antigravity model metadata")
        if model in result:
            continue
        effort = next(
            (value for value in ("low", "medium", "high") if model.endswith("-" + value)), None
        )
        if effort is None:
            continue
        result[model] = {"id": model, "label": label, "efforts": [effort]}
    return list(result.values())


async def _codex_models(
    item: SubscriptionInstallationSpec, supervisor: ClientProcessSupervisor
) -> tuple[list[dict[str, object]], bool]:
    spec = ClientLaunchSpec(
        argv=(item.executable, "app-server", "--stdio"),
        cwd=item.cwd,
        environment={"CODEX_HOME": item.home},
        allowed_environment=frozenset({"CODEX_HOME"}),
        duration_seconds=20,
        stdout_max_bytes=512 * 1024,
        stderr_max_bytes=64 * 1024,
        frame_max_bytes=256 * 1024,
    )
    session = await supervisor.start(spec)
    try:

        async def response(identity: int) -> Mapping[str, object]:
            for _ in range(32):
                frame = await session.receive()
                if frame is None:
                    break
                if frame.get("id") == identity:
                    result = frame.get("result")
                    if not isinstance(result, Mapping):
                        break
                    return result
            raise ValueError("model metadata unavailable")

        await session.send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "forge-model-catalog", "version": "0.2"},
                    "capabilities": {},
                },
            }
        )
        await response(1)
        await session.send({"method": "initialized", "params": {}})
        await session.send(
            {
                "id": 2,
                "method": "model/list",
                "params": {
                    "includeHidden": False,
                    "limit": 200,
                },
            }
        )
        page = await response(2)
        # The first page is capped. Surface partial coverage instead of
        # traversing an unbounded client-provided cursor.
        models = parse_codex_models(page.get("data"))
        partial = page.get("nextCursor") is not None
        receipt = await session.close()
        if not receipt.stop_confirmed:
            raise ValueError("model metadata process did not settle")
        return models, partial
    finally:
        await session.close()


async def _antigravity_models(
    item: SubscriptionInstallationSpec, supervisor: ClientProcessSupervisor
) -> list[dict[str, object]]:
    environment = antigravity_launch_environment(item.home)
    spec = ClientLaunchSpec(
        argv=(item.executable, "models"),
        cwd=item.cwd,
        environment=environment,
        allowed_environment=frozenset(environment),
        duration_seconds=20,
        stdout_max_bytes=64 * 1024,
        stderr_max_bytes=16 * 1024,
        frame_max_bytes=64 * 1024,
        protocol="text_document",
    )
    return parse_antigravity_models(await supervisor.run_text_document(spec))


def configured_catalogs(specs: Iterable[SubscriptionInstallationSpec]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for item in specs:
        key = (_PROVIDERS[item.client], item.client)
        catalog = grouped.setdefault(
            key,
            {
                "provider": key[0],
                "client": key[1],
                "source": "configured",
                "status": "available",
                "observed_at": None,
                "stale": False,
                "models": [],
                "message": "Configured choices only; worker configuration is required to run a selected model.",
            },
        )
        models = catalog["models"]
        assert isinstance(models, list)
        if not _MODEL_ID.fullmatch(item.model):
            continue
        option = next((model for model in models if model["id"] == item.model), None)
        if option is None:
            option = {"id": item.model, "label": item.model, "efforts": []}
            models.append(option)
        if item.effort not in option["efforts"]:
            option["efforts"].append(item.effort)
    return list(grouped.values())[:8]


class SubscriptionModelCatalogRefresher:
    def __init__(
        self,
        specs: Iterable[SubscriptionInstallationSpec],
        store: SubscriptionRuntimeStatusStore,
        instance_id: UUID,
        *,
        supervisor: ClientProcessSupervisor | None = None,
    ) -> None:
        self._specs = tuple(specs)
        self._store = store
        self._instance_id = instance_id
        self._supervisor = supervisor or ClientProcessSupervisor()
        self._catalogs = configured_catalogs(self._specs)

    async def refresh(self) -> None:
        current = {(c["provider"], c["client"]): dict(c) for c in self._catalogs}
        # One metadata request per installation identity, even when many routes
        # use that client. Other clients retain honest configured choices.
        seen: set[tuple[str, str, str]] = set()
        refreshed: set[tuple[str, str]] = set()
        partial_keys: set[tuple[str, str]] = set()
        for item in self._specs:
            if item.client not in {"codex_app_server", "antigravity_cli"}:
                continue
            identity = (item.executable, item.cwd, item.home)
            if identity in seen:
                continue
            seen.add(identity)
            key = (_PROVIDERS[item.client], item.client)
            try:
                if item.client == "codex_app_server":
                    models, partial = await _codex_models(item, self._supervisor)
                else:
                    models = await _antigravity_models(item, self._supervisor)
                    partial = False
                if not models:
                    raise ValueError("empty model metadata")
                if key in refreshed:
                    existing = current[key]["models"]
                    assert isinstance(existing, list)
                    seen_models = {model["id"] for model in existing}
                    models = existing + [
                        model for model in models if model["id"] not in seen_models
                    ]
                partial = partial or key in partial_keys or len(models) > 200
                if partial:
                    partial_keys.add(key)
                current[key] = {
                    "provider": key[0],
                    "client": key[1],
                    "source": "provider",
                    "status": "available",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "stale": False,
                    "models": models[:200],
                    "message": (
                        "Installed client returned more models than the catalog limit; showing first 200."
                        if partial
                        else "Available choices from the installed client; worker configuration is required to run a selected model."
                    ),
                }
                refreshed.add(key)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - provider text must never reach storage
                old = current[key]
                if key in refreshed:
                    continue
                old["status"] = "unavailable"
                old["stale"] = True
                old["message"] = (
                    "Installed client model metadata is unavailable; showing last known choices."
                    if old["source"] == "provider"
                    else "Installed client model metadata is unavailable; showing configured choices."
                )
        self._catalogs = list(current.values())[:8]
        await self._store.report_catalogs(self._instance_id, self._catalogs)

    async def run(self, stop: asyncio.Event) -> None:
        try:
            await self._store.report_catalogs(self._instance_id, self._catalogs)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - advisory persistence cannot stop worker
            logger.warning("Subscription model catalog snapshot unavailable")
        while not stop.is_set():
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - diagnostic failure cannot stop worker
                logger.warning("Subscription model catalog refresh unavailable")
            try:
                await asyncio.wait_for(stop.wait(), timeout=REFRESH_SECONDS)
            except TimeoutError:
                pass
