#!/usr/bin/env python3
"""Run a plain CLI under one sidecar-managed UNITARES process session."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from _http_auth import DEFAULT_MAX_RESPONSE_BYTES, read_limited_response


DEFAULT_SERVER_URL = "http://localhost:8767"
DEFAULT_SIDECAR_URL = "http://127.0.0.1:8768"
DEFAULT_MODEL_TYPE = "u-run"
STARTUP_TIMEOUT = 10.0
HTTP_TIMEOUT = 25.0
#: Wall-clock budget for the unauthenticated startup challenge.
PROOF_TIMEOUT = 0.5
PROOF_MAX_BYTES = 4096
#: Wall-clock budget for the local /health read on a proven connection.
HEALTH_TIMEOUT = 2.0
#: Statuses that carry no body under HTTP; never a valid answer from these
#: JSON endpoints, and ``http.client`` treats their length as zero.
_BODYLESS_STATUSES = frozenset({204, 304})

#: The clock every exchange deadline is measured on (replaceable in tests).
_monotonic = time.monotonic


class URunError(RuntimeError):
    """The wrapper could not establish its local lifecycle contract."""


class SidecarUnavailable(URunError):
    """Nothing on the sidecar port answered the startup challenge in time."""


class ListenerNotProven(URunError):
    """A listener answered on the sidecar port without proving the startup secret."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="u-run",
        description="Run one CLI process with sidecar-managed UNITARES lifecycle events.",
        usage="u-run [options] -- <agent command> [args ...]",
    )
    parser.add_argument(
        "--class",
        dest="agent_class",
        metavar="X",
        help=(
            "Explicit calibration/model class. It is never inferred from the "
            f"command; the neutral default is {DEFAULT_MODEL_TYPE!r}."
        ),
    )
    parser.add_argument(
        "--server-url",
        default=os.environ.get("UNITARES_SERVER_URL", DEFAULT_SERVER_URL),
        help="Upstream UNITARES server URL.",
    )
    parser.add_argument(
        "--sidecar-url",
        default=os.environ.get("UNITARES_SIDECAR_URL", DEFAULT_SIDECAR_URL),
        help="Local identity sidecar base URL.",
    )
    parser.add_argument(
        "--slot",
        default="",
        help="Explicit sidecar slot; omitted creates a unique slot for this invocation.",
    )
    parser.add_argument(
        "--workspace",
        default=os.getcwd(),
        help="Workspace in which the slot-scoped session cache is stored.",
    )
    return parser


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = build_parser()
    if "--" not in argv:
        if any(value in {"-h", "--help"} for value in argv):
            parser.parse_args(argv)
        parser.error("missing '--' before the child command")
    separator = argv.index("--")
    args = parser.parse_args(argv[:separator])
    command = argv[separator + 1 :]
    if not command:
        parser.error("a child command is required after '--'")
    if args.agent_class is not None and not args.agent_class.strip():
        parser.error("--class must not be empty")
    return args, command


def _sidecar_bind(sidecar_url: str) -> tuple[str, int, str]:
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
        raise URunError("--sidecar-url must be an HTTP base URL without a path")
    try:
        port = 80 if parsed.port is None else parsed.port
    except ValueError as exc:
        raise URunError(f"invalid sidecar port: {exc}") from exc
    host = parsed.hostname or ""
    if host == "localhost":
        bind_host = "127.0.0.1"
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise URunError(
                "--sidecar-url must use localhost or an IPv4 loopback address"
            ) from exc
        if address.version != 4 or not address.is_loopback:
            raise URunError(
                "--sidecar-url must use localhost or an IPv4 loopback address"
            )
        bind_host = host
    if not 1 <= port <= 65535:
        raise URunError("--sidecar-url port must be between 1 and 65535")
    return bind_host, port, base


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
) -> tuple[http.client.HTTPResponse, bytes]:
    """Send one request and read its whole response within ``seconds``.

    The socket timeout bounds each read, not the exchange: a peer that keeps
    sending a byte at a time would otherwise hold u-run indefinitely, so a
    watchdog shuts the socket down when the wall clock runs out. That shutdown
    can end a read early with whatever bytes had arrived, so the response is
    accepted only if it completed before the deadline (checked on the clock
    itself, not only by whether the watchdog ran) and the body is exactly an
    explicit ``Content-Length``: no chunked encoding and no bodyless status.
    Raises ``TimeoutError`` when the time ran out and ``OSError``,
    ``http.client.HTTPException`` or ``ValueError`` when the exchange failed
    or the response was malformed or incomplete.
    """
    deadline = _monotonic() + max(0.0, seconds)
    expired = threading.Event()
    sock = connection.sock

    def expire() -> None:
        expired.set()
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
    if expired.is_set() or _monotonic() >= deadline:
        raise TimeoutError(f"no response within {seconds}s")
    # http.client counts down the declared length as it reads; anything but
    # zero is a body cut short, or one with no declared length at all.
    if response.length != 0:
        raise http.client.IncompleteRead(raw, response.length)
    return response, raw


