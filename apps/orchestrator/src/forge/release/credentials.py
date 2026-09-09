"""Server-only GitHub token resolution with safe failure boundaries."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from forge.application.ports.provider_credentials import (
    ProviderCredentialError,
    parse_provider_secret_reference,
)
from forge.application.ports.worktrees import SecretStorePort

_ENV_REFERENCE = re.compile(r"\Aenv://([A-Z_][A-Z0-9_]{0,127})\Z", re.ASCII)
_MAX_TOKEN_BYTES = 4096


class GitHubCredentialError(RuntimeError):
    def __init__(self, _detail: object = None) -> None:
        del _detail
        super().__init__("GitHub credential could not be resolved")

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


@runtime_checkable
class GitHubCredentialResolverPort(Protocol):
    async def resolve(self, reference: str) -> str: ...


def validate_github_credential_reference(reference: object) -> str:
    if type(reference) is not str or not reference:
        raise GitHubCredentialError()
    if _ENV_REFERENCE.fullmatch(reference) is not None:
        return reference
    try:
        parse_provider_secret_reference(reference)
    except ProviderCredentialError:
        raise GitHubCredentialError() from None
    return reference


def _decode_token(value: object) -> str:
    if type(value) is bytes:
        try:
            value = value.decode("utf-8", "strict")
        except UnicodeError:
            raise GitHubCredentialError() from None
    if type(value) is not str or not value or value != value.strip():
        raise GitHubCredentialError()
    try:
        if len(value.encode("utf-8")) > _MAX_TOKEN_BYTES:
            raise GitHubCredentialError()
    except UnicodeError:
        raise GitHubCredentialError() from None
    if any(character.isspace() or not character.isascii() for character in value):
        raise GitHubCredentialError()
    return value


class LocalGitHubCredentialResolver:
    def __init__(
        self, secret_store: SecretStorePort, *, environment: Mapping[str, str] | None = None
    ) -> None:
        if not isinstance(secret_store, SecretStorePort):
            raise GitHubCredentialError()
        self._secret_store = secret_store
        self._environment = environment if environment is not None else os.environ

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    async def resolve(self, reference: str) -> str:
        validated = validate_github_credential_reference(reference)
        env_match = _ENV_REFERENCE.fullmatch(validated)
        try:
            if env_match is not None:
                return _decode_token(self._environment.get(env_match.group(1)))
            return _decode_token(
                await asyncio.to_thread(
                    self._secret_store.read, parse_provider_secret_reference(validated)
                )
            )
        except asyncio.CancelledError:
            raise
        except OSError, RuntimeError, TypeError, ValueError:
            raise GitHubCredentialError() from None


__all__ = [
    "GitHubCredentialError",
    "GitHubCredentialResolverPort",
    "LocalGitHubCredentialResolver",
    "validate_github_credential_reference",
]
