#!/usr/bin/env python3
"""Run a plain CLI under one sidecar-managed UNITARES process session."""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    from .sidecar_client import (
        DEFAULT_SIDECAR_URL,
        HTTP_TIMEOUT,
        ListenerNotProven,
        PROOF_MAX_BYTES as PROOF_MAX_BYTES,
        PROOF_TIMEOUT as PROOF_TIMEOUT,
        SidecarClientError as URunError,
        SidecarUnavailable,
        _exchange as _exchange,
        _prove_listener as _prove_listener,
        _require_declared_length as _require_declared_length,
        proven_exchange as _sidecar_proven_exchange,
        sidecar_bind as _sidecar_bind,
    )
except ImportError:  # Executed directly or via scripts/ on sys.path.
    from sidecar_client import (
        DEFAULT_SIDECAR_URL,
        HTTP_TIMEOUT,
        ListenerNotProven,
        PROOF_MAX_BYTES as PROOF_MAX_BYTES,
        PROOF_TIMEOUT as PROOF_TIMEOUT,
        SidecarClientError as URunError,
        SidecarUnavailable,
        _exchange as _exchange,
        _prove_listener as _prove_listener,
        _require_declared_length as _require_declared_length,
        proven_exchange as _sidecar_proven_exchange,
        sidecar_bind as _sidecar_bind,
    )


DEFAULT_SERVER_URL = "http://localhost:8767"
DEFAULT_MODEL_TYPE = "u-run"
STARTUP_TIMEOUT = 10.0
#: Wall-clock budget for the local /health read on a proven connection.
HEALTH_TIMEOUT = 2.0

#: The clock every exchange deadline is measured on (replaceable in tests).
_monotonic = time.monotonic


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
        default=None,
        help=(
            "Local identity sidecar base URL "
            f"(defaults to UNITARES_SIDECAR_URL or {DEFAULT_SIDECAR_URL}; "
            "explicitly passing --sidecar-url overrides env UNITARES_SIDECAR_SOCKET)."
        ),
    )
    parser.add_argument(
        "--sidecar-socket",
        default=None,
        help=(
            "Absolute path to a local identity sidecar Unix domain socket "
            "(defaults to UNITARES_SIDECAR_SOCKET)."
        ),
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


def _proven_exchange(
    sidecar_url: str,
    startup_secret: str | None,
    *,
    path: str,
    capability: str,
    payload: dict[str, Any] | None = None,
    slot: str = "",
    timeout: float = HTTP_TIMEOUT,
    sidecar_socket: str = "",
) -> dict[str, Any]:
    """Prove the listener (or validate UDS), then send one capability request."""
    return _sidecar_proven_exchange(
        sidecar_url,
        startup_secret,
        path=path,
        capability=capability,
        payload=payload,
        slot=slot,
        timeout=timeout,
        sidecar_socket=sidecar_socket or None,
        clock=_monotonic,
    )


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
    sidecar_url: str,
    startup_secret: str | None,
    capability: str,
    *,
    sidecar_socket: str = "",
) -> dict[str, Any] | None:
    """Return /health from a proven listener (or validated UDS), or None when unavailable."""
    try:
        return _proven_exchange(
            sidecar_url,
            startup_secret,
            path="/health",
            capability=capability,
            timeout=HEALTH_TIMEOUT,
            sidecar_socket=sidecar_socket,
        )
    except SidecarUnavailable:
        return None
    except ListenerNotProven as exc:
        raise URunError(f"sidecar listener failed startup authentication: {exc}") from exc


def _normalize_sidecar_socket(raw_socket: str) -> str:
    cleaned = raw_socket.strip()
    if not cleaned:
        return ""
    if "\x00" in cleaned:
        raise URunError("--sidecar-socket must not contain NUL bytes")
    expanded = os.path.expanduser(cleaned)
    if not os.path.isabs(expanded) or ".." in Path(expanded).parts:
        raise URunError(
            f"--sidecar-socket must be an absolute path without '..' segments: {raw_socket!r}"
        )
    return expanded


