"""Provider credential resolution ports kept outside agent request contracts."""

from __future__ import annotations

import re
from typing import Final, Protocol, runtime_checkable

_SECRET_ID_RE: Final = re.compile(r"\A[a-z0-9_][a-z0-9_-]{0,127}\Z", re.ASCII)
_LOCAL_REFERENCE_RE: Final = re.compile(
    r"\Asecret://forge/([a-z0-9_][a-z0-9_-]{0,127})\Z", re.ASCII
)
_WINDOWS_DEVICE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


class ProviderCredentialError(RuntimeError):
    """A provider credential could not be resolved safely."""

    _MESSAGE = "provider credential could not be resolved"

    def __init__(self, _detail: object = None) -> None:
        del _detail
        super().__init__(self._MESSAGE)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._MESSAGE!r})"


def parse_provider_secret_reference(reference: object) -> str:
    """Return the exact local secret ID encoded by a provider reference."""

    if type(reference) is not str:
        raise ProviderCredentialError()
    match = _LOCAL_REFERENCE_RE.fullmatch(reference)
    if match is None:
        raise ProviderCredentialError()
    secret_id = match.group(1)
    if _SECRET_ID_RE.fullmatch(secret_id) is None or secret_id in _WINDOWS_DEVICE_NAMES:
        raise ProviderCredentialError()
    return secret_id


def validate_provider_secret_reference(reference: object, *, allow_empty: bool = False) -> str:
    """Validate a configured local reference without exposing its value."""

    if type(reference) is not str:
        raise ProviderCredentialError()
    if allow_empty and reference == "":
        return ""
    parse_provider_secret_reference(reference)
    return reference


@runtime_checkable
class ProviderCredentialResolverPort(Protocol):
    """Async server-side lookup of one configured provider secret reference."""

    async def resolve(self, reference: str) -> str: ...


__all__ = [
    "ProviderCredentialError",
    "ProviderCredentialResolverPort",
    "parse_provider_secret_reference",
    "validate_provider_secret_reference",
]
