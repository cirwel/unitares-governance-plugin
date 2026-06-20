"""Tests for the strict thread-anchor contract canary."""

from __future__ import annotations

import http.server
import json
import socketserver
import subprocess
import threading
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parent.parent
CANARY = PLUGIN_ROOT / "scripts" / "dev" / "strict_thread_anchor_contract.py"


class StrictAnchorHandler(http.server.BaseHTTPRequestHandler):
    calls: list[dict] = []
    bindings: dict[str, str] = {}
    allow_bare_anchor = False

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        StrictAnchorHandler.calls.append(data)

        name = data.get("name")
        args = data.get("arguments") or {}
        if name != "onboard":
            payload = {"result": {"success": False, "error": f"unexpected tool: {name}"}}
        else:
            payload = {"result": self._onboard(args)}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def _onboard(self, args: dict) -> dict:
        anchor = args.get("client_session_id")
        orchestrated = args.get("orchestrated") is True
        if anchor and not orchestrated and not StrictAnchorHandler.allow_bare_anchor:
            return {
                "success": False,
                "status": "lineage_declaration_required",
                "error": "strict identity requires lineage or orchestrated anchor",
            }

        if anchor:
            uuid = StrictAnchorHandler.bindings.setdefault(
                anchor,
                "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
            )
        else:
            uuid = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
        return {
            "success": True,
            "uuid": uuid,
            "agent_id": f"Canary_{uuid[:8]}",
            "client_session_id": anchor or f"agent-{uuid[:12]}",
            "session_resolution_source": "explicit_client_session_id",
            "display_name": args.get("name", "canary"),
        }

    def log_message(self, *args, **kwargs):
        pass


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def _run_canary(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(CANARY), "--json", *args],
        cwd=str(PLUGIN_ROOT),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


def _with_server(allow_bare_anchor: bool = False):
    StrictAnchorHandler.calls = []
    StrictAnchorHandler.bindings = {}
    StrictAnchorHandler.allow_bare_anchor = allow_bare_anchor
    server = ReusableTCPServer(("127.0.0.1", 0), StrictAnchorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_local_envelope_contract_passes() -> None:
    result = _run_canary()

    assert result.returncode == 0, result.stderr
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["results"][0]["code"] == "local_envelope_ok"


def test_live_contract_requires_bare_refusal_and_orchestrated_resume() -> None:
    server, thread = _with_server()
    try:
        server_url = f"http://127.0.0.1:{server.server_address[1]}"
        result = _run_canary(
            "--live",
            "--server-url", server_url,
            "--anchor", "agent:/thread-contract-test",
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = _payload(result)
    assert payload["ok"] is True
    assert [item["code"] for item in payload["results"]] == [
        "local_envelope_ok",
        "live_contract_ok",
    ]

    calls = StrictAnchorHandler.calls
    assert len(calls) == 3
    assert calls[0]["arguments"]["client_session_id"] == "agent:/thread-contract-test"
    assert "orchestrated" not in calls[0]["arguments"]
    assert calls[1]["arguments"]["orchestrated"] is True
    assert calls[2]["arguments"]["orchestrated"] is True


def test_live_contract_fails_when_bare_anchor_mints() -> None:
    server, thread = _with_server(allow_bare_anchor=True)
    try:
        server_url = f"http://127.0.0.1:{server.server_address[1]}"
        result = _run_canary(
            "--live",
            "--server-url", server_url,
            "--anchor", "agent:/thread-contract-test",
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result.returncode == 2
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["results"][-1]["code"] == "live_bare_anchor_not_refused"