def _prove_listener(
    connection: http.client.HTTPConnection, url: str, startup_secret: str
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


def _proven_exchange(
    sidecar_url: str,
    startup_secret: str,
    *,
    path: str,
    capability: str,
    payload: dict[str, Any] | None = None,
    slot: str = "",
    timeout: float = HTTP_TIMEOUT,
) -> dict[str, Any]:
    """Prove the listener, then send one capability request on that connection.

    A connection stays with the process that accepted it, so a process that
    binds the port after the proof never receives the capability. Raises
    ``SidecarUnavailable`` when nothing answered the challenge,
    ``ListenerNotProven`` when a listener answered without proving the startup
    secret, and ``URunError`` when the proven request itself fails.
    """
    host, port, base = _sidecar_bind(sidecar_url)
    url = f"{base}{path}"
    connection = http.client.HTTPConnection(host, port, timeout=PROOF_TIMEOUT)
    try:
        try:
            connection.connect()
        except OSError as exc:
            raise SidecarUnavailable(f"{url} is unavailable: {exc}") from exc
        # Never reconnect: a fresh connection would reach whatever holds the
        # port now, not the listener that just proved itself.
        connection.auto_open = 0
        _prove_listener(connection, url, startup_secret)
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
            connection.sock.settimeout(timeout)
            response, raw = _exchange(
                connection,
                "POST" if payload is not None else "GET",
                path,
                headers=headers,
                body=body,
                seconds=timeout,
                max_bytes=DEFAULT_MAX_RESPONSE_BYTES,
            )
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise URunError(f"{url} failed: {exc}") from exc
    finally:
        connection.close()
    if not 200 <= response.status < 300:
        detail = raw[:300].decode("utf-8", errors="replace")
        raise URunError(f"{url} returned HTTP {response.status}: {detail}")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise URunError(f"{url} returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise URunError(f"{url} returned a non-object JSON response")
    return decoded


def _validate_health(
    health: dict[str, Any],
    *,
    server_url: str,
    workspace: Path,
    capability: str,
    slot: str,
) -> None:
    if health.get("success") is not True:
        raise URunError("sidecar health response did not report success")
    expected_capability_id = hashlib.sha256(capability.encode("utf-8")).hexdigest()
    capability_id = health.get("capability_id")
    if not (
        isinstance(capability_id, str)
        and capability_id.isascii()
        and secrets.compare_digest(capability_id, expected_capability_id)
    ):
        raise URunError("sidecar health response did not prove the configured capability")
    reported_server = str(health.get("server_url") or "").rstrip("/")
    if reported_server != server_url.rstrip("/"):
        raise URunError(
            "running sidecar targets a different UNITARES server "
            f"({reported_server or '<missing>'})"
        )
    reported_workspace = str(health.get("workspace") or "")
    if not reported_workspace or Path(reported_workspace).expanduser().resolve() != workspace:
        raise URunError(
            "running sidecar targets a different workspace "
            f"({reported_workspace or '<missing>'})"
        )
    reported_slot = str(health.get("default_slot") or "")
    if reported_slot != slot:
        raise URunError(
            "running sidecar is bound to a different slot "
            f"({reported_slot or '<missing>'})"
        )


def _process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _stop_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            return
        process.wait(timeout=5)


def _proven_health(
    sidecar_url: str, startup_secret: str, capability: str
) -> dict[str, Any] | None:
    """Return /health from a proven listener, or None when nothing answered."""
    try:
        return _proven_exchange(
            sidecar_url,
            startup_secret,
            path="/health",
            capability=capability,
            timeout=HEALTH_TIMEOUT,
        )
    except SidecarUnavailable:
        return None
    except ListenerNotProven as exc:
        raise URunError(f"sidecar listener failed startup authentication: {exc}") from exc


def ensure_sidecar(
    *,
    sidecar_url: str,
    server_url: str,
    workspace: Path,
    slot: str,
    model_type: str,
    capability: str,
    startup_secret: str,
    reuse_existing: bool,
) -> subprocess.Popen[Any] | None:
    host, port, sidecar_url = _sidecar_bind(sidecar_url)
    if reuse_existing:
        health = _proven_health(sidecar_url, startup_secret, capability)
        if health is not None:
            _validate_health(
                health,
                server_url=server_url,
                workspace=workspace,
                capability=capability,
                slot=slot,
            )
            return None

    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("identity_sidecar.py")),
        "--host",
        host,
        "--port",
        str(port),
        "--server-url",
        server_url,
        "--workspace",
        str(workspace),
        "--slot",
        slot,
        "--model-type",
        model_type,
    ]
    try:
        sidecar_environment = os.environ.copy()
        sidecar_environment["UNITARES_SIDECAR_CAPABILITY"] = capability
        sidecar_environment["UNITARES_SIDECAR_STARTUP_SECRET"] = startup_secret
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            close_fds=True,
            cwd=str(Path.home()),
            env=sidecar_environment,
            **_process_group_kwargs(),
        )
    except OSError as exc:
        raise URunError(f"could not start identity sidecar: {exc}") from exc

    deadline = time.monotonic() + STARTUP_TIMEOUT
    try:
        while time.monotonic() < deadline:
            health = _proven_health(sidecar_url, startup_secret, capability)
            if health is not None:
                _validate_health(
                    health,
                    server_url=server_url,
                    workspace=workspace,
                    capability=capability,
                    slot=slot,
                )
                # Another wrapper may have won the bind race. In that case our
                # failed process is not an owned, running sidecar.
                return process if process.poll() is None else None
            return_code = process.poll()
            if return_code is not None:
                raise URunError(
                    f"identity sidecar exited during startup with code {return_code}"
                )
            time.sleep(0.05)
        raise URunError("timed out waiting for identity sidecar health")
    except Exception:
        _stop_process(process)
        raise


