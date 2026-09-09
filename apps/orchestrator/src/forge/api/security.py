"""Same-origin, cookie, and CSRF primitives for the loopback API."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

from fastapi import Request, Response

from forge.settings import Settings

SESSION_COOKIE = "forge_session"
CSRF_HEADER = "X-CSRF-Token"


class RequestSecurityError(RuntimeError):
    """The request did not satisfy the configured same-origin policy."""


@dataclass(frozen=True, slots=True)
class WebOrigin:
    """Canonical configured origin and its exact Host header value."""

    scheme: str
    netloc: str

    @property
    def value(self) -> str:
        return f"{self.scheme}://{self.netloc}"


def parse_web_origin(value: str) -> WebOrigin:
    """Validate one canonical HTTP(S) loopback origin."""

    if not isinstance(value, str) or not value:
        raise ValueError("web origin must be a canonical loopback origin")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        raise ValueError("web origin must be a canonical loopback origin") from None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or hostname is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("web origin must be a canonical loopback origin")
    if not _is_loopback(hostname):
        raise ValueError("web origin must be a canonical loopback origin")
    if not parsed.netloc or parsed.netloc != _canonical_netloc(parsed):
        raise ValueError("web origin must be a canonical loopback origin")
    # A path slash is not part of an HTTP Origin header; reject it so config
    # and request comparisons stay byte-for-byte exact.
    if value != f"{parsed.scheme}://{parsed.netloc}":
        raise ValueError("web origin must be a canonical loopback origin")
    return WebOrigin(scheme=parsed.scheme, netloc=parsed.netloc)


def require_same_origin(
    request: Request,
    settings: Settings,
    *,
    require_origin: bool,
) -> WebOrigin:
    """Require exact configured Origin when requested and exact configured Host."""

    origin = parse_web_origin(settings.web_origin)
    host = request.headers.get("host")
    if host != origin.netloc:
        raise RequestSecurityError("request host is not allowed")
    if require_origin and request.headers.get("origin") != origin.value:
        raise RequestSecurityError("request origin is not allowed")
    return origin


def set_session_cookie(response: Response, token: str, origin: WebOrigin) -> None:
    """Set the HttpOnly, strict session cookie with HTTPS-only Secure."""

    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=origin.scheme == "https",
        path="/",
    )


def clear_session_cookie(response: Response, origin: WebOrigin) -> None:
    """Clear the session cookie with the same security attributes."""

    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=origin.scheme == "https",
        httponly=True,
        samesite="strict",
    )


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _canonical_netloc(parsed: SplitResult) -> str:
    if parsed.hostname is None:
        return ""
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if parsed.port is None:
        return host
    return f"{host}:{parsed.port}"


__all__ = [
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "RequestSecurityError",
    "WebOrigin",
    "clear_session_cookie",
    "parse_web_origin",
    "require_same_origin",
    "set_session_cookie",
]
