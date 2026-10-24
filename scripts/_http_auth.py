"""Shared authentication headers for plugin-originated governance HTTP calls."""

from __future__ import annotations

import ipaddress
import os
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ResponseTooLargeError(ValueError):
    """An HTTP peer exceeded the caller's bounded response budget."""


class InsecureBearerTransportError(ValueError):
    """A bearer would cross a cleartext, non-loopback HTTP connection."""


def _http_origin(url: str) -> tuple[str, str, int] | None:
    """Return a normalized HTTP origin, or None for malformed/non-HTTP URLs."""
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host, port


class _AuthorizationSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only within the request's original HTTP origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        source = _http_origin(req.full_url)
        target = _http_origin(redirected.full_url)
        if source is None or target is None or source != target:
            return None
        return redirected


_AUTHORIZATION_SAFE_OPENER = urllib.request.build_opener(
    _AuthorizationSafeRedirectHandler()
)
_LOOPBACK_DIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _AuthorizationSafeRedirectHandler(),
)


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_governance_transport(
    request: urllib.request.Request,
) -> tuple[str, str, int]:
    origin = _http_origin(request.full_url)
    if origin is None:
        raise InsecureBearerTransportError("governance request URL is malformed")
    scheme, host, _port = origin
    if scheme == "https" or (scheme == "http" and _is_loopback_host(host)):
        return origin
    raise InsecureBearerTransportError(
        "refusing to send governance data over non-loopback HTTP"
    )


def _validate_bearer_transport(
    request: urllib.request.Request,
) -> tuple[str, str, int]:
    """Compatibility name for callers of the original transport validator."""
    return _validate_governance_transport(request)


def authorization_safe_urlopen(
    request: urllib.request.Request,
    *,
    timeout: float,
) -> Any:
    """Open a governance request with safe transport and redirects."""
    scheme, host, _port = _validate_governance_transport(request)
    opener = (
        _LOOPBACK_DIRECT_OPENER
        if scheme == "http" and _is_loopback_host(host)
        else _AUTHORIZATION_SAFE_OPENER
    )
    return opener.open(request, timeout=timeout)


def read_limited_response(
    response: Any,
    *,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> bytes:
    """Read at most ``max_bytes`` from an HTTP response, or fail closed."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    headers = getattr(response, "headers", None)
    raw_length = headers.get("Content-Length") if headers is not None else None
    if raw_length:
        try:
            declared = int(raw_length)
        except (TypeError, ValueError):
            declared = -1
        if declared > max_bytes:
            raise ResponseTooLargeError(
                f"HTTP response declares {declared} bytes; limit is {max_bytes}"
            )
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ResponseTooLargeError(
            f"HTTP response exceeded {max_bytes} bytes"
        )
    return body


def governance_json_headers() -> dict[str, str]:
    """Return JSON headers with the optional governance client bearer token."""
    headers = {"Content-Type": "application/json"}
    token = (os.environ.get("UNITARES_HTTP_API_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
