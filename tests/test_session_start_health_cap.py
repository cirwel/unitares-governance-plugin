"""SessionStart bounds the /health body and warns about cleartext transport.

The health probe's body used to be collected with no size limit for the whole
curl timeout. It is now capped at 64 KiB; a larger answer fails the probe and
the session starts as if the server were unreachable, without the hook
aborting under ``set -e``.
"""

from __future__ import annotations

import http.server
import threading
import time
from pathlib import Path

import pytest

from tests.test_session_start_checkin import _ReusableTCPServer, _context, _run_hook

CAP = 65536


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    body_size = 0
    declare_length = True

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if self.declare_length:
            self.send_header("Content-Length", str(self.body_size))
        self.end_headers()
        if self.body_size <= 64:
            self.wfile.write(b'{"status":"alive"}'[: self.body_size].ljust(self.body_size))
            return
        chunk = b"x" * 65536
        sent = 0
        try:
            while sent < self.body_size:
                part = chunk[: self.body_size - sent]
                self.wfile.write(part)
                sent += len(part)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        self.send_response(503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a, **k):
        pass


def _run_with_health(tmp_path, *, body_size, declare_length):
    _HealthHandler.body_size = body_size
    _HealthHandler.declare_length = declare_length
    srv = _ReusableTCPServer(("127.0.0.1", 0), _HealthHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        stdout, _ = _run_hook(
            tmp_path,
            f"http://127.0.0.1:{port}",
            extra_env={"UNITARES_AUTO_ONBOARD": "off", "UNITARES_EAGER_BIND": "off"},
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)
    return stdout, time.monotonic() - started


def _health_log_line(tmp_path: Path) -> str:
    log = tmp_path / ".unitares" / "session-start.log"
    lines = [line for line in log.read_text().splitlines() if "curl exit=" in line]
    assert lines, "the health probe wrote no log line"
    return lines[-1]


@pytest.mark.parametrize("declare_length", [True, False])
def test_oversized_health_body_counts_as_unreachable(tmp_path, declare_length):
    stdout, elapsed = _run_with_health(
        tmp_path, body_size=32 * 1024 * 1024, declare_length=declare_length
    )
    context = _context(stdout)
    assert "UNITARES Governance: OFFLINE" in context
    line = _health_log_line(tmp_path)
    assert "curl exit=0 " not in line
    assert " len=0 " in line
    assert elapsed < 15


def test_small_health_body_is_unchanged(tmp_path):
    stdout, _ = _run_with_health(tmp_path, body_size=18, declare_length=True)
    context = _context(stdout)
    assert "UNITARES Governance: ONLINE" in context
    line = _health_log_line(tmp_path)
    assert "curl exit=0 " in line
    assert " len=18 " in line


def test_body_at_the_cap_is_accepted(tmp_path):
    stdout, _ = _run_with_health(tmp_path, body_size=CAP, declare_length=False)
    assert "UNITARES Governance: ONLINE" in _context(stdout)
    assert "curl exit=0 " in _health_log_line(tmp_path)


def test_cleartext_non_loopback_server_url_is_warned(tmp_path):
    stdout, _ = _run_hook(
        tmp_path,
        "http://gov.example.test",
        extra_env={"UNITARES_AUTO_ONBOARD": "off", "UNITARES_EAGER_BIND": "off"},
    )
    context = _context(stdout)
    assert "UNITARES_SERVER_URL is not https:// or a loopback address" in context
    assert "bundled MCP server are denied" in context


def test_loopback_server_url_is_not_warned(tmp_path):
    stdout, _ = _run_with_health(tmp_path, body_size=18, declare_length=True)
    assert "UNITARES_SERVER_URL is not https://" not in _context(stdout)


def _curl_ignoring_max_filesize(tmp_path: Path) -> str:
    """A PATH with a curl that drops --max-filesize, as curl before 8.4 does
    for a response that declares no length."""
    shim_dir = tmp_path / "old-curl"
    shim_dir.mkdir()
    shim = shim_dir / "curl"
    shim.write_text(
        "#!/bin/sh\n"
        "skip=0\n"
        "for arg do\n"
        "  shift\n"
        "  if [ \"$skip\" = 1 ]; then skip=0; continue; fi\n"
        "  if [ \"$arg\" = --max-filesize ]; then skip=1; continue; fi\n"
        "  set -- \"$@\" \"$arg\"\n"
        "done\n"
        "exec /usr/bin/curl \"$@\"\n"
    )
    shim.chmod(0o755)
    return f"{shim_dir}:/usr/bin:/bin:/usr/local/bin"


@pytest.mark.parametrize("overshoot", [1, 100, 4096])
def test_slightly_oversized_body_fails_even_when_curl_ignores_the_cap(
    tmp_path, overshoot
):
    _HealthHandler.body_size = CAP + overshoot
    _HealthHandler.declare_length = False
    srv = _ReusableTCPServer(("127.0.0.1", 0), _HealthHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        stdout, _ = _run_hook(
            tmp_path,
            f"http://127.0.0.1:{port}",
            extra_env={
                "UNITARES_AUTO_ONBOARD": "off",
                "UNITARES_EAGER_BIND": "off",
                "PATH": _curl_ignoring_max_filesize(tmp_path),
            },
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)
    assert "UNITARES Governance: OFFLINE" in _context(stdout)
    line = _health_log_line(tmp_path)
    assert " len=0 " in line
