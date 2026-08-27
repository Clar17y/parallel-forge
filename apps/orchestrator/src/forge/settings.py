"""Validated configuration for Forge processes."""

from pathlib import Path
from typing import Literal

from platformdirs import user_data_path
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from forge.application.ports.provider_credentials import validate_provider_secret_reference
from forge.domain.validation import validate_runner_image_reference


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
    provider_secret_reference: str = ""
    google_api_key_reference: str = ""

    @property
    def artifact_root(self) -> Path:
        """Return the content-addressed artifact directory for this instance."""

        return self.data_root / "artifacts"

    @field_validator("runner_image")
    @classmethod
    def runner_image_must_be_immutable(cls, value: str) -> str:
        return validate_runner_image_reference(value)

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
        return self

    @property
    def effective_provider_secret_reference(self) -> str:
        """Return the Google-specific reference, or the generic fallback."""

        return self.google_api_key_reference or self.provider_secret_reference
