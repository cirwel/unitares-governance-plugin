"""Tests for the local UNITARES identity sidecar."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer

import pytest

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import _session_cache_io  # noqa: E402
import identity_sidecar  # noqa: E402
import session_cache  # noqa: E402
from identity_sidecar import (  # noqa: E402
    BoundedThreadingHTTPServer,
    IdentitySidecar,
    make_handler,
)


class _ReusableTCPServer(TCPServer):
    allow_reuse_address = True


class _ReusableThreadingHTTPServer(BoundedThreadingHTTPServer):
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
                elif name in {"onboard", "start_session"}:
                    result = {
                        "content": [{"type": "text", "text": json.dumps({
                            "success": True,
                            "uuid": "11111111-2222-4333-8444-555555555555",
                            "agent_id": "Sidecar_Test",
                            "client_session_id": "agent-sidecar-111",
                            "continuity_token": "v1.transientpayloadpayloadpayload.signaturesignaturesignature",
                            "raw_governance": {
                                "continuity_token": "v1.transientpayloadpayloadpayload.signaturesignaturesignature",
                                "client_session_id": "agent-sidecar-111",
                            },
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
                "raw_governance": {
                    "continuity_token": "v1.transientpayloadpayloadpayload.signaturesignaturesignature",
                    "client_session_id": "agent-sidecar-111",
                },
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


def _serve_sidecar(sidecar: IdentitySidecar):
    srv = _ReusableThreadingHTTPServer(("127.0.0.1", 0), make_handler(sidecar))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, thread, f"http://127.0.0.1:{srv.server_address[1]}"


def _stop_server(srv: TCPServer, thread: threading.Thread) -> None:
    srv.shutdown()
    thread.join(timeout=2)
    srv.server_close()


def _http_post_json(url: str, payload: object, *, headers: dict[str, str] | None = None) -> tuple[int, object]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return int(resp.status), json.loads(resp.read().decode("utf-8"))


def _http_get_json(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, object]:
    req = urllib.request.Request(url, method="GET")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return int(resp.status), json.loads(resp.read().decode("utf-8"))


def _json_text(payload: object) -> str:
    return json.dumps(payload, sort_keys=True)


def _mcp_text_payload(payload: dict) -> dict:
    text = payload["result"]["content"][0]["text"]
    return json.loads(text)


def test_tool_proxy_lazy_onboards_and_injects_client_session_id(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.tool_call(
        {"name": "knowledge", "arguments": {"action": "search", "query": "x"}},
        headers={},
    )

    assert status == 200
    assert payload["result"]["success"] is True
    assert "client_session_id" not in _json_text(payload)
    assert "continuity_token" not in _json_text(payload)
    assert "raw_governance" not in _json_text(payload)
    assert [call["name"] for call in FakeGovernanceHandler.calls] == ["onboard", "knowledge"]
    knowledge_args = FakeGovernanceHandler.calls[1]["arguments"]
    assert knowledge_args["client_session_id"] == "agent-sidecar-111"
    cache = json.loads((tmp_path / ".unitares" / "session-slot-a.json").read_text())
    assert cache["client_session_id"] == "agent-sidecar-111"
    assert "continuity_token" not in cache


def test_concurrent_ensure_session_is_single_flight(
    tmp_path: Path,
    fake_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    sidecar = _sidecar(tmp_path, fake_server)
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def blocked_onboard(**kwargs):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=5)
        payload = {
            "uuid": "11111111-2222-4333-8444-555555555555",
            "client_session_id": "agent-single-flight",
        }
        sidecar.write_session("slot-a", payload)
        return {"status": "ok", **payload}

    monkeypatch.setattr(identity_sidecar, "run_onboard", blocked_onboard)
    results: list[dict] = []
    workers = [
        threading.Thread(target=lambda: results.append(sidecar.ensure_session("slot-a")))
        for _ in range(2)
    ]
    workers[0].start()
    assert started.wait(timeout=5)
    workers[1].start()
    release.set()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert calls == 1
    assert len(results) == 2
    assert {result["client_session_id"] for result in results} == {
        "agent-single-flight"
    }


def test_ensure_session_waits_for_inflight_authority_replace(
    tmp_path: Path,
    fake_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    sidecar = _sidecar(tmp_path, fake_server)
    sidecar.write_session(
        "slot-a",
        {
            "schema_version": 2,
            "slot": "slot-a",
            "uuid": "11111111-2222-4333-8444-555555555555",
            "client_session_id": "agent-single-flight",
        },
    )
    home_cache = fake_home / ".unitares" / "session-slot-a.json"
    authority_path = _session_cache_io.cache_authority_path(home_cache)
    in_flight = threading.Event()
    release_writer = threading.Event()

    def transient_writer() -> None:
        with identity_sidecar.session_cache_lock(authority_path):
            authority_path.write_text(
                json.dumps({"schema_version": 1, "generation": 2, "mirror_valid": False}),
                encoding="utf-8",
            )
            in_flight.set()
            assert release_writer.wait(timeout=5)
            authority_path.write_text(
                json.dumps({"schema_version": 1, "generation": 2, "mirror_valid": True}),
                encoding="utf-8",
            )

    writer = threading.Thread(target=transient_writer)
    writer.start()
    assert in_flight.wait(timeout=5)
    reader_result: list[dict] = []
    reader = threading.Thread(target=lambda: reader_result.append(sidecar.ensure_session("slot-a")))
    reader.start()
    release_writer.set()
    writer.join(timeout=5)
    reader.join(timeout=5)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert len(reader_result) == 1
    assert reader_result[0]["status"] == "cached"
    assert reader_result[0]["client_session_id"] == "agent-single-flight"
    assert FakeGovernanceHandler.calls == []


def test_ensure_session_distinguishes_post_onboard_cache_lock_timeout(
    tmp_path: Path,
    fake_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    sidecar = _sidecar(tmp_path, fake_server)

    def ok_onboard(**kwargs):
        return {
            "status": "ok",
            "uuid": "11111111-2222-4333-8444-555555555555",
            "client_session_id": "agent-single-flight",
        }

    def timeout_update_cache(*args, **kwargs):
        raise TimeoutError("simulated cache lock timeout")

    monkeypatch.setattr(identity_sidecar, "run_onboard", ok_onboard)
    monkeypatch.setattr(identity_sidecar, "_update_cache", timeout_update_cache)

    result = sidecar.ensure_session("slot-a")
    assert result["status"] == "cache_lock_timeout"


def test_workspace_session_mirror_cannot_override_protected_binding(
    tmp_path: Path,
    fake_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    sidecar = _sidecar(tmp_path, fake_server)
    sidecar.write_session(
        "slot-a",
        {
            "schema_version": 2,
            "slot": "slot-a",
            "uuid": "protected-uuid",
            "client_session_id": "agent-protected",
        },
    )
    workspace_cache = tmp_path / ".unitares" / "session-slot-a.json"
    workspace_cache.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "slot": "slot-a",
                "uuid": "attacker-uuid",
                "client_session_id": "agent-attacker",
            }
        ),
        encoding="utf-8",
    )

    result = sidecar.ensure_session("slot-a")

    assert result["status"] == "cached"
    assert result["client_session_id"] == "agent-protected"
    assert json.loads(workspace_cache.read_text(encoding="utf-8"))["uuid"] == "protected-uuid"
    assert FakeGovernanceHandler.calls == []


def test_missing_workspace_mirror_is_restored_from_protected_binding(
    tmp_path: Path,
    fake_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    sidecar = _sidecar(tmp_path, fake_server)
    sidecar.write_session(
        "slot-a",
        {
            "schema_version": 2,
            "slot": "slot-a",
            "uuid": "protected-uuid",
            "client_session_id": "agent-protected",
        },
    )
    workspace_cache = tmp_path / ".unitares" / "session-slot-a.json"
    workspace_cache.unlink()

    result = sidecar.ensure_session("slot-a")

    assert result["status"] == "cached"
    assert result["client_session_id"] == "agent-protected"
    assert json.loads(workspace_cache.read_text(encoding="utf-8"))["uuid"] == "protected-uuid"
    assert FakeGovernanceHandler.calls == []


def test_invalid_existing_protected_binding_requires_explicit_clear(
    tmp_path: Path,
    fake_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    protected = fake_home / ".unitares" / "session-slot-a.json"
    protected.parent.mkdir(parents=True)
    protected.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "slot": "wrong-slot",
                "uuid": "stale",
                "client_session_id": "agent-stale",
            }
        ),
        encoding="utf-8",
    )
    protected.chmod(0o600)
    monkeypatch.setenv("HOME", str(fake_home))
    sidecar = _sidecar(tmp_path, fake_server)

    result = sidecar.ensure_session("slot-a")

    assert result["status"] == "invalid_protected_binding"
    assert FakeGovernanceHandler.calls == []


def test_clear_supersedes_inflight_sidecar_identity_response(
    tmp_path: Path,
    fake_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    sidecar = _sidecar(tmp_path, fake_server)
    started = threading.Event()
    release = threading.Event()
    result: list[tuple[int, dict]] = []

    def blocked_post(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=5)
        return (
            {
                "result": {
                    "success": True,
                    "uuid": "99999999-2222-4333-8444-555555555555",
                    "client_session_id": "agent-too-late",
                }
            },
            1,
            None,
        )

    monkeypatch.setattr(identity_sidecar, "_post_json", blocked_post)
    worker = threading.Thread(
        target=lambda: result.append(
            sidecar.tool_call({"name": "onboard", "arguments": {}}, headers={})
        )
    )
    worker.start()
    assert started.wait(timeout=5)
    assert session_cache.cmd_clear(
        argparse.Namespace(kind="session", workspace=str(tmp_path), slot="slot-a")
    ) == 0
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert result[0][0] == 200
    assert sidecar.read_session("slot-a") == {}
    assert not (fake_home / ".unitares" / "session-slot-a.json").exists()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "example.com"])
def test_standalone_sidecar_rejects_non_loopback_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    monkeypatch.setenv("UNITARES_SIDECAR_CAPABILITY", "test-capability")
    with pytest.raises(SystemExit, match="2"):
        identity_sidecar.main(["--host", host, "--workspace", str(tmp_path)])


def test_turn_checkin_lazy_onboards_then_sends_real_checkin(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.turn_checkin(
        {"response_text": "did work", "complexity": 0.2, "confidence": 0.8},
        headers={},
    )

    assert status == 200
    assert payload["success"] is True
    assert payload["session_bound"] is True
    assert "client_session_id" not in payload
    assert [call["name"] for call in FakeGovernanceHandler.calls] == ["onboard", "process_agent_update"]
    checkin_args = FakeGovernanceHandler.calls[1]["arguments"]
    assert checkin_args["client_session_id"] == "agent-sidecar-111"
    assert checkin_args["response_text"] == "did work"
    cache = json.loads((tmp_path / ".unitares" / "session-slot-a.json").read_text())
    assert "last_checkin_ts" in cache


def test_bare_onboard_through_proxy_gets_force_new_and_updates_cache(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.tool_call({"name": "onboard", "arguments": {}}, headers={})

    assert status == 200
    assert payload["result"]["success"] is True
    assert "client_session_id" not in _json_text(payload)
    assert "continuity_token" not in _json_text(payload)
    assert "raw_governance" not in _json_text(payload)
    sent = FakeGovernanceHandler.calls[0]["arguments"]
    assert sent["force_new"] is True
    assert sent["name"] == "sidecar-agent"
    cache = json.loads((tmp_path / ".unitares" / "session-slot-a.json").read_text())
    assert cache["uuid"] == "11111111-2222-4333-8444-555555555555"
    assert "continuity_token" not in cache


def test_foreign_explicit_proof_cannot_override_configured_slot(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    sidecar.write_session("slot-a", {
        "uuid": "11111111-2222-4333-8444-555555555555",
        "client_session_id": "agent-sidecar-111",
        "schema_version": 2,
        "slot": "slot-a",
    })

    status, payload = sidecar.tool_call(
        {"name": "knowledge", "arguments": {"action": "search", "client_session_id": "agent-explicit"}},
        headers={},
    )

    assert status == 409
    assert "configured slot binding" in payload["error"]
    assert FakeGovernanceHandler.calls == []


@pytest.mark.parametrize("proof_field", identity_sidecar.SLOT_BOUND_PROOF_FIELDS)
def test_foreign_nested_gateway_proof_cannot_override_configured_slot(
    tmp_path: Path,
    fake_server: str,
    proof_field: str,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    sidecar.write_session("slot-a", {
        "uuid": "11111111-2222-4333-8444-555555555555",
        "agent_id": "Sidecar_Test",
        "client_session_id": "agent-sidecar-111",
        "schema_version": 2,
        "slot": "slot-a",
    })

    status, payload = sidecar.tool_call(
        {
            "name": "use_tool",
            "arguments": {
                "tool_name": "knowledge",
                "arguments": {"action": "search", proof_field: "foreign-proof"},
            },
        },
        headers={},
    )

    assert status == 409
    assert f"arguments.{proof_field}" in payload["error"]
    assert FakeGovernanceHandler.calls == []


def _slot_a(sidecar) -> None:
    sidecar.write_session("slot-a", {
        "uuid": "11111111-2222-4333-8444-555555555555",
        "agent_id": "Sidecar_Test",
        "client_session_id": "agent-sidecar-111",
        "schema_version": 2,
        "slot": "slot-a",
    })


@pytest.mark.parametrize("target_field", ["agent_id", "agent_uuid"])
def test_nested_lookup_target_is_not_treated_as_a_slot_proof(
    tmp_path: Path, fake_server: str, target_field: str,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _slot_a(sidecar)

    status, _payload = sidecar.tool_call(
        {
            "name": "use_tool",
            "arguments": {
                "tool_name": "observe",
                "arguments": {target_field: "another-agent"},
            },
        },
        headers={},
    )

    assert status == 200
    sent = FakeGovernanceHandler.calls[0]["arguments"]
    assert sent["client_session_id"] == "agent-sidecar-111"
    assert sent["arguments"][target_field] == "another-agent"


@pytest.mark.parametrize(
    "outer_name, inner_tool",
    [
        ("USE_TOOL", "knowledge"),
        (" use_tool ", "knowledge"),
        ("mcp__unitares-governance__use_tool", "knowledge"),
        ("mcp_unitares_use_tool", "knowledge"),
        ("use_tool", "mcp_unitares_knowledge"),
    ],
)
def test_gateway_name_variants_cannot_skip_the_nested_proof_check(
    tmp_path: Path, fake_server: str, outer_name: str, inner_tool: str,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _slot_a(sidecar)
    inner = {"action": "search", "client_session_id": "agent-foreign-111"}

    status, payload = sidecar.tool_call(
        {"name": outer_name, "arguments": {"tool_name": inner_tool, "arguments": inner}},
        headers={},
    )

    assert status == 409, payload
    assert "client_session_id" in payload["error"]
    assert FakeGovernanceHandler.calls == []


@pytest.mark.parametrize("nested", ['{"client_session_id": "agent-foreign-111"}', ["x"], 7])
def test_non_object_gateway_arguments_are_refused(
    tmp_path: Path, fake_server: str, nested: object,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _slot_a(sidecar)

    status, payload = sidecar.tool_call(
        {"name": "use_tool", "arguments": {"tool_name": "knowledge", "arguments": nested}},
        headers={},
    )

    assert status == 409
    assert "not an object" in payload["error"]
    assert FakeGovernanceHandler.calls == []


@pytest.mark.parametrize("inner_tool", ["use_tool", "mcp__unitares__use_tool", "mcp_unitares_use_tool"])
def test_gateway_invoking_the_gateway_is_refused(
    tmp_path: Path, fake_server: str, inner_tool: str,
) -> None:
    # The server refuses use_tool invoking itself, so only one gateway layer
    # exists and the sidecar checks exactly that one.
    sidecar = _sidecar(tmp_path, fake_server)
    _slot_a(sidecar)

    status, payload = sidecar.tool_call(
        {
            "name": "use_tool",
            "arguments": {
                "tool_name": inner_tool,
                "arguments": {"tool_name": "knowledge", "arguments": {"action": "search"}},
            },
        },
        headers={},
    )

    assert status == 409
    assert "use_tool invoking use_tool" in payload["error"]
    assert FakeGovernanceHandler.calls == []


def test_matching_explicit_proof_is_allowed_for_configured_slot(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    sidecar.write_session("slot-a", {
        "uuid": "11111111-2222-4333-8444-555555555555",
        "client_session_id": "agent-sidecar-111",
        "schema_version": 2,
        "slot": "slot-a",
    })

    status, _payload = sidecar.tool_call(
        {"name": "knowledge", "arguments": {"action": "search", "client_session_id": "agent-sidecar-111"}},
        headers={},
    )

    assert status == 200
    assert [call["name"] for call in FakeGovernanceHandler.calls] == ["knowledge"]
    assert FakeGovernanceHandler.calls[0]["arguments"]["client_session_id"] == "agent-sidecar-111"


def test_matching_nested_gateway_proof_is_allowed_for_configured_slot(
    tmp_path: Path,
    fake_server: str,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    sidecar.write_session("slot-a", {
        "uuid": "11111111-2222-4333-8444-555555555555",
        "client_session_id": "agent-sidecar-111",
        "schema_version": 2,
        "slot": "slot-a",
    })

    status, _payload = sidecar.tool_call(
        {
            "name": "use_tool",
            "arguments": {
                "tool_name": "knowledge",
                "arguments": {
                    "action": "search",
                    "client_session_id": "agent-sidecar-111",
                },
            },
        },
        headers={},
    )

    assert status == 200
    sent = FakeGovernanceHandler.calls[0]["arguments"]
    assert sent["client_session_id"] == "agent-sidecar-111"
    assert sent["arguments"]["client_session_id"] == "agent-sidecar-111"


def test_recursively_nested_foreign_gateway_proof_is_rejected(
    tmp_path: Path,
    fake_server: str,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    sidecar.write_session("slot-a", {
        "uuid": "11111111-2222-4333-8444-555555555555",
        "client_session_id": "agent-sidecar-111",
        "schema_version": 2,
        "slot": "slot-a",
    })

    status, payload = sidecar.tool_call(
        {
            "name": "use_tool",
            "arguments": {
                "tool_name": "use_tool",
                "arguments": {
                    "tool_name": "knowledge",
                    "arguments": {
                        "action": "search",
                        "client_session_id": "agent-foreign-111",
                    },
                },
            },
        },
        headers={},
    )

    # The server refuses use_tool invoking itself, so the sidecar refuses the
    # nested gateway outright rather than checking a layer that never runs.
    assert status == 409
    assert "use_tool invoking use_tool" in payload["error"]
    assert FakeGovernanceHandler.calls == []
    assert FakeGovernanceHandler.calls == []


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
    visible = _mcp_text_payload(payload)
    assert visible["echo"]["action"] == "search"
    assert "client_session_id" not in visible["echo"]
    assert "agent-sidecar-111" not in _json_text(payload)
    cache = json.loads((tmp_path / ".unitares" / "session-slot-a.json").read_text())
    assert cache["client_session_id"] == "agent-sidecar-111"
    assert "continuity_token" not in cache


def test_mcp_foreign_nested_gateway_proof_is_rejected(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    sidecar.write_session("slot-a", {
        "uuid": "11111111-2222-4333-8444-555555555555",
        "client_session_id": "agent-sidecar-111",
        "schema_version": 2,
        "slot": "slot-a",
    })

    status, payload = sidecar.mcp_proxy(
        {
            "jsonrpc": "2.0",
            "id": 71,
            "method": "tools/call",
            "params": {
                "name": "use_tool",
                "arguments": {
                    "tool_name": "knowledge",
                    "arguments": {
                        "action": "search",
                        "client_session_id": "agent-foreign-111",
                    },
                },
            },
        },
        headers={},
        path="/mcp/",
    )

    assert status == 409
    assert "arguments.client_session_id" in payload["error"]["message"]
    assert FakeGovernanceHandler.calls == []


def test_mcp_start_session_response_hides_proof_material(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.mcp_proxy(
        {
            "jsonrpc": "2.0",
            "id": 70,
            "method": "tools/call",
            "params": {"name": "start_session", "arguments": {}},
        },
        headers={},
        path="/mcp/",
    )

    assert status == 200
    visible = _mcp_text_payload(payload)
    assert visible["success"] is True
    assert visible["uuid"] == "11111111-2222-4333-8444-555555555555"
    assert "client_session_id" not in visible
    assert "continuity_token" not in visible
    assert "raw_governance" not in visible
    assert "agent-sidecar-111" not in _json_text(payload)


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
    sidecar.write_session("slot-a", {
        "uuid": "11111111-2222-4333-8444-555555555555",
        "client_session_id": "agent-sidecar-111",
        "schema_version": 2,
        "slot": "slot-a",
    })

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


def test_http_client_config_returns_slot_scoped_mcp_snippet(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    srv, thread, base_url = _serve_sidecar(sidecar)
    try:
        status, payload = _http_get_json(
            f"{base_url}/client-config?slot=codex-local",
            headers={"Authorization": f"Bearer {sidecar.capability_token}"},
        )
    finally:
        _stop_server(srv, thread)

    assert status == 200
    assert isinstance(payload, dict)
    assert payload["success"] is True
    assert payload["sidecar_url"] == base_url
    assert payload["slot"] == "slot-a"
    assert payload["transport"]["mcp"]["mode"] == "jsonrpc-http"
    assert payload["transport"]["mcp"]["url"] == f"{base_url}/mcp/"
    config = payload["mcp_config"]["mcpServers"]["unitares-governance-sidecar"]
    assert config == {
        "type": "url",
        "url": f"{base_url}/mcp/",
        "headers": {
            "Authorization": f"Bearer {sidecar.capability_token}",
            "X-UNITARES-Slot": "slot-a",
        },
    }


def test_http_mcp_route_lazy_onboards_and_injects_client_session_id(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    srv, thread, base_url = _serve_sidecar(sidecar)
    try:
        status, payload = _http_post_json(
            f"{base_url}/mcp/",
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "knowledge", "arguments": {"action": "search"}},
            },
            headers={
                "Authorization": f"Bearer {sidecar.capability_token}",
                "X-UNITARES-Slot": "codex-http",
            },
        )
    finally:
        _stop_server(srv, thread)

    assert status == 200
    assert isinstance(payload, dict)
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 10
    assert [call.get("name") or call.get("method") for call in FakeGovernanceHandler.calls] == [
        "onboard",
        "tools/call",
    ]
    mcp_args = FakeGovernanceHandler.calls[1]["params"]["arguments"]
    assert mcp_args["client_session_id"] == "agent-sidecar-111"
    visible = _mcp_text_payload(payload)
    assert visible["echo"]["action"] == "search"
    assert "client_session_id" not in visible["echo"]
    cache = json.loads((tmp_path / ".unitares" / "session-slot-a.json").read_text())
    assert cache["client_session_id"] == "agent-sidecar-111"
    assert "continuity_token" not in cache


def test_http_session_start_hides_onboard_proof_material(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    srv, thread, base_url = _serve_sidecar(sidecar)
    try:
        status, payload = _http_post_json(
            f"{base_url}/session/start",
            {"agent_name": "sidecar-agent", "slot": "codex-start"},
            headers={"Authorization": f"Bearer {sidecar.capability_token}"},
        )
    finally:
        _stop_server(srv, thread)

    assert status == 200
    assert isinstance(payload, dict)
    assert payload["success"] is True
    assert payload["result"]["status"] == "ok"
    assert "client_session_id" not in _json_text(payload)
    assert "continuity_token" not in _json_text(payload)
    assert "raw_governance" not in _json_text(payload)


@pytest.mark.parametrize("path", ["/health", "/client-config", "/session", "/audit"])
def test_every_get_route_requires_the_sidecar_capability(
    tmp_path: Path, fake_server: str, path: str
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    srv, thread, base_url = _serve_sidecar(sidecar)
    try:
        with pytest.raises(urllib.error.HTTPError) as raised:
            _http_get_json(f"{base_url}{path}")
    finally:
        _stop_server(srv, thread)
    assert raised.value.code == 401


def test_startup_proof_authenticates_listener_without_disclosing_capability(
    tmp_path: Path, fake_server: str
) -> None:
    sidecar = IdentitySidecar(
        server_url=fake_server,
        workspace=tmp_path,
        agent_name="sidecar-agent",
        model_type="sidecar-test",
        default_slot="slot-a",
        startup_secret="separate-startup-secret",
    )
    srv, thread, base_url = _serve_sidecar(sidecar)
    challenge = "fresh-random-challenge"
    try:
        status, payload = _http_get_json(
            f"{base_url}/startup-proof?challenge={challenge}"
        )
    finally:
        _stop_server(srv, thread)

    assert status == 200
    assert payload == {
        "success": True,
        "proof": hmac.new(
            b"separate-startup-secret",
            challenge.encode(),
            hashlib.sha256,
        ).hexdigest(),
    }
    assert sidecar.capability_token not in json.dumps(payload)


def test_post_route_rejects_unauthenticated_governance_call(
    tmp_path: Path, fake_server: str
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    srv, thread, base_url = _serve_sidecar(sidecar)
    try:
        with pytest.raises(urllib.error.HTTPError) as raised:
            _http_post_json(
                f"{base_url}/v1/tools/call",
                {"name": "sync_state", "arguments": {"response_text": "forged"}},
            )
    finally:
        _stop_server(srv, thread)
    assert raised.value.code == 401
    assert FakeGovernanceHandler.calls == []


def test_post_rejects_oversize_body_before_json_parse(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    srv, thread, base_url = _serve_sidecar(sidecar)
    try:
        host, port_text = base_url.removeprefix("http://").split(":", 1)
        with socket.create_connection((host, int(port_text)), timeout=5) as client:
            client.sendall(
                (
                    "POST /v1/tools/call HTTP/1.1\r\n"
                    f"Host: {host}:{port_text}\r\n"
                    f"Authorization: Bearer {sidecar.capability_token}\r\n"
                    f"Content-Length: {identity_sidecar.MAX_REQUEST_BYTES + 1}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
            )
            response = client.recv(4096)
    finally:
        _stop_server(srv, thread)
    assert b" 413 " in response


def test_bounded_server_refuses_connections_above_worker_cap() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingHandler(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):  # pragma: no cover
            return

        def do_GET(self):  # noqa: N802
            entered.set()
            release.wait(timeout=5)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = BoundedThreadingHTTPServer(
        ("127.0.0.1", 0), BlockingHandler, max_workers=1
    )
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    first = threading.Thread(
        target=lambda: urllib.request.urlopen(f"{base_url}/hold", timeout=5).close(),
        daemon=True,
    )
    first.start()
    try:
        assert entered.wait(timeout=2)
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{base_url}/overflow", timeout=2)
        assert raised.value.code == 503
    finally:
        release.set()
        first.join(timeout=2)
        _stop_server(server, serving)


# --- Slot owner rule: a bound slot keeps its identity -----------------------

BOUND_UUID = "aaaaaaaa-2222-4333-8444-555555555555"
BOUND_CSID = "agent-bound-owner"


def _bind_slot_a(sidecar, **extra) -> dict:
    record = {
        "uuid": BOUND_UUID,
        "agent_id": "Bound_Owner",
        "client_session_id": BOUND_CSID,
        "schema_version": 2,
        "slot": "slot-a",
        **extra,
    }
    sidecar.write_session("slot-a", record)
    return sidecar.read_session("slot-a")


def _mcp_call(sidecar, name: str, arguments: dict, request_id: int = 1):
    return sidecar.mcp_proxy(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={},
        path="/mcp/",
    )


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("name", ["onboard", "start_session", "ONBOARD", "mcp__unitares__start_session"])
def test_unmarked_mint_into_a_bound_slot_is_refused_before_the_upstream_call(
    tmp_path: Path, fake_server: str, transport: str, name: str,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar)

    if transport == "rest":
        status, payload = sidecar.tool_call({"name": name, "arguments": {}}, headers={})
        message = payload["error"]
    else:
        status, payload = _mcp_call(sidecar, name, {})
        message = payload["error"]["message"]

    assert status == 409
    assert "already bound" in message
    assert FakeGovernanceHandler.calls == []
    assert sidecar.read_session("slot-a") == before


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "name, arguments",
    [
        ("onboard", {"force_new": False, "client_session_id": BOUND_CSID}),
        ("start_session", {"force_new": False, "client_session_id": BOUND_CSID}),
    ],
)
def test_a_different_identity_never_replaces_the_bound_slot(
    tmp_path: Path, fake_server: str, transport: str, name: str, arguments: dict,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar)

    if transport == "rest":
        status, payload = sidecar.tool_call({"name": name, "arguments": arguments}, headers={})
        assert payload["sidecar"]["identity_cache_update"] == "refused_owner_mismatch"
    else:
        status, _payload = _mcp_call(sidecar, name, arguments)

    assert status == 200
    assert len(FakeGovernanceHandler.calls) == 1
    assert sidecar.read_session("slot-a") == before


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "name, arguments",
    [
        ("start_session", {"force_new": True, "spawn_reason": "explicit"}),
        ("onboard", {"spawn_reason": "dispatch"}),
        ("onboard", {"parent_agent_id": BOUND_UUID}),
        ("start_session", {"parent_agent_id": BOUND_UUID, "spawn_reason": "compaction"}),
        ("identity", {"force_new": True}),
        ("identity", {"force_new": True, "spawn_reason": "explicit"}),
    ],
)
def test_a_declared_lineage_mint_into_a_bound_slot_never_reaches_the_server(
    tmp_path: Path, fake_server: str, transport: str, name: str, arguments: dict,
) -> None:
    # The sidecar cannot hold a second identity, so forwarding the mint would
    # only leave an identity on the server that no caller can use.
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar)

    if transport == "rest":
        status, payload = sidecar.tool_call({"name": name, "arguments": arguments}, headers={})
        message = payload["error"]
    else:
        status, payload = _mcp_call(sidecar, name, arguments)
        message = payload["error"]["message"]

    assert status == 409
    assert "already bound" in message
    assert "clear the slot" in message
    assert "parent_agent_id" not in message
    assert FakeGovernanceHandler.calls == []
    assert sidecar.read_session("slot-a") == before


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("bound", [False, True])
def test_a_declared_subagent_mint_is_refused_before_the_upstream_call(
    tmp_path: Path, fake_server: str, transport: str, bound: bool,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar) if bound else {}
    arguments = {"spawn_reason": "subagent", "parent_agent_id": BOUND_UUID}

    if transport == "rest":
        status, payload = sidecar.tool_call({"name": "onboard", "arguments": arguments}, headers={})
        message = payload["error"]
    else:
        status, payload = _mcp_call(sidecar, "onboard", arguments)
        message = payload["error"]["message"]

    assert status == 409
    assert "subagent" in message
    assert FakeGovernanceHandler.calls == []
    assert sidecar.read_session("slot-a") == before


def test_owner_rule_never_persists_a_declared_subagent_response(
    tmp_path: Path, fake_server: str,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    decision = sidecar.update_cache_from_identity_response(
        "slot-a",
        {"success": True, "uuid": BOUND_UUID, "client_session_id": BOUND_CSID},
        arguments={"spawn_reason": "subagent"},
    )

    assert decision == "refused_subagent"
    assert sidecar.read_session("slot-a") == {}


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "name, arguments",
    [
        ("identity", {"agent_id": "Other_Agent"}),
        ("onboard", {"force_new": False, "agent_id": "Other_Agent"}),
        ("bind_session", {"agent_id": "Other_Agent", "resume": True}),
    ],
)
def test_a_foreign_agent_id_is_refused_before_the_upstream_call(
    tmp_path: Path, fake_server: str, transport: str, name: str, arguments: dict,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar)

    if transport == "rest":
        status, payload = sidecar.tool_call({"name": name, "arguments": arguments}, headers={})
        message = payload["error"]
    else:
        status, payload = _mcp_call(sidecar, name, arguments)
        message = payload["error"]["message"]

    assert status == 409
    assert "agent_id" in message
    assert FakeGovernanceHandler.calls == []
    assert sidecar.read_session("slot-a") == before


@pytest.mark.parametrize("agent_id", ["Bound_Owner", BOUND_UUID])
def test_the_bound_agent_id_still_resumes(tmp_path: Path, fake_server: str, agent_id: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _bind_slot_a(sidecar)

    _name, arguments, _lifecycle = sidecar.prepare_tool_arguments(
        name="identity",
        arguments={"agent_id": agent_id, "client_session_id": BOUND_CSID, "resume": True},
        slot="slot-a",
    )

    assert arguments["agent_id"] == agent_id


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "name, arguments",
    [
        ("identity", {"agent_uuid": "bbbbbbbb-2222-4333-8444-555555555555", "resume": True}),
        ("identity", {"client_session_id": "agent-foreign-222"}),
        ("identity", {"agent_uuid": BOUND_UUID, "continuity_token": "v1.foreign.token"}),
        ("bind_session", {"agent_uuid": "bbbbbbbb-2222-4333-8444-555555555555"}),
        ("bind_session", {"client_session_id": "agent-foreign-222"}),
        ("onboard", {"force_new": False, "client_session_id": "agent-foreign-222"}),
        ("start_session", {"force_new": False, "client_session_id": "agent-foreign-222"}),
        (" Identity ", {"agent_uuid": "bbbbbbbb-2222-4333-8444-555555555555"}),
    ],
)
def test_foreign_identity_proof_is_refused_before_the_upstream_call(
    tmp_path: Path, fake_server: str, transport: str, name: str, arguments: dict,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar)

    if transport == "rest":
        status, payload = sidecar.tool_call({"name": name, "arguments": arguments}, headers={})
        message = payload["error"]
    else:
        status, payload = _mcp_call(sidecar, name, arguments)
        message = payload["error"]["message"]

    assert status == 409
    assert "configured slot binding" in message
    assert FakeGovernanceHandler.calls == []
    assert sidecar.read_session("slot-a") == before


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_proofless_identity_in_a_bound_slot_is_refused(
    tmp_path: Path, fake_server: str, transport: str,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar)

    if transport == "rest":
        status, payload = sidecar.tool_call({"name": "identity", "arguments": {}}, headers={})
        message = payload["error"]
    else:
        status, payload = _mcp_call(sidecar, "identity", {})
        message = payload["error"]["message"]

    assert status == 409
    assert "already bound" in message
    assert FakeGovernanceHandler.calls == []
    assert sidecar.read_session("slot-a") == before


def test_same_identity_resume_still_refreshes_the_session_id(
    tmp_path: Path, fake_server: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _bind_slot_a(sidecar, onboard_origin="agent")
    sent: list[dict] = []

    def resumed(_url, payload, *_args, **_kwargs):
        sent.append(payload)
        return (
            {"result": {"success": True, "uuid": BOUND_UUID, "client_session_id": "agent-bound-refreshed"}},
            1,
            None,
        )

    monkeypatch.setattr(identity_sidecar, "_post_json", resumed)
    status, payload = sidecar.tool_call(
        {"name": "identity", "arguments": {"agent_uuid": BOUND_UUID, "resume": True}},
        headers={},
    )

    assert status == 200
    assert payload["sidecar"]["identity_cache_update"] == "updated"
    assert sent[0]["arguments"]["agent_uuid"] == BOUND_UUID
    session = sidecar.read_session("slot-a")
    assert session["uuid"] == BOUND_UUID
    assert session["client_session_id"] == "agent-bound-refreshed"
    assert session["onboard_origin"] == "agent"


@pytest.mark.parametrize(
    "response",
    [
        {"success": False, "uuid": "bbbbbbbb-2222-4333-8444-555555555555", "client_session_id": "agent-x"},
        {
            "uuid": "bbbbbbbb-2222-4333-8444-555555555555",
            "client_session_id": "agent-x",
            "raw_governance": {"success": False},
        },
        {"client_session_id": "agent-x"},
    ],
)
def test_failed_or_uuidless_identity_response_is_not_persisted(
    tmp_path: Path, fake_server: str, monkeypatch: pytest.MonkeyPatch, response: dict,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    monkeypatch.setattr(
        identity_sidecar, "_post_json", lambda *_a, **_k: ({"result": dict(response)}, 1, None)
    )

    status, payload = sidecar.tool_call({"name": "onboard", "arguments": {}}, headers={})

    assert status == 200
    assert payload["sidecar"]["identity_cache_update"].startswith("refused_")
    assert sidecar.read_session("slot-a") == {}


def test_mcp_error_result_is_not_persisted(
    tmp_path: Path, fake_server: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": json.dumps({
                "uuid": "bbbbbbbb-2222-4333-8444-555555555555",
                "client_session_id": "agent-x",
            })}],
        },
    }
    monkeypatch.setattr(identity_sidecar, "_post_json_any", lambda *_a, **_k: (body, 200, 1, None))

    status, _payload = _mcp_call(sidecar, "onboard", {})

    assert status == 200
    assert sidecar.read_session("slot-a") == {}


def test_inherited_binding_is_not_used_for_session_proof(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _bind_slot_a(sidecar, prior_process=True)

    ensured = sidecar.ensure_session("slot-a")
    _name, arguments, lifecycle = sidecar.prepare_tool_arguments(
        name="process_agent_update",
        arguments={"response_text": "x"},
        slot="slot-a",
    )

    assert ensured["status"] == "inherited_binding"
    assert "client_session_id" not in ensured
    assert "client_session_id" not in arguments
    assert lifecycle["session_proof_injected"] is False
    assert lifecycle["ensure_session"]["status"] == "inherited_binding"
    assert FakeGovernanceHandler.calls == []

    status, payload = sidecar.turn_checkin({"response_text": "x"}, headers={})
    assert status == 502
    assert payload["success"] is False
    assert BOUND_CSID not in _json_text(FakeGovernanceHandler.calls)


# --- Gateway, inherited, proofless and bind-reply edges of the owner rule ---

FOREIGN_UUID = "bbbbbbbb-2222-4333-8444-555555555555"


def _call(sidecar, transport: str, name: str, arguments: dict) -> tuple[int, str, object]:
    if transport == "rest":
        status, payload = sidecar.tool_call({"name": name, "arguments": arguments}, headers={})
        return status, str(payload.get("error", "")), payload
    status, payload = _mcp_call(sidecar, name, arguments)
    error = payload.get("error") if isinstance(payload, dict) else None
    return status, str((error or {}).get("message", "")), payload


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize(
    "gateway",
    [
        {"tool_name": "onboard", "arguments": {"force_new": True}},
        {"tool_name": "start_session"},
        {"tool_name": "identity", "arguments": {"agent_uuid": FOREIGN_UUID, "resume": True}},
        {"tool_name": "bind_session", "arguments": {"agent_uuid": FOREIGN_UUID, "resume": True}},
        {"tool_name": " mcp__unitares__Identity ", "arguments": {}},
        {"tool_name": "use_tool", "arguments": {"tool_name": "onboard", "arguments": {}}},
    ],
)
def test_identity_tools_through_the_gateway_are_refused_before_any_upstream_call(
    tmp_path: Path, fake_server: str, transport: str, bound: bool, gateway: dict,
) -> None:
    # The gateway would dispatch the identity tool upstream under the
    # sidecar's credential and its reply would never reach the owner rule.
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar) if bound else {}

    status, message, _payload = _call(sidecar, transport, "use_tool", gateway)

    assert status == 409
    assert "use_tool" in message
    assert FakeGovernanceHandler.calls == []
    assert sidecar.read_session("slot-a") == before


@pytest.mark.parametrize("mirror", ["missing", "stale"])
def test_owner_rule_reads_the_protected_binding_not_the_workspace_mirror(
    tmp_path: Path, fake_server: str, mirror: str,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar, onboard_origin="agent", prior_process=True)
    workspace_cache = tmp_path / ".unitares" / "session-slot-a.json"
    if mirror == "missing":
        workspace_cache.unlink()
    else:
        workspace_cache.write_text(json.dumps({
            "schema_version": 2, "slot": "slot-a",
            "uuid": FOREIGN_UUID, "client_session_id": "agent-foreign-222",
        }), encoding="utf-8")

    foreign = sidecar.update_cache_from_identity_response(
        "slot-a",
        {"success": True, "uuid": FOREIGN_UUID, "client_session_id": "agent-foreign-222"},
    )
    assert foreign == "refused_owner_mismatch"
    assert sidecar.read_session("slot-a") == before

    same = sidecar.update_cache_from_identity_response(
        "slot-a",
        {"success": True, "uuid": BOUND_UUID, "client_session_id": "agent-bound-refreshed"},
    )
    assert same == "updated"
    session = sidecar.read_session("slot-a")
    assert session["client_session_id"] == "agent-bound-refreshed"
    assert session["prior_process"] is True
    assert session["onboard_origin"] == "agent"


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "name, arguments",
    [
        # The server promotes a declared parent to a fresh mint.
        ("onboard", {"force_new": False, "parent_agent_id": BOUND_UUID}),
        ("onboard", {"force_new": False, "client_session_id": BOUND_CSID, "parent_agent_id": BOUND_UUID}),
        ("start_session", {"force_new": "false", "parent_agent_id": BOUND_UUID}),
        ("identity", {"client_session_id": BOUND_CSID, "parent_agent_id": BOUND_UUID}),
        # Without proof the server mints (or guesses from the transport).
        ("onboard", {"force_new": False}),
        ("start_session", {"force_new": False, "name": "Bound_Owner"}),
        ("identity", {"name": "Bound_Owner"}),
        ("identity", {"name": "anything", "resume": True}),
    ],
)
def test_a_bound_slot_refuses_identity_calls_the_server_would_mint_for(
    tmp_path: Path, fake_server: str, transport: str, name: str, arguments: dict,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar)

    status, message, _payload = _call(sidecar, transport, name, arguments)

    assert status == 409
    assert "already bound" in message
    assert FakeGovernanceHandler.calls == []
    assert sidecar.read_session("slot-a") == before


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "name, arguments",
    [
        ("bind_session", {"client_session_id": BOUND_CSID, "resume": True}),
        ("identity", {"client_session_id": BOUND_CSID}),
        ("identity", {"agent_uuid": BOUND_UUID, "resume": True}),
        ("onboard", {"force_new": False, "client_session_id": BOUND_CSID}),
    ],
)
def test_an_inherited_binding_is_no_proof_for_identity_calls(
    tmp_path: Path, fake_server: str, transport: str, name: str, arguments: dict,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar, prior_process=True)

    status, message, _payload = _call(sidecar, transport, name, arguments)

    assert status == 409
    assert "prior process" in message
    assert FakeGovernanceHandler.calls == []
    assert sidecar.read_session("slot-a") == before


def test_an_invalid_protected_binding_refuses_identity_calls(
    tmp_path: Path, fake_server: str,
) -> None:
    protected = Path.home() / ".unitares" / "session-slot-a.json"
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text(json.dumps({
        "schema_version": 2, "slot": "wrong-slot",
        "uuid": BOUND_UUID, "client_session_id": BOUND_CSID,
    }), encoding="utf-8")
    protected.chmod(0o600)
    sidecar = _sidecar(tmp_path, fake_server)

    status, payload = sidecar.tool_call({"name": "onboard", "arguments": {}}, headers={})

    assert status == 409
    assert "clear it explicitly" in payload["error"]
    assert FakeGovernanceHandler.calls == []
    assert json.loads(protected.read_text(encoding="utf-8"))["slot"] == "wrong-slot"


def _reply(monkeypatch: pytest.MonkeyPatch, result: dict) -> None:
    monkeypatch.setattr(
        identity_sidecar, "_post_json", lambda *_a, **_k: ({"result": dict(result)}, 1, None)
    )


@pytest.mark.parametrize("bound", [True, False])
def test_a_bind_reply_without_a_session_id_never_binds_an_empty_slot(
    tmp_path: Path, fake_server: str, monkeypatch: pytest.MonkeyPatch, bound: bool,
) -> None:
    # bind_session replies carry the agent, never the client_session_id.
    sidecar = _sidecar(tmp_path, fake_server)
    real_post = identity_sidecar._post_json
    _reply(monkeypatch, {
        "success": True, "bound": bound, "agent_uuid": FOREIGN_UUID, "agent_id": "Other",
    })

    status, payload = sidecar.tool_call(
        {"name": "bind_session", "arguments": {"client_session_id": "agent-other", "resume": True}},
        headers={},
    )

    assert status == 200
    assert payload["sidecar"]["identity_cache_update"].startswith("refused_")
    assert sidecar.read_session("slot-a") == {}
    # The slot stays usable: lazy onboarding still binds it.
    monkeypatch.setattr(identity_sidecar, "_post_json", real_post)
    assert sidecar.ensure_session("slot-a")["status"] == "ok"


def test_a_same_identity_bind_reply_keeps_the_session_id(
    tmp_path: Path, fake_server: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _bind_slot_a(sidecar)
    _reply(monkeypatch, {
        "success": True, "bound": True, "agent_uuid": BOUND_UUID,
        "agent_id": "Bound_Owner", "display_name": "Owner",
    })

    status, payload = sidecar.tool_call(
        {"name": "bind_session", "arguments": {"client_session_id": BOUND_CSID, "resume": True}},
        headers={},
    )

    assert status == 200
    assert payload["sidecar"]["identity_cache_update"] == "updated"
    session = sidecar.read_session("slot-a")
    assert session["uuid"] == BOUND_UUID
    assert session["client_session_id"] == BOUND_CSID


def test_a_refused_bind_of_the_bound_identity_is_not_persisted(
    tmp_path: Path, fake_server: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar)
    _reply(monkeypatch, {
        "success": True, "bound": False, "agent_uuid": BOUND_UUID, "display_name": "Changed",
    })

    status, payload = sidecar.tool_call(
        {"name": "bind_session", "arguments": {"client_session_id": BOUND_CSID, "resume": True}},
        headers={},
    )

    assert status == 200
    assert payload["sidecar"]["identity_cache_update"] == "refused_failed_response"
    assert sidecar.read_session("slot-a") == before


@pytest.mark.parametrize("alias", ["Owner Display", "public-owner-id", "Bound_Owner", BOUND_UUID])
def test_a_strict_bind_may_pin_the_owner_by_any_server_alias(
    tmp_path: Path, fake_server: str, alias: str,
) -> None:
    # The server checks the pin against every alias of the agent the matching
    # client_session_id resolves to, so the sidecar leaves that check to it.
    sidecar = _sidecar(tmp_path, fake_server)
    _bind_slot_a(sidecar)

    _name, arguments, _lifecycle = sidecar.prepare_tool_arguments(
        name="bind_session",
        arguments={"client_session_id": BOUND_CSID, "agent_id": alias, "strict": True},
        slot="slot-a",
    )

    assert arguments["agent_id"] == alias


# --- Check what is sent, send what was checked -----------------------------
#
# The server strips an "mcp_unitares_" tool-name prefix and merges object or
# JSON-string kwargs over the outer arguments. The sidecar canonicalizes both
# before any check and forwards the canonical call.


def _forwarded(transport: str) -> tuple[str, dict]:
    (sent,) = FakeGovernanceHandler.calls
    if transport == "mcp":
        sent = sent["params"]
    return sent["name"], sent["arguments"]


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "name, arguments",
    [
        ("mcp_unitares_onboard", {}),
        ("mcp_unitares_start_session", {"force_new": False}),
        ("MCP_UNITARES_ONBOARD", {}),
        ("mcp_unitares_identity", {"agent_uuid": FOREIGN_UUID}),
        ("mcp_unitares_identity", {"client_session_id": BOUND_CSID, "resume": False}),
        ("mcp_unitares_use_tool", {"tool_name": "onboard"}),
        ("use_tool", {"tool_name": "mcp_unitares_onboard"}),
    ],
)
def test_the_server_name_prefix_cannot_skip_the_owner_rule(
    tmp_path: Path, fake_server: str, transport: str, name: str, arguments: dict,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar)

    status, _message, _payload = _call(sidecar, transport, name, arguments)

    assert status == 409
    assert FakeGovernanceHandler.calls == []
    assert sidecar.read_session("slot-a") == before


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "name",
    ["mcp_unitares_mcp_unitares_onboard", "mcp_other_onboard", "mcp__unitares__mcp_x", "mcp_unitares_", " ", 7],
)
def test_a_tool_name_the_sidecar_cannot_canonicalize_is_refused(
    tmp_path: Path, fake_server: str, transport: str, name: object,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _bind_slot_a(sidecar)

    if transport == "rest":
        status, _payload = sidecar.tool_call({"name": name, "arguments": {}}, headers={})
    else:
        status, _payload = _mcp_call(sidecar, name, {})  # type: ignore[arg-type]

    assert status in {400, 409}
    assert FakeGovernanceHandler.calls == []


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("name", ["mcp_unitares_knowledge", "mcp__unitares__Knowledge", " KNOWLEDGE "])
def test_the_canonical_tool_name_is_what_is_forwarded(
    tmp_path: Path, fake_server: str, transport: str, name: str,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _bind_slot_a(sidecar)

    status, _message, _payload = _call(sidecar, transport, name, {"action": "search"})

    assert status == 200
    sent_name, sent_arguments = _forwarded(transport)
    assert sent_name == "knowledge"
    assert sent_arguments["client_session_id"] == BOUND_CSID


def _wrap(arguments: dict, as_string: bool) -> dict:
    return {"kwargs": json.dumps(arguments) if as_string else arguments}


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("as_string", [False, True])
@pytest.mark.parametrize(
    "name, outer, wrapped",
    [
        # A wrapped flag replaces the checked outer one.
        ("onboard", {"force_new": False, "client_session_id": BOUND_CSID}, {"force_new": True}),
        ("onboard", {"force_new": False, "client_session_id": BOUND_CSID}, {"resume": False}),
        ("identity", {"client_session_id": BOUND_CSID}, {"parent_agent_id": BOUND_UUID}),
        # Wrapped proof replaces the checked outer proof.
        ("identity", {"client_session_id": BOUND_CSID}, {"client_session_id": "agent-foreign-222"}),
        ("knowledge", {"action": "search"}, {"client_session_id": "agent-foreign-222"}),
        # A wrapped gateway target or nested proof.
        ("use_tool", {"tool_name": "knowledge"}, {"tool_name": "onboard"}),
        ("use_tool", {"tool_name": "knowledge"}, {"arguments": {"client_session_id": "agent-foreign-222"}}),
        ("use_tool", {"tool_name": "knowledge", "arguments": {"kwargs": {"client_session_id": "agent-foreign-222"}}}, {}),
    ],
)
def test_wrapped_kwargs_cannot_replace_what_was_checked(
    tmp_path: Path, fake_server: str, transport: str, as_string: bool,
    name: str, outer: dict, wrapped: dict,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar)

    status, _message, _payload = _call(sidecar, transport, name, {**outer, **_wrap(wrapped, as_string)})

    assert status == 409
    assert FakeGovernanceHandler.calls == []
    assert sidecar.read_session("slot-a") == before


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_a_wrapped_subagent_mint_is_refused_in_an_empty_slot(
    tmp_path: Path, fake_server: str, transport: str,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)

    status, message, _payload = _call(
        sidecar, transport, "onboard", {"kwargs": {"kwargs": '{"spawn_reason": "subagent"}'}},
    )

    assert status == 409
    assert "subagent" in message
    assert FakeGovernanceHandler.calls == []


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_flattened_kwargs_are_what_is_forwarded(
    tmp_path: Path, fake_server: str, transport: str,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _bind_slot_a(sidecar)
    arguments = {
        "tool_name": "knowledge",
        "action": "outer",
        "kwargs": json.dumps({"action": "middle", "kwargs": {"limit": 3}}),
        "arguments": {"query": "x", "kwargs": '{"action": "search"}'},
    }

    status, _message, _payload = _call(sidecar, transport, "use_tool", arguments)

    assert status == 200
    sent_name, sent_arguments = _forwarded(transport)
    assert sent_name == "use_tool"
    assert sent_arguments == {
        "tool_name": "knowledge",
        "action": "middle",
        "limit": 3,
        "arguments": {"query": "x", "action": "search"},
        "client_session_id": BOUND_CSID,
    }


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("kwargs", ["not json", "[1]", '"x"', None, ["x"], 7])
def test_kwargs_the_sidecar_cannot_flatten_are_refused(
    tmp_path: Path, fake_server: str, transport: str, kwargs: object,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _bind_slot_a(sidecar)

    status, message, _payload = _call(sidecar, transport, "knowledge", {"action": "search", "kwargs": kwargs})

    assert status == 409
    assert "kwargs" in message
    assert FakeGovernanceHandler.calls == []


def test_kwargs_nested_past_the_cap_are_refused(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _bind_slot_a(sidecar)
    arguments: dict = {"action": "search"}
    for _ in range(identity_sidecar.MAX_KWARGS_DEPTH + 1):
        arguments = {"kwargs": arguments}

    status, payload = sidecar.tool_call({"name": "knowledge", "arguments": arguments}, headers={})

    assert status == 409
    assert "nested more than" in payload["error"]
    assert FakeGovernanceHandler.calls == []


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "name, arguments",
    [
        # The server skips reuse and mints when resume is not true.
        ("onboard", {"force_new": False, "client_session_id": BOUND_CSID, "resume": False}),
        ("start_session", {"force_new": False, "client_session_id": BOUND_CSID, "resume": "false"}),
        ("identity", {"client_session_id": BOUND_CSID, "resume": False}),
        ("identity", {"client_session_id": BOUND_CSID, "resume": None}),
        ("identity", {"agent_uuid": BOUND_UUID, "resume": 0}),
        ("identity", {"client_session_id": BOUND_CSID, "force_new": None}),
        ("identity", {"client_session_id": BOUND_CSID, "spawn_reason": "new_session"}),
        # Matching proof that does not pin the session: the server resolves
        # the session from the transport and may mint for it.
        ("onboard", {"force_new": False, "agent_uuid": BOUND_UUID}),
        ("onboard", {"force_new": False, "agent_id": "Bound_Owner"}),
        ("identity", {"agent_id": "Bound_Owner"}),
    ],
)
def test_matching_proof_that_could_still_mint_is_refused(
    tmp_path: Path, fake_server: str, transport: str, name: str, arguments: dict,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    before = _bind_slot_a(sidecar)

    status, message, _payload = _call(sidecar, transport, name, arguments)

    assert status == 409
    assert "already bound" in message
    assert FakeGovernanceHandler.calls == []
    assert sidecar.read_session("slot-a") == before


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "name, arguments",
    [
        ("onboard", {"force_new": False, "client_session_id": BOUND_CSID}),
        ("onboard", {"force_new": False, "client_session_id": BOUND_CSID, "resume": True}),
        ("start_session", {"force_new": False, "client_session_id": BOUND_CSID, "agent_id": "Bound_Owner"}),
        ("identity", {"client_session_id": BOUND_CSID, "name": "Renamed"}),
        ("identity", {"agent_uuid": BOUND_UUID}),
        ("identity", {"agent_uuid": BOUND_UUID, "resume": True, "force_new": False}),
        ("bind_session", {"client_session_id": BOUND_CSID, "resume": True}),
    ],
)
def test_a_resume_of_the_bound_identity_is_forwarded_as_sent(
    tmp_path: Path, fake_server: str, transport: str, name: str, arguments: dict,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _bind_slot_a(sidecar)

    status, _message, _payload = _call(sidecar, transport, name, {"kwargs": dict(arguments)})

    assert status == 200
    sent_name, sent_arguments = _forwarded(transport)
    assert sent_name == name
    assert "kwargs" not in sent_arguments
    for key, value in arguments.items():
        assert sent_arguments[key] == value


def test_a_batch_with_a_repeated_tools_call_id_is_refused(tmp_path: Path, fake_server: str) -> None:
    # Replies are matched to calls by id; a repeated id would let one call's
    # reply be read as an identity reply.
    sidecar = _sidecar(tmp_path, fake_server)
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}

    status, payload = sidecar.mcp_proxy(
        [
            {**call, "params": {"name": "knowledge", "arguments": {"action": "search"}}},
            {**call, "params": {"name": "onboard", "arguments": {}}},
        ],
        headers={},
        path="/mcp/",
    )

    assert status == 409
    assert "repeated" in payload["error"]["message"]
    assert FakeGovernanceHandler.calls == []


@pytest.mark.parametrize("params", [None, ["onboard"], {"name": "onboard", "arguments": "{}"}])
def test_an_unreadable_mcp_tools_call_is_refused_not_passed_through(
    tmp_path: Path, fake_server: str, params: object,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _bind_slot_a(sidecar)

    status, _payload = sidecar.mcp_proxy(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
        headers={},
        path="/mcp/",
    )

    assert status == 409
    assert FakeGovernanceHandler.calls == []


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize(
    "name, arguments",
    [
        ("identity", {"client_session_id": f" {BOUND_CSID} "}),
        ("onboard", {"force_new": False, "client_session_id": f"{BOUND_CSID}\n"}),
        ("knowledge", {"action": "search", "client_session_id": f" {BOUND_CSID}"}),
        ("identity", {"client_session_id": BOUND_CSID, "agent_id": " Bound_Owner "}),
    ],
)
def test_proof_is_compared_exactly_as_it_is_forwarded(
    tmp_path: Path, fake_server: str, transport: str, name: str, arguments: dict,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _bind_slot_a(sidecar)

    status, message, _payload = _call(sidecar, transport, name, arguments)

    assert status == 409
    assert "configured slot binding" in message
    assert FakeGovernanceHandler.calls == []
