"""Integration coverage for the plain-CLI UNITARES process wrapper."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import u_run  # noqa: E402


SERVER_URL = "http://governance.test:8767"
CAPABILITY = "test-sidecar-capability"
STARTUP_SECRET = "test-sidecar-startup-secret"


class _ReusableServer(ThreadingHTTPServer):
    allow_reuse_address = True


def _serve(handler: type[BaseHTTPRequestHandler]):
    server = _ReusableServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _stop_server(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()


def _json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def _respond(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _sidecar_handler(
    workspace: Path,
    calls: list[tuple[str, dict[str, Any]]],
    *,
    slot: str = "plain-cli",
):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):  # pragma: no cover
            return

        def _before_proof_response(self) -> None:
            return

        def _keeps_proven_connection(self) -> bool:
            return self.headers.get("Connection", "").lower() == "keep-alive"

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/startup-proof":
                assert self.headers.get("Authorization") is None
                challenge = urllib.parse.parse_qs(parsed.query)["challenge"][0]
                proof = hmac.new(
                    STARTUP_SECRET.encode(),
                    challenge.encode(),
                    hashlib.sha256,
                ).hexdigest()
                self._before_proof_response()
                # Like the real sidecar: keep the proven connection open for
                # the capability-bearing request that follows it.
                keep_alive = self._keeps_proven_connection()
                body = json.dumps({"success": True, "proof": proof}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if keep_alive:
                    self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(body)
                if keep_alive:
                    self.close_connection = False
                return
            assert parsed.path == "/health"
            assert self.headers.get("Authorization") == f"Bearer {CAPABILITY}"
            _respond(
                self,
                {
                    "success": True,
                    "capability_id": hashlib.sha256(CAPABILITY.encode()).hexdigest(),
                    "server_url": SERVER_URL,
                    "workspace": str(workspace.resolve()),
                    "default_slot": slot,
                },
            )

        def do_POST(self):  # noqa: N802
            assert self.headers.get("Authorization") == f"Bearer {CAPABILITY}"
            body = _json_body(self)
            calls.append((self.path, body))
            if self.path == "/session/start":
                _respond(self, {"success": True, "result": {"status": "ok"}})
            elif self.path == "/turn/stop":
                _respond(self, {"success": True, "status": "sent"})
            else:  # pragma: no cover
                self.send_error(404)

    return Handler


def test_reuses_sidecar_exports_context_and_preserves_child_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNITARES_SIDECAR_CAPABILITY", CAPABILITY)
    monkeypatch.setenv("UNITARES_SIDECAR_STARTUP_SECRET", STARTUP_SECRET)
    calls: list[tuple[str, dict[str, Any]]] = []
    server, thread, sidecar_url = _serve(_sidecar_handler(tmp_path, calls))
    output = tmp_path / "child-env.json"
    child = (
        "import json, os, pathlib, sys; "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
        "'server': os.environ.get('UNITARES_SERVER_URL'), "
        "'sidecar': os.environ.get('UNITARES_SIDECAR_URL'), "
        "'slot': os.environ.get('UNITARES_SIDECAR_SLOT'), "
        "'capability_set': bool(os.environ.get('UNITARES_SIDECAR_CAPABILITY')), "
        "'class': os.environ.get('UNITARES_MODEL_TYPE')})); "
        "raise SystemExit(7)"
    )
    try:
        result = u_run.main(
            [
                "--class",
                "goose",
                "--server-url",
                SERVER_URL,
                "--sidecar-url",
                sidecar_url,
                "--workspace",
                str(tmp_path),
                "--slot",
                "plain-cli",
                "--",
                sys.executable,
                "-c",
                child,
                str(output),
            ]
        )
    finally:
        _stop_server(server, thread)

    assert result == 7
    assert json.loads(output.read_text()) == {
        "server": SERVER_URL,
        "sidecar": sidecar_url,
        "slot": "plain-cli",
        "capability_set": True,
        "class": "goose",
    }
    assert [path for path, _body in calls] == ["/session/start", "/turn/stop"]
    start = calls[0][1]
    assert start == {"slot": "plain-cli", "model_type": "goose"}
    stopped = calls[1][1]
    assert stopped["event"] == "u_run_exit"
    assert stopped["epistemic_class"] == "substrate_interpretation"
    assert "confidence" not in stopped
    assert "exit code 7" in stopped["response_text"]


def test_health_lookalike_without_capability_proof_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization_headers: list[str | None] = []

    class Lookalike(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):  # pragma: no cover
            return

        def do_GET(self):  # noqa: N802
            authorization_headers.append(self.headers.get("Authorization"))
            _respond(
                self,
                {
                    "success": True,
                    "server_url": SERVER_URL,
                    "workspace": str(tmp_path.resolve()),
                },
            )

    monkeypatch.setenv("UNITARES_SIDECAR_CAPABILITY", CAPABILITY)
    server, thread, sidecar_url = _serve(Lookalike)
    try:
        result = u_run.main(
            [
                "--server-url",
                SERVER_URL,
                "--sidecar-url",
                sidecar_url,
                "--workspace",
                str(tmp_path),
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(0)",
            ]
        )
    finally:
        _stop_server(server, thread)

    assert result == 125
    assert authorization_headers
    assert authorization_headers == [None]


class _GovernanceHandler(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def log_message(self, _fmt, *_args):  # pragma: no cover
        return

    def do_POST(self):  # noqa: N802
        body = _json_body(self)
        self.__class__.calls.append(body)
        if body.get("name") == "onboard":
            result = {
                "success": True,
                "uuid": "11111111-2222-4333-8444-555555555555",
                "agent_id": "URun_Test",
                "client_session_id": "agent-u-run-111",
                "session_resolution_source": "force_new",
            }
        else:
            result = {"success": True, "verdict": {"value": "proceed"}}
        _respond(self, {"result": result})


def _unused_sidecar_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def test_starts_real_sidecar_writes_slot_cache_and_tears_it_down(tmp_path: Path) -> None:
    _GovernanceHandler.calls = []
    server, thread, governance_url = _serve(_GovernanceHandler)
    sidecar_url = _unused_sidecar_url()
    try:
        result = u_run.main(
            [
                "--server-url",
                governance_url,
                "--sidecar-url",
                sidecar_url,
                "--workspace",
                str(tmp_path),
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(0)",
            ]
        )
    finally:
        _stop_server(server, thread)

    assert result == 0
    caches = list((tmp_path / ".unitares").glob("session-u-run-*.json"))
    assert len(caches) == 1
    cache = json.loads(caches[0].read_text())
    assert cache["uuid"] == "11111111-2222-4333-8444-555555555555"
    assert [call["name"] for call in _GovernanceHandler.calls] == [
        "onboard",
        "process_agent_update",
    ]
    assert _GovernanceHandler.calls[0]["arguments"]["model_type"] == "u-run"
    checkin = _GovernanceHandler.calls[1]["arguments"]
    assert checkin["epistemic_class"] == "substrate_interpretation"
    assert "confidence" not in checkin
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"{sidecar_url}/health", timeout=0.2)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group signal semantics")
def test_forwards_termination_signal_to_child_group(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    server, thread, sidecar_url = _serve(
        _sidecar_handler(tmp_path, calls, slot="signal-slot")
    )
    pid_file = tmp_path / "child.pid"
    child = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    wrapper = subprocess.Popen(
        [
            str(SCRIPTS / "u-run"),
            "--server-url",
            SERVER_URL,
            "--sidecar-url",
            sidecar_url,
            "--workspace",
            str(tmp_path),
            "--slot",
            "signal-slot",
            "--",
            sys.executable,
            "-c",
            child,
            str(pid_file),
        ],
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "UNITARES_SIDECAR_CAPABILITY": CAPABILITY,
            "UNITARES_SIDECAR_STARTUP_SECRET": STARTUP_SECRET,
        },
    )
    child_pid = 0
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not pid_file.exists():
            if wrapper.poll() is not None:
                pytest.fail(f"wrapper exited early: {wrapper.stderr.read()}")
            time.sleep(0.02)
        assert pid_file.exists()
        child_pid = int(pid_file.read_text())
        wrapper.send_signal(signal.SIGTERM)
        _stdout, stderr = wrapper.communicate(timeout=5)
        assert stderr == ""
    finally:
        wrapper_was_running = wrapper.poll() is None
        if wrapper_was_running:
            wrapper.kill()
            wrapper.wait(timeout=2)
        if wrapper_was_running and child_pid:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        _stop_server(server, thread)

    assert wrapper.returncode == 128 + signal.SIGTERM
    assert [path for path, _body in calls] == ["/session/start", "/turn/stop"]
    stopped = calls[1][1]
    assert f"signal {signal.SIGTERM}" in stopped["response_text"]
    assert f"exit code {128 + signal.SIGTERM}" in stopped["response_text"]
    assert stopped["epistemic_class"] == "substrate_interpretation"


def test_cli_requires_separator_and_complexity_is_bounded() -> None:
    with pytest.raises(SystemExit) as caught:
        u_run.parse_args(["agent-cli"])
    assert caught.value.code == 2
    assert u_run.exit_complexity(0, 0) == 0.3
    assert u_run.exit_complexity(3600, 0) == 0.65
    assert u_run.exit_complexity(3600, 9) == 0.85
    assert u_run.exit_complexity(100_000, 9) == 0.85


@pytest.mark.parametrize(
    "url",
    ["https://127.0.0.1:8768", "http://127.evil:8768", "http://127.0.0.1:0"],
)
def test_sidecar_address_must_be_plain_http_ipv4_loopback(url: str) -> None:
    with pytest.raises(u_run.URunError):
        u_run._sidecar_bind(url)


def test_exit_checkin_skipped_when_listener_is_replaced_during_child_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("UNITARES_SIDECAR_CAPABILITY", CAPABILITY)
    monkeypatch.setenv("UNITARES_SIDECAR_STARTUP_SECRET", STARTUP_SECRET)
    calls: list[tuple[str, dict[str, Any]]] = []
    server, thread, sidecar_url = _serve(_sidecar_handler(tmp_path, calls))
    port = server.server_address[1]
    lookalike_requests: list[tuple[str, str, str | None]] = []

    class Lookalike(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):  # pragma: no cover
            return

        def _record(self) -> None:
            lookalike_requests.append(
                (self.command, self.path, self.headers.get("Authorization"))
            )
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length:
                self.rfile.read(length)
            _respond(self, {"success": True, "proof": "0" * 64, "status": "sent"})

        do_GET = _record
        do_POST = _record

    servers: list[tuple[ThreadingHTTPServer, threading.Thread]] = [(server, thread)]

    def replace_listener_during_child(
        _command: list[str], _environment: dict[str, str]
    ) -> tuple[int, int, float]:
        genuine, genuine_thread = servers.pop()
        _stop_server(genuine, genuine_thread)
        squatter = _ReusableServer(("127.0.0.1", port), Lookalike)
        squatter_thread = threading.Thread(target=squatter.serve_forever, daemon=True)
        squatter_thread.start()
        servers.append((squatter, squatter_thread))
        return 3, 3, 1.0

    monkeypatch.setattr(u_run, "_run_child", replace_listener_during_child)
    try:
        result = u_run.main(
            [
                "--server-url",
                SERVER_URL,
                "--sidecar-url",
                sidecar_url,
                "--workspace",
                str(tmp_path),
                "--slot",
                "plain-cli",
                "--",
                "agent-cli",
            ]
        )
    finally:
        for running, running_thread in servers:
            _stop_server(running, running_thread)

    assert result == 3
    assert [path for path, _body in calls] == ["/session/start"]
    assert lookalike_requests
    assert all(authorization is None for _m, _p, authorization in lookalike_requests)
    assert all(method == "GET" for method, _p, _a in lookalike_requests)
    assert "u-run: warning:" in capsys.readouterr().err


def _sidecar_main_args(sidecar_url: str, workspace: Path) -> list[str]:
    return [
        "--server-url",
        SERVER_URL,
        "--sidecar-url",
        sidecar_url,
        "--workspace",
        str(workspace),
        "--slot",
        "plain-cli",
        "--",
        "agent-cli",
    ]


@pytest.mark.parametrize("listener_keeps_connection", [True, False])
def test_exit_capability_travels_on_the_connection_that_proved_the_listener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    listener_keeps_connection: bool,
) -> None:
    """The listener changes hands between the exit proof and the check-in.

    A listener that closes the proven connection (an older sidecar) must not
    make u-run reconnect and hand the capability to the new port holder.
    """
    monkeypatch.setenv("UNITARES_SIDECAR_CAPABILITY", CAPABILITY)
    monkeypatch.setenv("UNITARES_SIDECAR_STARTUP_SECRET", STARTUP_SECRET)
    calls: list[tuple[str, dict[str, Any]]] = []
    squatter_requests: list[tuple[str, str, str | None]] = []
    child_exited = threading.Event()
    running: list[tuple[ThreadingHTTPServer, threading.Thread]] = []

    class Squatter(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):  # pragma: no cover
            return

        def _record(self) -> None:
            squatter_requests.append(
                (self.command, self.path, self.headers.get("Authorization"))
            )
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length:
                self.rfile.read(length)
            _respond(self, {"success": True, "status": "sent"})

        do_GET = _record
        do_POST = _record

    genuine_handler = _sidecar_handler(tmp_path, calls)

    class HandsOverAfterExitProof(genuine_handler):  # type: ignore[misc, valid-type]
        def _keeps_proven_connection(self) -> bool:
            if child_exited.is_set() and not listener_keeps_connection:
                return False
            return super()._keeps_proven_connection()

        def _before_proof_response(self) -> None:
            if not child_exited.is_set() or squatter_requests or len(running) > 1:
                return
            genuine, _genuine_thread = running[0]
            port = genuine.server_address[1]
            # Stop accepting on the genuine listener (this handler keeps its
            # own connection) and let another process take the port before
            # the proof response is released.
            threading.Thread(target=genuine.shutdown, daemon=True).start()
            genuine.socket.close()
            # The old listening socket can take a moment to leave the port
            # (its accept loop is still unwinding), so retry the bind briefly.
            squatter = None
            for _attempt in range(100):
                try:
                    squatter = _ReusableServer(("127.0.0.1", port), Squatter)
                    break
                except OSError:
                    time.sleep(0.02)
            if squatter is None:
                return
            squatter_thread = threading.Thread(target=squatter.serve_forever, daemon=True)
            squatter_thread.start()
            running.append((squatter, squatter_thread))

    server, thread, sidecar_url = _serve(HandsOverAfterExitProof)
    running.append((server, thread))

    def child(_command: list[str], _environment: dict[str, str]) -> tuple[int, int, float]:
        child_exited.set()
        return 4, 4, 1.0

    monkeypatch.setattr(u_run, "_run_child", child)
    try:
        result = u_run.main(_sidecar_main_args(sidecar_url, tmp_path))
    finally:
        for index, (running_server, running_thread) in enumerate(running):
            running_server.shutdown()
            running_thread.join(timeout=2)
            if index:
                running_server.server_close()

    assert result == 4
    if len(running) != 2:
        # The port could not be handed to a second listener in time on this
        # platform, so the scenario under test was never staged.
        pytest.skip("could not stage the listener hand-over on this platform")
    assert all(authorization is None for _m, _p, authorization in squatter_requests)
    stderr = capsys.readouterr().err
    if listener_keeps_connection:
        assert [path for path, _body in calls] == ["/session/start", "/turn/stop"]
        assert "warning" not in stderr
    else:
        assert [path for path, _body in calls] == ["/session/start"]
        assert squatter_requests == []
        assert "closed the connection" in stderr


def _replace_listener_with(
    monkeypatch: pytest.MonkeyPatch,
    servers: list[tuple[Any, threading.Thread]],
    handler: type[BaseHTTPRequestHandler],
    *,
    child_code: int = 3,
) -> None:
    def replace_listener_during_child(
        _command: list[str], _environment: dict[str, str]
    ) -> tuple[int, int, float]:
        genuine, genuine_thread = servers.pop()
        port = genuine.server_address[1]
        _stop_server(genuine, genuine_thread)
        squatter = _ReusableServer(("127.0.0.1", port), handler)
        squatter_thread = threading.Thread(target=squatter.serve_forever, daemon=True)
        squatter_thread.start()
        servers.append((squatter, squatter_thread))
        return child_code, child_code, 1.0

    monkeypatch.setattr(u_run, "_run_child", replace_listener_during_child)


class _NonAsciiProof(BaseHTTPRequestHandler):
    def log_message(self, _fmt, *_args):  # pragma: no cover
        return

    def do_GET(self):  # noqa: N802
        _respond(self, {"success": True, "proof": "é" * 64})


class _BadStatusLine(BaseHTTPRequestHandler):
    def log_message(self, _fmt, *_args):  # pragma: no cover
        return

    def do_GET(self):  # noqa: N802
        self.wfile.write(b"NOT-HTTP garbage\r\n\r\n")
        self.close_connection = True


class _SurrogateProof(BaseHTTPRequestHandler):
    def log_message(self, _fmt, *_args):  # pragma: no cover
        return

    def do_GET(self):  # noqa: N802
        body = b'{"success": true, "proof": "\\ud800"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.parametrize(
    "handler", [_NonAsciiProof, _BadStatusLine, _SurrogateProof], ids=lambda h: h.__name__
)
def test_malformed_exit_proof_warns_and_keeps_child_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    handler: type[BaseHTTPRequestHandler],
) -> None:
    monkeypatch.setenv("UNITARES_SIDECAR_CAPABILITY", CAPABILITY)
    monkeypatch.setenv("UNITARES_SIDECAR_STARTUP_SECRET", STARTUP_SECRET)
    calls: list[tuple[str, dict[str, Any]]] = []
    server, thread, sidecar_url = _serve(_sidecar_handler(tmp_path, calls))
    servers: list[tuple[Any, threading.Thread]] = [(server, thread)]
    _replace_listener_with(monkeypatch, servers, handler)
    try:
        result = u_run.main(_sidecar_main_args(sidecar_url, tmp_path))
    finally:
        for running, running_thread in servers:
            _stop_server(running, running_thread)

    assert result == 3
    assert [path for path, _body in calls] == ["/session/start"]
    assert "u-run: warning:" in capsys.readouterr().err


class _TrickledProof(BaseHTTPRequestHandler):
    def log_message(self, _fmt, *_args):  # pragma: no cover
        return

    def do_GET(self):  # noqa: N802
        self.wfile.write(b"HTTP/1.0 200 OK\r\nX-Pad: ")
        stop = time.monotonic() + 6.0
        try:
            while time.monotonic() < stop:
                self.wfile.write(b"a")
                time.sleep(0.1)
        except OSError:
            pass
        self.close_connection = True


def test_trickled_exit_proof_cannot_hold_u_run_past_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("UNITARES_SIDECAR_CAPABILITY", CAPABILITY)
    monkeypatch.setenv("UNITARES_SIDECAR_STARTUP_SECRET", STARTUP_SECRET)
    calls: list[tuple[str, dict[str, Any]]] = []
    server, thread, sidecar_url = _serve(_sidecar_handler(tmp_path, calls))
    servers: list[tuple[Any, threading.Thread]] = [(server, thread)]
    child_returned: list[float] = []

    def note_child_return(*_args: Any) -> None:
        child_returned.append(time.monotonic())

    _replace_listener_with(monkeypatch, servers, _TrickledProof)
    replaced = u_run._run_child

    def timed_child(command: list[str], environment: dict[str, str]) -> tuple[int, int, float]:
        outcome = replaced(command, environment)
        note_child_return()
        return outcome

    monkeypatch.setattr(u_run, "_run_child", timed_child)
    try:
        result = u_run.main(_sidecar_main_args(sidecar_url, tmp_path))
        finished = time.monotonic()
    finally:
        for running, running_thread in servers:
            _stop_server(running, running_thread)

    assert result == 3
    assert finished - child_returned[0] < 2.0
    assert [path for path, _body in calls] == ["/session/start"]
    assert "u-run: warning:" in capsys.readouterr().err


def _proof_for(path: str) -> str:
    challenge = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["challenge"][0]
    return hmac.new(STARTUP_SECRET.encode(), challenge.encode(), hashlib.sha256).hexdigest()


def _short_body_handler(*, short_part: str, ending: str) -> type[BaseHTTPRequestHandler]:
    """A proven listener whose ``short_part`` response is valid JSON cut short.

    ``ending`` is "trickle" (keep sending JSON whitespace until u-run's
    deadline shuts the socket) or "close" (close before the declared length).
    """

    class ShortBody(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):  # pragma: no cover
            return

        def do_GET(self):  # noqa: N802
            is_proof = self.path.startswith("/startup-proof")
            payload: dict[str, Any] = {"success": True}
            if is_proof:
                payload["proof"] = _proof_for(self.path)
            body = json.dumps(payload).encode()
            short = (short_part == "proof") == is_proof
            self.send_response(200)
            self.send_header("Content-Length", str(len(body) + 4000 if short else len(body)))
            self.send_header("Connection", "keep-alive" if is_proof else "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            if not short:
                self.close_connection = not is_proof
                return
            if ending == "trickle":
                stop = time.monotonic() + 4.0
                try:
                    while time.monotonic() < stop:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.05)
                except OSError:
                    pass
            self.close_connection = True

    return ShortBody


@pytest.mark.parametrize("ending", ["trickle", "close"])
@pytest.mark.parametrize("short_part", ["proof", "request"])
def test_response_cut_short_of_its_declared_length_is_refused(
    short_part: str, ending: str
) -> None:
    """A deadline-cut or truncated body is never parsed as the answer."""
    server, thread, sidecar_url = _serve(_short_body_handler(short_part=short_part, ending=ending))
    started = time.monotonic()
    try:
        with pytest.raises(u_run.URunError) as raised:
            u_run._proven_exchange(
                sidecar_url,
                STARTUP_SECRET,
                path="/health",
                capability=CAPABILITY,
                timeout=0.5,
            )
    finally:
        _stop_server(server, thread)
    assert time.monotonic() - started < 2.0
    if short_part == "proof":
        expected = u_run.SidecarUnavailable if ending == "trickle" else u_run.ListenerNotProven
        assert type(raised.value) is expected
    else:
        assert type(raised.value) is u_run.URunError
        expected_reason = "no response within" if ending == "trickle" else "IncompleteRead"
        assert expected_reason in str(raised.value)


class _AdvanceableClock:
    """A monotonic clock that only moves when a test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize("late_part", ["proof", "request"])