def _start_session(
    *,
    sidecar_url: str,
    startup_secret: str,
    slot: str,
    model_type: str,
    capability: str,
) -> None:
    response = _proven_exchange(
        sidecar_url,
        startup_secret,
        path="/session/start",
        # u-run owns the session it starts, including on a reused --slot.
        payload={"slot": slot, "model_type": model_type, "replace_existing": True},
        slot=slot,
        capability=capability,
    )
    result = response.get("result")
    if (
        response.get("success") is not True
        or not isinstance(result, dict)
        or result.get("status") != "ok"
    ):
        detail = result.get("error") if isinstance(result, dict) else response.get("error")
        raise URunError(f"sidecar could not start a UNITARES session: {detail or response}")


def exit_complexity(wall_seconds: float, exit_code: int) -> float:
    """Bound an objective duration/failure proxy on the existing 0..0.85 scale."""
    duration_component = min(max(0.0, wall_seconds) / 3600.0, 1.0) * 0.35
    failure_component = 0.20 if exit_code != 0 else 0.0
    return round(min(0.85, 0.30 + duration_component + failure_component), 3)


def _exit_summary(return_code: int, exit_code: int, wall_seconds: float) -> str:
    if return_code < 0:
        return (
            f"u-run observed child termination by signal {-return_code} "
            f"(shell exit code {exit_code}) after {wall_seconds:.3f} seconds."
        )
    return f"u-run observed child exit code {exit_code} after {wall_seconds:.3f} seconds."


def _submit_exit_checkin(
    *,
    sidecar_url: str,
    startup_secret: str,
    slot: str,
    return_code: int,
    exit_code: int,
    wall_seconds: float,
    capability: str,
) -> None:
    # The child may run for hours and the listener may have changed hands;
    # the check-in travels on a connection that has just proved itself.
    response = _proven_exchange(
        sidecar_url,
        startup_secret,
        path="/turn/stop",
        payload={
            "slot": slot,
            "event": "u_run_exit",
            "response_text": _exit_summary(return_code, exit_code, wall_seconds),
            "complexity": exit_complexity(wall_seconds, exit_code),
            "epistemic_class": "substrate_interpretation",
        },
        slot=slot,
        capability=capability,
    )
    if response.get("success") is not True:
        raise URunError(f"exit check-in was not accepted: {response}")


