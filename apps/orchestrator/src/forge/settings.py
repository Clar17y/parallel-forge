"""Validated configuration for Forge processes."""

from pathlib import Path
from typing import Literal

from platformdirs import user_data_path
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from forge.domain.validation import validate_runner_image_reference


class Settings(BaseSettings):
    """Server-side configuration shared by the API, worker, and CLI."""

    model_config = SettingsConfigDict(
        env_prefix="FORGE_",
        env_file=".env",
        extra="ignore",
    )

    process_role: Literal["api", "worker", "cli"] = "api"
    database_url: str = "postgresql+asyncpg://forge:forge@127.0.0.1:5435/forge"
    data_root: Path = user_data_path("Forge", "Parallel")
    bind_host: str = "127.0.0.1"
    api_port: int = 8000
    web_origin: str = "http://127.0.0.1:3000"
    runner_image: str = ""
    allow_remote: bool = False

    @property
    def artifact_root(self) -> Path:
        """Return the content-addressed artifact directory for this instance."""

        return self.data_root / "artifacts"

    @field_validator("runner_image")
    @classmethod
    def runner_image_must_be_immutable(cls, value: str) -> str:
        return validate_runner_image_reference(value)

    @model_validator(mode="after")
    def enforce_local_only(self) -> Settings:
        """Reject remote listeners until authentication is designed."""

        if self.bind_host not in {"127.0.0.1", "::1", "localhost"} and not self.allow_remote:
            raise ValueError("remote exposure requires a later authentication design")
        return self
