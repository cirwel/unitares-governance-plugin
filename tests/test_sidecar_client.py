"""Tests for the stdlib sidecar client module and CLI (Finding 17)."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import socket
import socketserver
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import identity_sidecar  # noqa: E402
import sidecar_client  # noqa: E402


CAPABILITY = "test-client-capability"
STARTUP_SECRET = "test-client-startup-secret"


class _ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def _respond(
    handler: BaseHTTPRequestHandler,
    payload: dict[str, Any],
    *,
    status: int = 200,
    keep_alive: bool = False,
) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    if keep_alive:
        handler.send_header("Connection", "keep-alive")
    handler.end_headers()
    handler.wfile.write(body)
    if keep_alive:
        handler.close_connection = False


def _make_recording_sidecar(
    records: list[dict[str, Any]],
    *,
    proof_secret: str | None = STARTUP_SECRET,
    keep_alive_proof: bool = True,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):  # pragma: no cover
            return

        def address_string(self):
            return "unix" if not isinstance(self.client_address, tuple) else self.client_address[0]

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            records.append(
                {
                    "method": "GET",
                    "path": parsed.path,
                    "auth": self.headers.get("Authorization"),
                    "slot": self.headers.get("X-UNITARES-Slot"),
                }
            )
            if parsed.path == "/startup-proof":
                if proof_secret is None:
                    _respond(self, {"success": False, "error": "not found"}, status=404)
                    return
                challenge = urllib.parse.parse_qs(parsed.query).get("challenge", [""])[0]
                proof = hmac.new(
                    proof_secret.encode("utf-8"),
                    challenge.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                _respond(
                    self,
                    {"success": True, "proof": proof},
                    keep_alive=keep_alive_proof
                    and self.headers.get("Connection", "").lower() == "keep-alive",
                )
                return
            if self.headers.get("Authorization") != f"Bearer {CAPABILITY}":
                _respond(self, {"success": False, "error": "unauthorized"}, status=401)
                return
            _respond(self, {"success": True, "route": parsed.path})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            records.append(
                {
                    "method": "POST",
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "slot": self.headers.get("X-UNITARES-Slot"),
                    "body": body,
                }
            )
            if self.headers.get("Authorization") != f"Bearer {CAPABILITY}":
                _respond(self, {"success": False, "error": "unauthorized"}, status=401)
                return
            _respond(self, {"success": True, "echo": body, "path": self.path})

    return Handler


def _serve_tcp(handler: type[BaseHTTPRequestHandler]):
    server = _ReusableHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _stop_server(server: Any, thread: threading.Thread) -> None:
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()


def test_tcp_client_proves_startup_secret_before_sending_capability() -> None:
    records: list[dict[str, Any]] = []
    server, thread, url = _serve_tcp(_make_recording_sidecar(records))
    try:
        client = sidecar_client.SidecarClient(
            capability=CAPABILITY,
            sidecar_url=url,
            startup_secret=STARTUP_SECRET,
            slot="slot-tcp",
        )
        reply = client.call_tool("check_working_state", {"detail": True})
    finally:
        _stop_server(server, thread)

    assert reply["success"] is True
    assert reply["echo"] == {"name": "check_working_state", "arguments": {"detail": True}}
    assert [r["path"] for r in records] == ["/startup-proof", "/v1/tools/call"]
    assert records[0]["auth"] is None
    assert records[1]["auth"] == f"Bearer {CAPABILITY}"
    assert records[1]["slot"] == "slot-tcp"


def test_tcp_client_never_sends_capability_without_valid_startup_proof() -> None:
    # 1. Wrong startup secret on listener -> ListenerNotProven, capability withheld.
    records: list[dict[str, Any]] = []
    server, thread, url = _serve_tcp(
        _make_recording_sidecar(records, proof_secret="wrong-secret")
    )
    try:
        client = sidecar_client.SidecarClient(
            capability=CAPABILITY,
            sidecar_url=url,
            startup_secret=STARTUP_SECRET,
        )
        with pytest.raises(sidecar_client.ListenerNotProven):
            client.call_tool("check_working_state")
    finally:
        _stop_server(server, thread)

    assert [r["path"] for r in records] == ["/startup-proof"]
    assert all(r["auth"] is None for r in records)

    # 2. Missing startup secret on client -> refuses before even connecting.
    records.clear()
    server, thread, url = _serve_tcp(_make_recording_sidecar(records))
    try:
        client = sidecar_client.SidecarClient(
            capability=CAPABILITY,
            sidecar_url=url,
            startup_secret=None,
        )
        with pytest.raises(sidecar_client.ListenerNotProven, match="UNITARES_SIDECAR_STARTUP_SECRET"):
            client.call_tool("check_working_state")
    finally:
        _stop_server(server, thread)
    assert records == []

    # 3. Listener that closes keep-alive connection after proof -> refuses (no reconnect).
    records.clear()
    server, thread, url = _serve_tcp(
        _make_recording_sidecar(records, keep_alive_proof=False)
    )
    try:
        client = sidecar_client.SidecarClient(
            capability=CAPABILITY,
            sidecar_url=url,
            startup_secret=STARTUP_SECRET,
        )
        with pytest.raises(sidecar_client.ListenerNotProven, match="closed the connection"):
            client.post("/turn/checkin", {"response_text": "hi"})
    finally:
        _stop_server(server, thread)
    assert [r["path"] for r in records] == ["/startup-proof"]
    assert all(r["auth"] is None for r in records)


def test_unix_socket_client_connects_and_validates_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(socket, "AF_UNIX") or not hasattr(socketserver, "UnixStreamServer"):
        pytest.skip("AF_UNIX not supported on this platform")

    records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="u-sc-") as tmpdir:
        os.chmod(tmpdir, 0o700)
        sock_path = Path(tmpdir) / "s.sock"
        srv = identity_sidecar.BoundedThreadingUnixHTTPServer(
            sock_path,
            _make_recording_sidecar(records, proof_secret=None),
        )
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            # Valid 0600 owner-only socket succeeds without startup secret.
            client = sidecar_client.SidecarClient.from_env(
                {
                    "UNITARES_SIDECAR_SOCKET": str(sock_path),
                    "UNITARES_SIDECAR_CAPABILITY": CAPABILITY,
                    "UNITARES_SIDECAR_SLOT": "slot-uds",
                }
            )
            res = client.post("/turn/checkin", {"response_text": "ok", "complexity": 0.2})
            assert res["success"] is True
            assert res["echo"] == {"response_text": "ok", "complexity": 0.2}
            assert len(records) == 1
            assert records[0]["auth"] == f"Bearer {CAPABILITY}"
            assert records[0]["slot"] == "slot-uds"

            # Symlink to valid socket is refused fail-closed.
            records.clear()
            link = Path(tmpdir) / "link.sock"
            link.symlink_to(sock_path)
            with pytest.raises(sidecar_client.ListenerNotProven, match="symlink"):
                sidecar_client.SidecarClient(
                    capability=CAPABILITY,
                    sidecar_socket=str(link),
                ).health()
            assert records == []

            # Regular file is refused fail-closed.
            regular = Path(tmpdir) / "file.sock"
            regular.write_text("x", encoding="utf-8")
            os.chmod(regular, 0o600)
            with pytest.raises(sidecar_client.ListenerNotProven, match="not a socket"):
                sidecar_client.SidecarClient(
                    capability=CAPABILITY,
                    sidecar_socket=str(regular),
                ).health()

            # Group/world readable socket is refused fail-closed.
            os.chmod(sock_path, 0o644)
            with pytest.raises(sidecar_client.ListenerNotProven, match="permissions"):
                client.health()
            assert records == []
            os.chmod(sock_path, 0o600)

            # Socket owned by another UID is refused fail-closed.
            fake_uid = os.lstat(sock_path).st_uid + 1
            monkeypatch.setattr(os, "geteuid", lambda: fake_uid)
            monkeypatch.setattr(os, "getuid", lambda: fake_uid)
            with pytest.raises(sidecar_client.ListenerNotProven, match="not owned"):
                client.health()
            assert records == []
            monkeypatch.undo()

            # Relative socket path is refused fail-closed.
            with pytest.raises(sidecar_client.ListenerNotProven, match="absolute"):
                sidecar_client.SidecarClient(
                    capability=CAPABILITY,
                    sidecar_socket="relative/s.sock",
                ).health()

            # Group/world-writable non-sticky parent dir is refused fail-closed.
            os.chmod(tmpdir, 0o775)
            try:
                with pytest.raises(sidecar_client.ListenerNotProven, match="sticky"):
                    client.health()
                assert records == []
            finally:
                os.chmod(tmpdir, 0o700)

            # Post-connect peer UID mismatch refuses before sending capability.
            monkeypatch.setattr(
                sidecar_client,
                "_verify_unix_peer_uid",
                lambda _sock: (_ for _ in ()).throw(
                    sidecar_client.ListenerNotProven("peer uid mismatch")
                ),
            )
            with pytest.raises(sidecar_client.ListenerNotProven, match="peer uid mismatch"):
                client.health()
            assert records == []
        finally:
            _stop_server(srv, thread)

        # Missing socket raises SidecarUnavailable (not ListenerNotProven).
        with pytest.raises(sidecar_client.SidecarUnavailable):
            sidecar_client.SidecarClient(
                capability=CAPABILITY,
                sidecar_socket=str(Path(tmpdir) / "missing.sock"),
            ).health()


def test_startup_proof_503_maps_to_sidecar_unavailable() -> None:
    class BusyHandler(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):  # pragma: no cover
            return

        def do_GET(self):  # noqa: N802
            _respond(self, {"success": False, "error": "busy"}, status=503)

    server, thread, url = _serve_tcp(BusyHandler)
    try:
        client = sidecar_client.SidecarClient(
            capability=CAPABILITY,
            sidecar_url=url,
            startup_secret=STARTUP_SECRET,
        )
        with pytest.raises(sidecar_client.SidecarUnavailable, match="503"):
            client.health()
    finally:
        _stop_server(server, thread)


def test_cli_call_post_and_get_over_tcp_and_uds(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    records: list[dict[str, Any]] = []
    server, thread, url = _serve_tcp(_make_recording_sidecar(records))
    monkeypatch.setenv("UNITARES_SIDECAR_URL", url)
    monkeypatch.setenv("UNITARES_SIDECAR_CAPABILITY", CAPABILITY)
    monkeypatch.setenv("UNITARES_SIDECAR_STARTUP_SECRET", STARTUP_SECRET)
    monkeypatch.delenv("UNITARES_SIDECAR_SOCKET", raising=False)
    try:
        rc = sidecar_client.main(["call", "knowledge", "--arguments", '{"action": "search"}'])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["echo"] == {"name": "knowledge", "arguments": {"action": "search"}}

        # Stdin JSON input via '-'
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"response_text": "from-stdin"}'))
        rc = sidecar_client.main(["post", "/turn/checkin", "--data", "-"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["echo"] == {"response_text": "from-stdin"}

        rc = sidecar_client.main(["get", "/health"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["route"] == "/health"

        # Invalid JSON exits with argparse code 2.
        with pytest.raises(SystemExit) as caught:
            sidecar_client.main(["call", "knowledge", "--arguments", "[1,2]"])
        assert caught.value.code == 2

        # Wrong secret returns exit code 1 with diagnostic on stderr.
        monkeypatch.setenv("UNITARES_SIDECAR_STARTUP_SECRET", "bad-secret")
        rc = sidecar_client.main(["call", "knowledge"])
        assert rc == 1
        assert "sidecar-client:" in capsys.readouterr().err
    finally:
        _stop_server(server, thread)