def test_complete_response_after_the_deadline_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_part: str
) -> None:
    """A late last byte expires the exchange even if the watchdog never ran.

    The injected clock jumps past the budget while the listener answers; real
    time stays short, so the watchdog thread cannot fire.
    """
    clock = _AdvanceableClock()
    monkeypatch.setattr(u_run, "_monotonic", clock)
    calls: list[tuple[str, dict[str, Any]]] = []
    base = _sidecar_handler(tmp_path, calls)

    class LateHandler(base):  # type: ignore[misc, valid-type]
        def _before_proof_response(self) -> None:
            if late_part == "proof":
                clock.now += 60.0

        def do_GET(self):  # noqa: N802
            if late_part == "request" and self.path == "/health":
                clock.now += 60.0
            super().do_GET()

    server, thread, sidecar_url = _serve(LateHandler)
    try:
        with pytest.raises(u_run.URunError) as raised:
            u_run._proven_exchange(
                sidecar_url,
                STARTUP_SECRET,
                path="/health",
                capability=CAPABILITY,
                timeout=5.0,
            )
    finally:
        _stop_server(server, thread)
    if late_part == "proof":
        assert type(raised.value) is u_run.SidecarUnavailable
    else:
        assert type(raised.value) is u_run.URunError
        assert "no response within" in str(raised.value)


