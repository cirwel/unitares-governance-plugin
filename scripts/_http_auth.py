"""Shared authentication headers for plugin-originated governance HTTP calls."""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ResponseTooLargeError(ValueError):
    """An HTTP peer exceeded the caller's bounded response budget."""


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


def authorization_safe_urlopen(
    request: urllib.request.Request,
    *,
    timeout: float,
) -> Any:
    """Open a request while refusing every cross-origin redirect."""
    return _AUTHORIZATION_SAFE_OPENER.open(request, timeout=timeout)


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