def ensure_sidecar(
    *,
    sidecar_url: str,
    server_url: str,
    workspace: Path,
    slot: str,
    model_type: str,
    capability: str,
    startup_secret: str | None,
    reuse_existing: bool,
    sidecar_socket: str = "",
) -> subprocess.Popen[Any] | None:
    socket_path = _normalize_sidecar_socket(sidecar_socket)
    host = ""
    port = 0
    if not socket_path:
        host, port, sidecar_url = _sidecar_bind(sidecar_url)
    if reuse_existing:
        health = _proven_health(
            sidecar_url,
            startup_secret,
            capability,
            sidecar_socket=socket_path,
        )
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
    ]
    if socket_path:
        command.extend(["--unix-socket", socket_path])
    else:
        command.extend(["--host", host, "--port", str(port)])
    command.extend(
        [
            "--server-url",
            server_url,
            "--workspace",
            str(workspace),
            "--slot",
            slot,
            "--model-type",
            model_type,
        ]
    )
    try:
        sidecar_environment = os.environ.copy()
        sidecar_environment["UNITARES_SIDECAR_CAPABILITY"] = capability
        if socket_path:
            sidecar_environment["UNITARES_SIDECAR_SOCKET"] = socket_path
            sidecar_environment.pop("UNITARES_SIDECAR_STARTUP_SECRET", None)
        else:
            sidecar_environment.pop("UNITARES_SIDECAR_SOCKET", None)
            if startup_secret:
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
            health = _proven_health(
                sidecar_url,
                startup_secret,
                capability,
                sidecar_socket=socket_path,
            )
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
    startup_secret: str | None,
    slot: str,
    model_type: str,
    capability: str,
    sidecar_socket: str = "",
) -> None:
    response = _proven_exchange(
        sidecar_url,
        startup_secret,
        path="/session/start",
        # u-run owns the session it starts, including on a reused --slot.
        payload={"slot": slot, "model_type": model_type, "replace_existing": True},
        slot=slot,
        capability=capability,
        sidecar_socket=sidecar_socket,
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
    startup_secret: str | None,
    slot: str,
    return_code: int,
    exit_code: int,
    wall_seconds: float,
    capability: str,
    sidecar_socket: str = "",
) -> None:
    # The child may run for hours and the listener may have changed hands;
    # the check-in travels on a connection that has just proved itself (or over
    # the validated owner-only UDS).
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
        sidecar_socket=sidecar_socket,
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
    if args.sidecar_socket is not None:
        raw_sidecar_socket = args.sidecar_socket.strip()
    elif args.sidecar_url is not None:
        raw_sidecar_socket = ""
    else:
        raw_sidecar_socket = (os.environ.get("UNITARES_SIDECAR_SOCKET") or "").strip()
    resolved_sidecar_url = (
        args.sidecar_url
        if args.sidecar_url is not None
        else (os.environ.get("UNITARES_SIDECAR_URL") or DEFAULT_SIDECAR_URL)
    )
    capability = configured_capability or secrets.token_urlsafe(32)
    startup_secret = configured_startup_secret or secrets.token_urlsafe(32)
    owned_sidecar: subprocess.Popen[Any] | None = None

    try:
        sidecar_socket = _normalize_sidecar_socket(raw_sidecar_socket)
        if sidecar_socket:
            sidecar_url = ""
            effective_startup_secret: str | None = None
            reuse_existing = bool(configured_capability)
        else:
            _host, _port, sidecar_url = _sidecar_bind(resolved_sidecar_url)
            effective_startup_secret = startup_secret
            reuse_existing = bool(configured_capability and configured_startup_secret)
        owned_sidecar = ensure_sidecar(
            sidecar_url=sidecar_url,
            server_url=server_url,
            workspace=workspace,
            slot=slot,
            model_type=model_type,
            capability=capability,
            startup_secret=effective_startup_secret,
            reuse_existing=reuse_existing,
            sidecar_socket=sidecar_socket,
        )
        _start_session(
            sidecar_url=sidecar_url,
            startup_secret=effective_startup_secret,
            slot=slot,
            model_type=model_type,
            capability=capability,
            sidecar_socket=sidecar_socket,
        )
        sidecar_client_path = str(Path(__file__).resolve().with_name("sidecar_client.py"))
        environment = os.environ.copy()
        environment.update(
            {
                "UNITARES_SERVER_URL": server_url,
                "UNITARES_SIDECAR_SLOT": slot,
                "UNITARES_SIDECAR_CAPABILITY": capability,
                "UNITARES_SIDECAR_CLIENT": sidecar_client_path,
                "UNITARES_MODEL_TYPE": model_type,
            }
        )
        if sidecar_socket:
            environment["UNITARES_SIDECAR_SOCKET"] = sidecar_socket
            environment.pop("UNITARES_SIDECAR_URL", None)
            environment.pop("UNITARES_SIDECAR_STARTUP_SECRET", None)
        else:
            environment["UNITARES_SIDECAR_URL"] = sidecar_url
            environment["UNITARES_SIDECAR_STARTUP_SECRET"] = startup_secret
            environment.pop("UNITARES_SIDECAR_SOCKET", None)
        return_code, exit_code, wall_seconds = _run_child(command, environment)
        try:
            _submit_exit_checkin(
                sidecar_url=sidecar_url,
                startup_secret=effective_startup_secret,
                slot=slot,
                return_code=return_code,
                exit_code=exit_code,
                wall_seconds=wall_seconds,
                capability=capability,
                sidecar_socket=sidecar_socket,
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