def _chunked(body: bytes) -> bytes:
    return f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"


_UNDELIMITED_RESPONSES = {
    # Bodyless statuses that http.client reads as length zero, carrying a
    # chunked JSON body anyway.
    "204-chunked": (b"HTTP/1.1 204 No Content\r\nTransfer-Encoding: chunked\r\n", "chunked"),
    "304-chunked": (b"HTTP/1.1 304 Not Modified\r\nTransfer-Encoding: chunked\r\n", "chunked"),
    "101-chunked": (
        b"HTTP/1.1 101 Switching Protocols\r\nTransfer-Encoding: chunked\r\n",
        "chunked",
    ),
    "204-chunked-with-length": (
        b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n",
        "chunked",
    ),
    "200-chunked": (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n", "chunked"),
    "200-no-length": (b"HTTP/1.1 200 OK\r\nConnection: close\r\n", "raw"),
}


def _undelimited_handler(*, bad_part: str, variant: str) -> type[BaseHTTPRequestHandler]:
    """A listener whose ``bad_part`` answer is valid JSON without a declared length."""
    head, framing = _UNDELIMITED_RESPONSES[variant]

    class Undelimited(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):  # pragma: no cover
            return

        def do_GET(self):  # noqa: N802
            is_proof = self.path.startswith("/startup-proof")
            payload: dict[str, Any] = {
                "success": True,
                "capability_id": hashlib.sha256(CAPABILITY.encode()).hexdigest(),
            }
            if is_proof:
                payload["proof"] = _proof_for(self.path)
            body = json.dumps(payload).encode()
            if (bad_part == "proof") != is_proof:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "keep-alive" if is_proof else "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = not is_proof
                return
            self.wfile.write(head + b"\r\n" + (_chunked(body) if framing == "chunked" else body))
            self.wfile.flush()
            self.close_connection = True

    return Undelimited


@pytest.mark.parametrize("variant", sorted(_UNDELIMITED_RESPONSES))
@pytest.mark.parametrize("bad_part", ["proof", "request"])
def test_response_without_an_explicit_content_length_is_refused(
    bad_part: str, variant: str
) -> None:
    server, thread, sidecar_url = _serve(_undelimited_handler(bad_part=bad_part, variant=variant))
    try:
        with pytest.raises(u_run.URunError) as raised:
            u_run._proven_exchange(
                sidecar_url,
                STARTUP_SECRET,
                path="/health",
                capability=CAPABILITY,
                timeout=2.0,
            )
    finally:
        _stop_server(server, thread)
    if bad_part == "proof":
        assert type(raised.value) is u_run.ListenerNotProven
    else:
        assert type(raised.value) is u_run.URunError
