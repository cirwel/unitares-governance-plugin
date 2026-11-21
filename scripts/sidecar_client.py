#!/usr/bin/env python3
"""Stdlib client for the local UNITARES identity sidecar.

Proves the sidecar listener before presenting ``UNITARES_SIDECAR_CAPABILITY``:

* Over loopback TCP (``UNITARES_SIDECAR_URL``), performs a challenge-response
  ``GET /startup-proof`` HMAC check using ``UNITARES_SIDECAR_STARTUP_SECRET``
  on a single keep-alive connection and sends the capability request on that
  same connection (never reconnecting).
* Over a Unix domain socket (``UNITARES_SIDECAR_SOCKET``), validates the socket
  fail-closed (non-symlink, ``S_ISSOCK``, owned by the current UID, mode
  ``0o600`` / ``st_mode & 0o077 == 0``) before connecting over ``AF_UNIX``.

Usable both as an importable Python module and as a CLI:

    python3 scripts/sidecar_client.py call check_working_state
    python3 scripts/sidecar_client.py call knowledge --arguments '{"action":"search","query":"foo"}'
    python3 scripts/sidecar_client.py post /turn/checkin --data '{"response_text":"done","complexity":0.3}'
    python3 scripts/sidecar_client.py get /health
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import secrets
import socket
import stat
import struct
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

try:
    from ._http_auth import DEFAULT_MAX_RESPONSE_BYTES, read_limited_response
except ImportError:  # Executed directly or imported via scripts/ on sys.path.
    _scripts_dir = str(Path(__file__).resolve().parent)
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    from _http_auth import DEFAULT_MAX_RESPONSE_BYTES, read_limited_response


DEFAULT_SIDECAR_URL = "http://127.0.0.1:8768"
HTTP_TIMEOUT = 25.0
#: Wall-clock budget for the unauthenticated startup challenge.
PROOF_TIMEOUT = 0.5
PROOF_MAX_BYTES = 4096
#: Statuses that carry no body under HTTP; never a valid answer from these
#: JSON endpoints, and ``http.client`` treats their length as zero.
_BODYLESS_STATUSES = frozenset({204, 304})

#: The clock every exchange deadline is measured on (replaceable in tests).
_monotonic = time.monotonic


class SidecarClientError(RuntimeError):
    """A sidecar request could not complete safely."""


class SidecarUnavailable(SidecarClientError):
    """Nothing on the sidecar endpoint answered in time."""


class ListenerNotProven(SidecarClientError):
    """The sidecar endpoint failed transport or startup-secret authentication."""


def sidecar_bind(sidecar_url: str) -> tuple[str, int, str]:
    """Validate and split a loopback HTTP sidecar base URL."""
    base = sidecar_url.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if (
        parsed.scheme.lower() != "http"
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise SidecarClientError("--sidecar-url must be an HTTP base URL without a path")
    try:
        port = 80 if parsed.port is None else parsed.port
    except ValueError as exc:
        raise SidecarClientError(f"invalid sidecar port: {exc}") from exc
    host = parsed.hostname or ""
    if host == "localhost":
        bind_host = "127.0.0.1"
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise SidecarClientError(
                "--sidecar-url must use localhost or an IPv4 loopback address"
            ) from exc
        if address.version != 4 or not address.is_loopback:
            raise SidecarClientError(
                "--sidecar-url must use localhost or an IPv4 loopback address"
            )
        bind_host = host
    if not 1 <= port <= 65535:
        raise SidecarClientError("--sidecar-url port must be between 1 and 65535")
    return bind_host, port, base


def _current_effective_uid() -> int | None:
    geteuid = getattr(os, "geteuid", getattr(os, "getuid", None))
    return geteuid() if callable(geteuid) else None


def _verify_unix_peer_uid(sock: socket.socket) -> None:
    """Verify the connected AF_UNIX peer's effective UID before sending secrets."""
    expected_uid = _current_effective_uid()
    if expected_uid is None:
        raise SidecarClientError(
            "cannot determine current UID for Unix socket peer verification"
        )
    if sys.platform == "darwin":
        sol_local = getattr(socket, "SOL_LOCAL", 0)
        local_peercred = getattr(socket, "LOCAL_PEERCRED", 0x001)
        try:
            raw = sock.getsockopt(sol_local, local_peercred, struct.calcsize("=IIh2x16I"))
        except OSError as exc:
            raise ListenerNotProven(
                f"could not verify sidecar Unix socket peer credentials: {exc}"
            ) from exc
        cr_version, peer_uid = struct.unpack_from("=II", raw, 0)
        if cr_version != 0 or peer_uid != expected_uid:
            raise ListenerNotProven(
                "sidecar Unix socket peer UID does not match the current user"
            )
        return
    so_peercred = getattr(
        socket, "SO_PEERCRED", 17 if sys.platform.startswith("linux") else None
    )
    if so_peercred is not None:
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("=iII"))
        except OSError as exc:
            raise ListenerNotProven(
                f"could not verify sidecar Unix socket peer credentials: {exc}"
            ) from exc
        _pid, peer_uid, _gid = struct.unpack("=iII", raw)
        if peer_uid != expected_uid:
            raise ListenerNotProven(
                "sidecar Unix socket peer UID does not match the current user"
            )
        return
    raise SidecarClientError(
        "Unix socket peer credential verification is unavailable on this platform"
    )


