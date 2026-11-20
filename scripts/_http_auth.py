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
    """Return a normalized HTTP origin, or None for malformed/non-HTTP URLs.

    URLs whose host Python and the WHATWG URL parser (which Claude's MCP
    transport uses) can read differently are refused: a backslash ends the
    authority for WHATWG but not for urlsplit, and userinfo, whitespace and
    control characters are where their cleanup rules differ.
    """
    if any(ch == "\\" or ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if "@" in parsed.netloc:
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


def _whatwg_ipv4_number(part: str) -> int | None:
    """One dotted part, read as the WHATWG IPv4 number parser reads it."""
    radix = 10
    if part[:2] in {"0x", "0X"}:
        part, radix = part[2:], 16
    elif len(part) > 1 and part.startswith("0"):
        part, radix = part[1:], 8
    if not part:
        return 0
    digits = "0123456789abcdef"[:radix]
    if any(ch not in digits for ch in part.lower()):
        return None
    return int(part, radix)


def _whatwg_ipv4_address(host: str) -> ipaddress.IPv4Address | None:
    """The address the WHATWG URL parser reads ``host`` as, or None.

    Follows the URL Standard's IPv4 parser: one to four dotted parts, each
    decimal, ``0x`` hex or leading-zero octal, an optional trailing dot, and
    a last part that fills the remaining bytes, so ``127.1`` is 127.0.0.1.
    Returns None for anything else, including hosts that WHATWG would reject
    outright; those are not loopback either way.
    """
    parts = host.split(".")
    if len(parts) > 1 and parts[-1] == "":
        parts.pop()
    if not 1 <= len(parts) <= 4 or any(part == "" for part in parts):
        return None
    numbers = [_whatwg_ipv4_number(part) for part in parts]
    if any(number is None for number in numbers):
        return None
    *leading, last = numbers
    if any(number > 255 for number in leading) or last >= 256 ** (5 - len(numbers)):
        return None
    value = last
    for index, number in enumerate(leading):
        value += number * 256 ** (3 - index)
    return ipaddress.IPv4Address(value)


def _is_whatwg_loopback_host(url: str) -> bool:
    """Whether the WHATWG URL parser resolves ``url``'s host to loopback.

    Claude's MCP transport parses the URL this way and connects to the
    normalized host, so IPv4 shorthand such as ``127.1`` reaches 127.0.0.1.
    Only plain ASCII hosts are read: percent-escapes and non-ASCII hosts go
    through decoding and Unicode mapping steps that Python does not share
    with WHATWG, so they are not treated as loopback.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
    except ValueError:
        return False
    if not host or not host.isascii() or "%" in host:
        return False
    host = host.lower()
    if host == "localhost":
        return True
    if parsed.netloc.startswith("["):
        try:
            return ipaddress.IPv6Address(host).is_loopback
        except ValueError:
            return False
    address = _whatwg_ipv4_address(host)
    return address is not None and address.is_loopback


def is_secure_governance_url(url: str) -> bool:
    """True when ``url`` is HTTPS, or cleartext HTTP that WHATWG reads as loopback.

    This models Claude's MCP transport, which parses URLs with the WHATWG
    parser. Python's own REST helpers use ``_validate_governance_transport``
    instead, which stays strict: urllib passes the host string to
    getaddrinfo, whose handling of IPv4 shorthand varies by platform.
    """
    origin = _http_origin(url)
    if origin is None:
        return False
    scheme, _host, _port = origin
    return scheme == "https" or (scheme == "http" and _is_whatwg_loopback_host(url))


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
