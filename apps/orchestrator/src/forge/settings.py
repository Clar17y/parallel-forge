"""Validated configuration for Forge processes."""

from pathlib import Path
from typing import Literal

from platformdirs import user_data_path
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from forge.application.ports.provider_credentials import validate_provider_secret_reference
from forge.domain.subscription import TaskBudget
from forge.domain.subscription_installations import (
    load_subscription_installation_manifest,
    merge_installation_quota_policy,
)
from forge.domain.subscription_quota import QuotaPolicy
from forge.domain.validation import validate_runner_image_reference
from forge.release.credentials import validate_github_credential_reference


class Settings(BaseSettings):
    """Server-side configuration shared by the API, worker, and CLI."""

    model_config = SettingsConfigDict(
        env_prefix="FORGE_",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    process_role: Literal["api", "worker", "cli"] = "api"
    database_url: str = "postgresql+asyncpg://forge:forge@127.0.0.1:5435/forge"
    data_root: Path = user_data_path("Forge", "Parallel")
    bind_host: str = "127.0.0.1"
    api_port: int = 8000
    web_origin: str = "http://127.0.0.1:3000"
    runner_image: str = ""
    allow_remote: bool = False
    provider_secret_reference: str = Field(default="", repr=False)
    google_api_key_reference: str = Field(default="", repr=False)
    github_token_reference: str = Field(default="", repr=False)
    pricing_catalog_path: Path | None = None
    prompt_root: Path | None = None
    subscription_installations_path: Path | None = None
    subscription_quota_policy: QuotaPolicy = Field(default_factory=QuotaPolicy)
    subscription_primary_budget: TaskBudget = Field(
        default_factory=lambda: TaskBudget(max_provider_attempts=64)
    )
    subscription_worker_concurrency: int = Field(default=3, ge=1, le=64, strict=True)
    # Advisory search ranking. "shadow" measures without changing agent input;
    # "on" also collapses the low-relevance tail. Both require TYPESAFE_API_KEY
    # in the process environment and degrade to "off" without it. Enabling
    # either sends bounded, redacted repository match text to TypeSafe.
    search_ranking_mode: Literal["off", "shadow", "on"] = "off"
    search_ranking_top_k: int = Field(default=15, ge=1, le=100, strict=True)
    search_ranking_model: str = Field(default="jev-latest", min_length=1, max_length=128)
    subscription_attempt_budget: TaskBudget = Field(
        default_factory=lambda: TaskBudget(
            max_duration_seconds=300,
            max_tool_calls=25,
            max_named_checks=2,
            max_provider_attempts=1,
            max_repairs=0,
        )
    )

    @field_validator("subscription_worker_concurrency", mode="before")
    @classmethod
    def subscription_concurrency_from_environment(cls, value: object) -> object:
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            return int(value)
        return value

    @field_validator("subscription_attempt_budget")
    @classmethod
    def subscription_attempt_is_bounded(cls, value: TaskBudget) -> TaskBudget:
        if (
            value.max_provider_attempts != 1
            or value.max_repairs != 0
            or value.max_duration_seconds < 1
        ):
            raise ValueError(
                "subscription attempt requires positive duration, one attempt and no repair"
            )
        return value

    @property
    def artifact_root(self) -> Path:
        """Return the content-addressed artifact directory for this instance."""

        return self.data_root / "artifacts"

    @field_validator("runner_image")
    @classmethod
    def runner_image_must_be_immutable(cls, value: str) -> str:
        return validate_runner_image_reference(value)

    @field_validator("github_token_reference")
    @classmethod
    def github_reference_must_be_local(cls, value: str) -> str:
        return validate_github_credential_reference(value) if value else ""

    @field_validator(
        "provider_secret_reference",
        "google_api_key_reference",
    )
    @classmethod
    def provider_references_must_be_local(cls, value: str) -> str:
        return validate_provider_secret_reference(value, allow_empty=True)

    @model_validator(mode="after")
    def enforce_local_only(self) -> Settings:
        """Reject remote listeners until authentication is designed."""

        if self.bind_host not in {"127.0.0.1", "::1", "localhost"} and not self.allow_remote:
            raise ValueError("remote exposure requires a later authentication design")
        if (
            self.provider_secret_reference
            and self.google_api_key_reference
            and self.provider_secret_reference != self.google_api_key_reference
        ):
            raise ValueError("generic and Google provider references conflict")
        manifest = load_subscription_installation_manifest(self.subscription_installations_path)
        if manifest is not None:
            merged = merge_installation_quota_policy(self.subscription_quota_policy, manifest)
            if merged is not None:
                self.subscription_quota_policy = merged
        return self

    @property
    def effective_provider_secret_reference(self) -> str:
        """Return the Google-specific reference, or the generic fallback."""

        return self.google_api_key_reference or self.provider_secret_reference