def validate_unix_socket(socket_path: str) -> str:
    """Validate a sidecar Unix domain socket fail-closed before connecting.

    Raises ``SidecarUnavailable`` if the socket (or its parent directory) does
    not yet exist, and ``ListenerNotProven`` if the path is relative, contains
    ``..``, is a symlink, is not a socket, is owned by another user, is
    accessible to group/other users (``st_mode & 0o077 != 0``), or sits inside
    a symlinked or group/world-writable non-sticky parent directory.
    """
    if not hasattr(socket, "AF_UNIX"):
        raise SidecarClientError("Unix domain sockets are not supported on this platform")
    cleaned = socket_path.strip()
    if not cleaned or "\x00" in cleaned:
        raise ListenerNotProven("sidecar Unix socket path is invalid")
    expanded = os.path.expanduser(cleaned)
    if not os.path.isabs(expanded) or ".." in Path(expanded).parts:
        raise ListenerNotProven(
            f"sidecar Unix socket path must be absolute without '..' segments: {socket_path!r}"
        )
    parent = os.path.dirname(expanded)
    try:
        parent_info = os.lstat(parent)
    except FileNotFoundError as exc:
        raise SidecarUnavailable(f"unix://{expanded} is unavailable: {exc}") from exc
    except OSError as exc:
        raise ListenerNotProven(
            f"cannot inspect sidecar Unix socket parent {parent}: {exc}"
        ) from exc
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise ListenerNotProven(
            f"sidecar Unix socket parent must be a real directory: {parent}"
        )
    if (parent_info.st_mode & 0o022) and not (parent_info.st_mode & stat.S_ISVTX):
        raise ListenerNotProven(
            f"sidecar Unix socket parent directory is group- or world-writable without sticky bit: {parent}"
        )
    try:
        info = os.lstat(expanded)
    except FileNotFoundError as exc:
        raise SidecarUnavailable(f"unix://{expanded} is unavailable: {exc}") from exc
    except OSError as exc:
        raise ListenerNotProven(
            f"cannot inspect sidecar Unix socket {expanded}: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise ListenerNotProven(f"sidecar Unix socket must not be a symlink: {expanded}")
    if not stat.S_ISSOCK(info.st_mode):
        raise ListenerNotProven(f"sidecar Unix socket path is not a socket: {expanded}")
    expected_uid = _current_effective_uid()
    if expected_uid is None or info.st_uid != expected_uid:
        raise ListenerNotProven(
            f"sidecar Unix socket is not owned by the current user: {expanded}"
        )
    if info.st_mode & 0o077:
        raise ListenerNotProven(
            f"sidecar Unix socket must not have group or world permissions: {expanded}"
        )
    return expanded


class _UnixHTTPConnection(http.client.HTTPConnection):
    """Stdlib HTTPConnection over a fail-closed validated AF_UNIX socket."""

    def __init__(self, socket_path: str, *, timeout: float | None = None) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        validated = validate_unix_socket(self._socket_path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if self.timeout is not None:
                sock.settimeout(self.timeout)
            sock.connect(validated)
            _verify_unix_peer_uid(sock)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def _normalize_request_path(path: str) -> str:
    cleaned = path.strip()
    if not cleaned or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in cleaned):
        raise SidecarClientError(f"invalid sidecar request path: {path!r}")
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return cleaned


def _require_declared_length(response: http.client.HTTPResponse) -> None:
    """Refuse a response whose body is not delimited by an explicit length."""
    if 100 <= response.status < 200 or response.status in _BODYLESS_STATUSES:
        raise http.client.HTTPException(
            f"HTTP {response.status} is not a JSON response"
        )
    if response.getheader("Transfer-Encoding") is not None:
        raise http.client.HTTPException("Transfer-Encoding is not accepted")
    if response.getheader("Content-Length") is None:
        raise http.client.HTTPException("Content-Length is required")


def _exchange(
    connection: http.client.HTTPConnection,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    seconds: float,
    max_bytes: int,
    body: bytes | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[http.client.HTTPResponse, bytes]:
    """Send one request and read its whole response within ``seconds``.

    The socket timeout bounds each read, not the exchange: a peer that keeps
    sending a byte at a time would otherwise hold the client indefinitely, so a
    watchdog shuts the socket down when the wall clock runs out. That shutdown
    can end a read early with whatever bytes had arrived, so the response is
    accepted only if it completed before the deadline (checked on the clock
    itself, not only by whether the watchdog ran) and the body is exactly an
    explicit ``Content-Length``: no chunked encoding and no bodyless status.
    Raises ``TimeoutError`` when the time ran out and ``OSError``,
    ``http.client.HTTPException`` or ``ValueError`` when the exchange failed
    or the response was malformed or incomplete.
    """
    now = _monotonic if clock is None else clock
    deadline = now() + max(0.0, seconds)
    expired = threading.Event()
    sock = connection.sock

    def expire() -> None:
        expired.set()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(max(0.0, seconds), expire)
    timer.daemon = True
    timer.start()
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        _require_declared_length(response)
        raw = read_limited_response(response, max_bytes=max_bytes)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        if expired.is_set():
            raise TimeoutError(f"no response within {seconds}s") from exc
        raise
    finally:
        timer.cancel()
    # A delayed watchdog can lose the race with a late last byte, so the
    # deadline is also checked here, after the exchange finished.
    if expired.is_set() or now() >= deadline:
        raise TimeoutError(f"no response within {seconds}s")
    # http.client counts down the declared length as it reads; anything but
    # zero is a body cut short, or one with no declared length at all.
    if response.length != 0:
        raise http.client.IncompleteRead(raw, response.length)
    return response, raw


def _prove_listener(
    connection: http.client.HTTPConnection,
    url: str,
    startup_secret: str,
    *,
    clock: Callable[[], float] | None = None,
) -> None:
    """Challenge the listener and leave the proven connection open."""
    challenge = secrets.token_urlsafe(32)
    query = urllib.parse.urlencode({"challenge": challenge})
    try:
        response, raw = _exchange(
            connection,
            "GET",
            f"/startup-proof?{query}",
            headers={"Accept": "application/json", "Connection": "keep-alive"},
            seconds=PROOF_TIMEOUT,
            max_bytes=PROOF_MAX_BYTES,
            clock=clock,
        )
    except TimeoutError as exc:
        raise SidecarUnavailable(
            f"{url} did not answer the startup challenge within {PROOF_TIMEOUT}s"
        ) from exc
    except OSError as exc:
        # Refused, reset or closed before any answer.
        raise SidecarUnavailable(f"{url} is unavailable: {exc}") from exc
    except (http.client.HTTPException, ValueError) as exc:
        raise ListenerNotProven(
            f"{url} returned a malformed startup-proof response: "
            f"{type(exc).__name__}"
        ) from exc
    if response.status == 503:
        raise SidecarUnavailable(
            f"{url} is temporarily unavailable (HTTP 503) during startup challenge"
        )
    if response.status != 200:
        raise ListenerNotProven(
            f"{url} answered the startup challenge with HTTP {response.status}"
        )
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ListenerNotProven(f"{url} returned an invalid startup proof") from exc
    proof = decoded.get("proof") if isinstance(decoded, dict) else None
    expected = hmac.new(
        startup_secret.encode("utf-8"),
        challenge.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not (
        isinstance(decoded, dict)
        and decoded.get("success") is True
        and isinstance(proof, str)
        and proof.isascii()
        and hmac.compare_digest(proof, expected)
    ):
        raise ListenerNotProven(f"{url} did not prove the startup secret")
    if response.will_close or connection.sock is None:
        raise ListenerNotProven(
            f"{url} proved the startup secret but closed the connection; "
            "a sidecar started before this u-run version must be restarted"
        )


def proven_exchange(
    sidecar_url: str | None = None,
    startup_secret: str | None = None,
    *,
    path: str,
    capability: str,
    payload: dict[str, Any] | None = None,
    slot: str = "",
    timeout: float = HTTP_TIMEOUT,
    sidecar_socket: str | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Authenticate the sidecar transport, then send one capability request.

    * When ``sidecar_socket`` is set, validates the Unix domain socket
      fail-closed (non-symlink, ``S_ISSOCK``, owned by current UID, mode
      ``0o600``) and connects over ``AF_UNIX``. If ``startup_secret`` is also
      supplied, runs ``/startup-proof`` on that connection before the request.
    * Otherwise connects over loopback TCP to ``sidecar_url`` and requires a
      valid ``/startup-proof`` HMAC response for ``startup_secret`` on the same
      keep-alive connection before sending ``capability``.

    Raises ``SidecarUnavailable`` when nothing answered in time,
    ``ListenerNotProven`` when transport or startup-secret verification failed,
    and ``SidecarClientError`` when the request itself failed.
    """
    normalized_path = _normalize_request_path(path)
    if not capability:
        raise SidecarClientError("UNITARES_SIDECAR_CAPABILITY is required")

    socket_value = (sidecar_socket or "").strip()
    if socket_value:
        validated_socket = validate_unix_socket(socket_value)
        url = f"unix://{validated_socket}{normalized_path}"
        connection: http.client.HTTPConnection = _UnixHTTPConnection(
            validated_socket,
            timeout=PROOF_TIMEOUT if startup_secret else timeout,
        )
        require_proof = bool(startup_secret)
    else:
        if not sidecar_url:
            raise SidecarClientError(
                "UNITARES_SIDECAR_URL or UNITARES_SIDECAR_SOCKET is required"
            )
        host, port, base = sidecar_bind(sidecar_url)
        url = f"{base}{normalized_path}"
        if not startup_secret:
            raise ListenerNotProven(
                f"{url}: UNITARES_SIDECAR_STARTUP_SECRET is required before "
                "sending capability over loopback TCP"
            )
        connection = http.client.HTTPConnection(host, port, timeout=PROOF_TIMEOUT)
        require_proof = True

    try:
        try:
            connection.connect()
        except SidecarClientError:
            raise
        except OSError as exc:
            raise SidecarUnavailable(f"{url} is unavailable: {exc}") from exc
        # Never reconnect: a fresh connection could reach a different listener
        # than the one that was just verified.
        connection.auto_open = 0
        if require_proof:
            assert startup_secret is not None
            _prove_listener(connection, url, startup_secret, clock=clock)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {capability}",
            "Connection": "close",
        }
        if slot:
            headers["X-UNITARES-Slot"] = slot
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
            response, raw = _exchange(
                connection,
                "POST" if payload is not None else "GET",
                normalized_path,
                headers=headers,
                body=body,
                seconds=timeout,
                max_bytes=DEFAULT_MAX_RESPONSE_BYTES,
                clock=clock,
            )
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise SidecarClientError(f"{url} failed: {exc}") from exc
    finally:
        connection.close()
    if response.status == 503:
        detail = raw[:300].decode("utf-8", errors="replace")
        raise SidecarUnavailable(f"{url} returned HTTP 503: {detail}")
    if not 200 <= response.status < 300:
        detail = raw[:300].decode("utf-8", errors="replace")
        raise SidecarClientError(f"{url} returned HTTP {response.status}: {detail}")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise SidecarClientError(f"{url} returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise SidecarClientError(f"{url} returned a non-object JSON response")
    return decoded


_proven_exchange = proven_exchange


class SidecarClient:
    """Reusable client for a child process talking to the UNITARES sidecar."""

    def __init__(
        self,
        *,
        capability: str,
        sidecar_url: str | None = None,
        sidecar_socket: str | None = None,
        startup_secret: str | None = None,
        slot: str = "",
        timeout: float = HTTP_TIMEOUT,
    ) -> None:
        self.capability = capability.strip()
        self.sidecar_socket = (sidecar_socket or "").strip() or None
        self.sidecar_url = (sidecar_url or DEFAULT_SIDECAR_URL).strip()
        self.startup_secret = (startup_secret or "").strip() or None
        self.slot = slot.strip()
        self.timeout = timeout

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        sidecar_url: str | None = None,
        sidecar_socket: str | None = None,
        slot: str | None = None,
        timeout: float = HTTP_TIMEOUT,
    ) -> SidecarClient:
        source = os.environ if env is None else env
        if sidecar_socket is not None:
            resolved_socket = sidecar_socket.strip()
        elif sidecar_url is not None:
            resolved_socket = ""
        else:
            resolved_socket = (source.get("UNITARES_SIDECAR_SOCKET") or "").strip()
        resolved_url = (
            sidecar_url
            if sidecar_url is not None
            else (source.get("UNITARES_SIDECAR_URL") or DEFAULT_SIDECAR_URL)
        ).strip()
        resolved_slot = (
            slot if slot is not None else (source.get("UNITARES_SIDECAR_SLOT") or "")
        ).strip()
        capability = (source.get("UNITARES_SIDECAR_CAPABILITY") or "").strip()
        startup_secret = (
            None
            if resolved_socket
            else (source.get("UNITARES_SIDECAR_STARTUP_SECRET") or "").strip() or None
        )
        return cls(
            capability=capability,
            sidecar_url=resolved_url,
            sidecar_socket=resolved_socket or None,
            startup_secret=startup_secret,
            slot=resolved_slot,
            timeout=timeout,
        )

    def get(self, path: str, *, timeout: float | None = None) -> dict[str, Any]:
        return proven_exchange(
            self.sidecar_url,
            self.startup_secret,
            path=path,
            capability=self.capability,
            payload=None,
            slot=self.slot,
            timeout=self.timeout if timeout is None else timeout,
            sidecar_socket=self.sidecar_socket,
        )

    def post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return proven_exchange(
            self.sidecar_url,
            self.startup_secret,
            path=path,
            capability=self.capability,
            payload={} if payload is None else payload,
            slot=self.slot,
            timeout=self.timeout if timeout is None else timeout,
            sidecar_socket=self.sidecar_socket,
        )

    def call_tool(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        name = tool.strip()
        if not name:
            raise SidecarClientError("tool name must not be empty")
        return self.post(
            "/v1/tools/call",
            {"name": name, "arguments": {} if arguments is None else arguments},
            timeout=timeout,
        )

    def health(self, *, timeout: float | None = None) -> dict[str, Any]:
        return self.get("/health", timeout=timeout)


def _parse_json_object(raw: str, *, flag: str) -> dict[str, Any]:
    text = sys.stdin.read() if raw == "-" else raw
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{flag} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{flag} must decode to a JSON object")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sidecar-client",
        description="Authenticated client for the local UNITARES identity sidecar.",
    )
    parser.add_argument(
        "--sidecar-url",
        default=None,
        help="Loopback sidecar HTTP base URL (defaults to UNITARES_SIDECAR_URL).",
    )
    parser.add_argument(
        "--sidecar-socket",
        default=None,
        help="Sidecar Unix domain socket path (defaults to UNITARES_SIDECAR_SOCKET).",
    )
    parser.add_argument(
        "--slot",
        default=None,
        help="Optional sidecar slot header (defaults to UNITARES_SIDECAR_SLOT).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=HTTP_TIMEOUT,
        help="Wall-clock request timeout in seconds.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    call_parser = subparsers.add_parser(
        "call",
        help="Invoke a UNITARES tool via POST /v1/tools/call.",
    )
    call_parser.add_argument("tool", help="UNITARES tool name (e.g. check_working_state).")
    call_parser.add_argument(
        "--arguments",
        "--args",
        dest="arguments",
        default="{}",
        help="JSON object of tool arguments, or '-' to read from stdin.",
    )

    post_parser = subparsers.add_parser(
        "post",
        help="Send a JSON POST request to a sidecar endpoint (e.g. /turn/checkin).",
    )
    post_parser.add_argument("path", help="Sidecar HTTP path (e.g. /turn/checkin).")
    post_parser.add_argument(
        "--data",
        "--body",
        dest="data",
        default="{}",
        help="JSON object request body, or '-' to read from stdin.",
    )

    get_parser = subparsers.add_parser(
        "get",
        help="Send a JSON GET request to a sidecar endpoint (e.g. /health).",
    )
    get_parser.add_argument("path", nargs="?", default="/health", help="Sidecar HTTP path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = SidecarClient.from_env(
        sidecar_url=args.sidecar_url,
        sidecar_socket=args.sidecar_socket,
        slot=args.slot,
        timeout=args.timeout,
    )
    try:
        if args.command == "call":
            try:
                arguments = _parse_json_object(args.arguments, flag="--arguments")
            except ValueError as exc:
                parser.error(str(exc))
            response = client.call_tool(args.tool, arguments)
        elif args.command == "post":
            try:
                payload = _parse_json_object(args.data, flag="--data")
            except ValueError as exc:
                parser.error(str(exc))
            response = client.post(args.path, payload)
        elif args.command == "get":
            response = client.get(args.path)
        else:  # pragma: no cover
            parser.error(f"unknown command: {args.command}")
            return 2
    except SidecarClientError as exc:
        print(f"sidecar-client: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(response, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
