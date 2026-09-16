"""Closed operator configuration for trusted subscription installations."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.domain.subscription_quota import QuotaPolicy, QuotaPoolKey, QuotaRoutePool

_MAX_MANIFEST_BYTES = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class InstallationQuota(_ClosedModel):
    """Opaque scheduler identity; never an account name or credential."""

    account: str
    pool: str

    @model_validator(mode="after")
    def validate_labels(self) -> InstallationQuota:
        QuotaPoolKey("configured", self.account, self.pool)
        return self


class _InstallationSpec(_ClosedModel):
    executable: str = Field(min_length=1, max_length=4096)
    cwd: str = Field(min_length=1, max_length=4096)
    home: str = Field(min_length=1, max_length=4096)
    model: str = Field(min_length=1, max_length=255)
    effort: str = Field(min_length=1, max_length=32)
    account: str
    executable_digest: str
    quota: InstallationQuota

    @field_validator("executable", "cwd", "home", "model", "effort")
    @classmethod
    def reject_controlled_text(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("installation text contains a null byte")
        return value

    @field_validator("executable", "cwd", "home")
    @classmethod
    def require_absolute_path(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("installation paths must be absolute")
        return value

    @field_validator("account")
    @classmethod
    def require_opaque_account(cls, value: str) -> str:
        QuotaPoolKey("configured", value, "configured")
        return value

    @field_validator("executable_digest")
    @classmethod
    def require_executable_digest(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("executable identity requires a lowercase SHA-256 digest")
        return value


class CodexInstallationSpec(_InstallationSpec):
    client: Literal["codex_app_server"]
    quota_limit_id: str | None = None
    disabled_mcp_servers: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("account")
    @classmethod
    def require_account_digest(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("Codex account identity requires a lowercase SHA-256 digest")
        return value


class ClaudeInstallationSpec(_InstallationSpec):
    client: Literal["claude_code"]
    quota_limit_types: tuple[
        Literal["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"], ...
    ] = Field(default=(), max_length=4)

    @field_validator("quota_limit_types")
    @classmethod
    def unique_quota_windows(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Claude quota windows must be unique")
        return value


class GeminiInstallationSpec(_InstallationSpec):
    client: Literal["gemini_cli"]


SubscriptionInstallationSpec = Annotated[
    CodexInstallationSpec | ClaudeInstallationSpec | GeminiInstallationSpec,
    Field(discriminator="client"),
]


class SubscriptionInstallationManifest(_ClosedModel):
    version: Literal[1]
    installations: tuple[SubscriptionInstallationSpec, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def reject_duplicate_routes(self) -> SubscriptionInstallationManifest:
        routes = tuple((item.client, item.model, item.effort) for item in self.installations)
        if len(routes) != len(set(routes)):
            raise ValueError("subscription installation routes must be unique")
        return self


def load_subscription_installation_manifest(
    path: Path | None,
) -> SubscriptionInstallationManifest | None:
    """Read one bounded manifest, returning no authority for every invalid input."""

    if path is None or not isinstance(path, Path) or not path.is_absolute():
        return None
    try:
        if not path.is_file():
            return None
        with path.open("rb") as stream:
            wire = stream.read(_MAX_MANIFEST_BYTES + 1)
        if not wire or len(wire) > _MAX_MANIFEST_BYTES:
            return None
        text = wire.decode("utf-8")
        json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        return SubscriptionInstallationManifest.model_validate_json(wire, strict=True)
    except OSError, UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError:
        return None


def quota_route_for(item: SubscriptionInstallationSpec) -> QuotaRoutePool:
    """Map a closed client discriminator to its exact durable quota selector."""

    provider = {
        "codex_app_server": "openai",
        "claude_code": "anthropic",
        "gemini_cli": "google",
    }[item.client]
    return QuotaRoutePool(
        provider=provider,
        client=item.client,
        account=item.quota.account,
        pool=item.quota.pool,
        model=item.model,
    )


def merge_installation_quota_policy(
    policy: QuotaPolicy,
    manifest: SubscriptionInstallationManifest,
) -> QuotaPolicy | None:
    """Merge exact installation mappings, rejecting conflicting selectors."""

    if not isinstance(policy, QuotaPolicy) or not isinstance(
        manifest, SubscriptionInstallationManifest
    ):
        raise TypeError("installation quota merge requires validated inputs")
    routes = list(policy.route_pools)
    by_selector = {
        (route.provider, route.client, route.auth_mode, route.billing_mode, route.model): route
        for route in routes
    }
    for item in manifest.installations:
        route = quota_route_for(item)
        selector = (
            route.provider,
            route.client,
            route.auth_mode,
            route.billing_mode,
            route.model,
        )
        existing = by_selector.get(selector)
        if existing is not None:
            if existing != route:
                return None
            continue
        routes.append(route)
        by_selector[selector] = route
    try:
        return QuotaPolicy(
            route_pools=tuple(routes),
            unknown_reset_cooldown_seconds=policy.unknown_reset_cooldown_seconds,
        )
    except TypeError, ValueError:
        return None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"illegal JSON constant: {value}")


__all__ = [
    "ClaudeInstallationSpec",
    "CodexInstallationSpec",
    "GeminiInstallationSpec",
    "SubscriptionInstallationManifest",
    "SubscriptionInstallationSpec",
    "load_subscription_installation_manifest",
    "merge_installation_quota_policy",
    "quota_route_for",
]
