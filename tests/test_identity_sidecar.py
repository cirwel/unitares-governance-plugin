"""Tests for the local UNITARES identity sidecar."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer

import pytest

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from identity_sidecar import IdentitySidecar  # noqa: E402


class _ReusableTCPServer(TCPServer):
    allow_reuse_address = True


class FakeGovernanceHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, _fmt, *_args):  # pragma: no cover
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.calls.append(data)
        if self.path.startswith("/mcp"):
            if data.get("method") == "tools/call":
                params = data.get("params") or {}
                name = params.get("name")
                if name in {"process_agent_update", "sync_state"}:
                    result = {
                        "content": [{"type": "text", "text": json.dumps({
                            "success": True,
                            "verdict": {"value": "proceed"},
                            "identity_assurance": {"tier": "strong"},
                        })}]
                    }
                else:
                    result = {
                        "content": [{"type": "text", "text": json.dumps({
                            "success": True,
                            "echo": params.get("arguments", {}),
                        })}]
                    }
            else:
                result = {"tools": [{"name": "knowledge"}]}
            body = json.dumps({
                "jsonrpc": "2.0",
                "id": data.get("id"),
                "result": result,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        name = data.get("name")
        if name in {"onboard", "start_session"}:
            payload = {
                "success": True,
                "uuid": "11111111-2222-4333-8444-555555555555",
                "agent_id": "Sidecar_Test",
                "client_session_id": "agent-sidecar-111",
                "continuity_token": "v1.transient",
                "continuity_token_supported": True,
                "session_resolution_source": "force_new",
                "display_name": "sidecar-test",
            }
        elif name in {"process_agent_update", "sync_state"}:
            payload = {
                "success": True,
                "verdict": {"value": "proceed"},
                "identity_assurance": {"tier": "strong"},
            }
        else:
            payload = {"success": True, "echo": data.get("arguments", {})}

        body = json.dumps({"result": payload}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def fake_server():
    FakeGovernanceHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), FakeGovernanceHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        thread.join(timeout=2)
        srv.server_close()


def _sidecar(tmp_path: Path, fake_server: str) -> IdentitySidecar:
    return IdentitySidecar(
        server_url=fake_server,
        workspace=tmp_path,
        agent_name="sidecar-agent",
        model_type="sidecar-test",
        default_slot="slot-a",
        log_path=tmp_path / "checkins.log",
    )


def test_tool_proxy_lazy_onboards_and_injects_client_session_id(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.tool_call(
        {"name": "knowledge", "arguments": {"action": "search", "query": "x"}},
        headers={},
    )

    assert status == 200
    assert payload["result"]["success"] is True
    assert [call["name"] for call in FakeGovernanceHandler.calls] == ["onboard", "knowledge"]
    knowledge_args = FakeGovernanceHandler.calls[1]["arguments"]
    assert knowledge_args["client_session_id"] == "agent-sidecar-111"
    cache = json.loads((tmp_path / ".unitares" / "session-slot-a.json").read_text())
    assert cache["client_session_id"] == "agent-sidecar-111"
    assert "continuity_token" not in cache


def test_turn_checkin_lazy_onboards_then_sends_real_checkin(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.turn_checkin(
        {"response_text": "did work", "complexity": 0.2, "confidence": 0.8},
        headers={},
    )

    assert status == 200
    assert payload["success"] is True
    assert [call["name"] for call in FakeGovernanceHandler.calls] == ["onboard", "process_agent_update"]
    checkin_args = FakeGovernanceHandler.calls[1]["arguments"]
    assert checkin_args["client_session_id"] == "agent-sidecar-111"
    assert checkin_args["response_text"] == "did work"
    cache = json.loads((tmp_path / ".unitares" / "session-slot-a.json").read_text())
    assert "last_checkin_ts" in cache


def test_bare_onboard_through_proxy_gets_force_new_and_updates_cache(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, _payload = sidecar.tool_call({"name": "onboard", "arguments": {}}, headers={})

    assert status == 200
    sent = FakeGovernanceHandler.calls[0]["arguments"]
    assert sent["force_new"] is True
    assert sent["name"] == "sidecar-agent"
    cache = json.loads((tmp_path / ".unitares" / "session-slot-a.json").read_text())
    assert cache["uuid"] == "11111111-2222-4333-8444-555555555555"
    assert "continuity_token" not in cache


def test_explicit_proof_field_skips_lazy_onboard_and_injection(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, _payload = sidecar.tool_call(
        {"name": "knowledge", "arguments": {"action": "search", "client_session_id": "agent-explicit"}},
        headers={},
    )

    assert status == 200
    assert [call["name"] for call in FakeGovernanceHandler.calls] == ["knowledge"]
    assert FakeGovernanceHandler.calls[0]["arguments"]["client_session_id"] == "agent-explicit"


def test_audit_endpoint_reports_local_contract_findings(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    (cache_dir / "session-bad.json").write_text(json.dumps({"continuity_token": "v1.bad"}))

    payload = sidecar.audit()

    assert payload["success"] is True
    assert payload["errors"] == 2
    assert {f["code"] for f in payload["findings"]} == {
        "session_cache_token_at_rest",
        "session_cache_missing_identity",
    }


def test_audit_endpoint_uses_bounded_log_tail(tmp_path: Path, fake_server: str) -> None:
    log = tmp_path / "checkins.log"
    log.write_text(
        "\n".join([
            "2026-06-16T00:00:00Z | slot=s1 | event=turn_stop | uuid=u1 | status=fail",
            "2026-06-16T00:01:00Z | slot=s1 | event=turn_stop | uuid=u1 | status=sent",
        ]) + "\n",
        encoding="utf-8",
    )
    sidecar = IdentitySidecar(
        server_url=fake_server,
        workspace=tmp_path,
        agent_name="sidecar-agent",
        model_type="sidecar-test",
        default_slot="slot-a",
        log_path=log,
        audit_log_tail=1,
    )

    payload = sidecar.audit()

    assert payload["log_tail"] == 1
    assert payload["warnings"] == 0


def test_mcp_tools_call_lazy_onboards_and_injects(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.mcp_proxy(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "knowledge", "arguments": {"action": "search"}},
        },
        headers={},
        path="/mcp/",
    )

    assert status == 200
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 7
    assert [call.get("name") or call.get("method") for call in FakeGovernanceHandler.calls] == [
        "onboard",
        "tools/call",
    ]
    mcp_call = FakeGovernanceHandler.calls[1]
    assert mcp_call["params"]["arguments"]["client_session_id"] == "agent-sidecar-111"
    cache = json.loads((tmp_path / ".unitares" / "session-slot-a.json").read_text())
    assert cache["client_session_id"] == "agent-sidecar-111"
    assert "continuity_token" not in cache


def test_mcp_non_tool_call_passes_through_without_onboard(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.mcp_proxy(
        {"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}},
        headers={},
        path="/mcp/",
    )

    assert status == 200
    assert payload["result"]["tools"] == [{"name": "knowledge"}]
    assert [call.get("method") for call in FakeGovernanceHandler.calls] == ["tools/list"]
    assert not (tmp_path / ".unitares").exists()


def test_mcp_checkin_stamps_existing_session(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    (cache_dir / "session-slot-a.json").write_text(json.dumps({
        "uuid": "11111111-2222-4333-8444-555555555555",
        "client_session_id": "agent-sidecar-111",
        "schema_version": 2,
    }))

    status, _payload = sidecar.mcp_proxy(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "sync_state", "arguments": {"response_text": "did work"}},
        },
        headers={},
        path="/mcp/",
    )

    assert status == 200
    assert [call.get("method") for call in FakeGovernanceHandler.calls] == ["tools/call"]
    sent_args = FakeGovernanceHandler.calls[0]["params"]["arguments"]
    assert sent_args["client_session_id"] == "agent-sidecar-111"
    cache = json.loads((cache_dir / "session-slot-a.json").read_text())
    assert "last_checkin_ts" in cache