def _shell_exit_code(return_code: int) -> int:
    return 128 + (-return_code) if return_code < 0 else return_code


def _forward_to_child(child: subprocess.Popen[Any], signum: int) -> None:
    try:
        if os.name == "nt":
            child.send_signal(signum)
        else:
            os.killpg(child.pid, signum)
    except (OSError, ProcessLookupError):
        pass


@contextmanager
def _forward_signals(child: subprocess.Popen[Any]) -> Iterator[None]:
    previous: dict[int, Any] = {}

    def forward(signum: int, _frame: Any) -> None:
        _forward_to_child(child, signum)

    forwarded = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        forwarded.append(signal.SIGHUP)
    try:
        for signum in forwarded:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, forward)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _run_child(command: list[str], environment: dict[str, str]) -> tuple[int, int, float]:
    started = time.monotonic()
    try:
        child = subprocess.Popen(command, env=environment, **_process_group_kwargs())
    except OSError as exc:
        print(f"u-run: could not start child command: {exc}", file=sys.stderr)
        return 127, 127, time.monotonic() - started
    with _forward_signals(child):
        return_code = child.wait()
    return return_code, _shell_exit_code(return_code), time.monotonic() - started


def main(argv: list[str] | None = None) -> int:
    args, command = parse_args(list(sys.argv[1:] if argv is None else argv))
    workspace = Path(args.workspace).expanduser().resolve()
    server_url = args.server_url.rstrip("/")
    if not server_url:
        print("u-run: --server-url must not be empty", file=sys.stderr)
        return 125
    model_type = args.agent_class.strip() if args.agent_class else DEFAULT_MODEL_TYPE
    slot = args.slot.strip() or f"u-run-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    configured_capability = (os.environ.get("UNITARES_SIDECAR_CAPABILITY") or "").strip()
    configured_startup_secret = (
        os.environ.get("UNITARES_SIDECAR_STARTUP_SECRET") or ""
    ).strip()
    capability = configured_capability or secrets.token_urlsafe(32)
    startup_secret = configured_startup_secret or secrets.token_urlsafe(32)
    owned_sidecar: subprocess.Popen[Any] | None = None

    try:
        _host, _port, sidecar_url = _sidecar_bind(args.sidecar_url)
        owned_sidecar = ensure_sidecar(
            sidecar_url=sidecar_url,
            server_url=server_url,
            workspace=workspace,
            slot=slot,
            model_type=model_type,
            capability=capability,
            startup_secret=startup_secret,
            reuse_existing=bool(configured_capability and configured_startup_secret),
        )
        _start_session(
            sidecar_url=sidecar_url,
            startup_secret=startup_secret,
            slot=slot,
            model_type=model_type,
            capability=capability,
        )
        environment = os.environ.copy()
        environment.update(
            {
                "UNITARES_SERVER_URL": server_url,
                "UNITARES_SIDECAR_URL": sidecar_url,
                "UNITARES_SIDECAR_SLOT": slot,
                "UNITARES_SIDECAR_CAPABILITY": capability,
                "UNITARES_MODEL_TYPE": model_type,
            }
        )
        return_code, exit_code, wall_seconds = _run_child(command, environment)
        try:
            _submit_exit_checkin(
                sidecar_url=sidecar_url,
                startup_secret=startup_secret,
                slot=slot,
                return_code=return_code,
                exit_code=exit_code,
                wall_seconds=wall_seconds,
                capability=capability,
            )
        except Exception as exc:
            # Lifecycle telemetry must not replace the wrapped command's exit
            # status, whatever the listener sent back. The warning remains
            # visible to the operator.
            print(f"u-run: warning: exit check-in skipped: {exc}", file=sys.stderr)
        return exit_code
    except URunError as exc:
        print(f"u-run: {exc}", file=sys.stderr)
        return 125
    finally:
        _stop_process(owned_sidecar)


if __name__ == "__main__":
    raise SystemExit(main())
