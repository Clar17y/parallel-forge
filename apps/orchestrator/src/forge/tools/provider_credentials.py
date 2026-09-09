"""Local, exact-reference provider credential resolution."""

from __future__ import annotations

import asyncio
from typing import Final

from forge.application.ports.provider_credentials import (
    ProviderCredentialError,
    ProviderCredentialResolverPort,
    parse_provider_secret_reference,
    validate_provider_secret_reference,
)
from forge.application.ports.worktrees import SecretStorePort

_MAX_PROVIDER_KEY_BYTES: Final[int] = 4_096


def _decode_provider_key(value: object) -> str:
    if type(value) is not bytes or not value or len(value) > _MAX_PROVIDER_KEY_BYTES:
        raise ProviderCredentialError()
    try:
        decoded = value.decode("utf-8", errors="strict")
    except UnicodeError:
        raise ProviderCredentialError() from None
    try:
        encoded_length = len(decoded.encode("utf-8"))
    except UnicodeError:
        raise ProviderCredentialError() from None
    if (
        not decoded
        or decoded != decoded.strip()
        or encoded_length > _MAX_PROVIDER_KEY_BYTES
        or any(character.isspace() or not character.isascii() for character in decoded)
    ):
        raise ProviderCredentialError()
    return decoded


class LocalProviderCredentialResolver:
    """Resolve only ``secret://forge/<secret-id>`` through a local secret store."""

    def __init__(self, secret_store: SecretStorePort) -> None:
        if not isinstance(secret_store, SecretStorePort):
            raise ProviderCredentialError()
        self._secret_store = secret_store

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    async def resolve(self, reference: str) -> str:
        """Read and strictly decode one configured API key off the event loop."""

        secret_id = parse_provider_secret_reference(reference)
        try:
            raw_value = await asyncio.to_thread(self._secret_store.read, secret_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - store failures are context-free
            raise ProviderCredentialError() from None
        return _decode_provider_key(raw_value)


__all__ = [
    "LocalProviderCredentialResolver",
    "ProviderCredentialError",
    "ProviderCredentialResolverPort",
    "parse_provider_secret_reference",
    "validate_provider_secret_reference",
]
