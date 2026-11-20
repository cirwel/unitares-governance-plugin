"""Tests for the local UNITARES identity sidecar."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import socket
import threading
import time
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
        ("use_tool", "mcp__unitares__use_tool"),
    ],
)
def test_gateway_name_variants_cannot_skip_the_nested_proof_check(
    tmp_path: Path, fake_server: str, outer_name: str, inner_tool: str,
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _slot_a(sidecar)
    inner = {"action": "search", "client_session_id": "agent-foreign-111"}
    if inner_tool != "knowledge":
        inner = {"tool_name": "knowledge", "arguments": inner}

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


def test_gateway_nesting_beyond_the_cap_is_refused(tmp_path: Path, fake_server: str) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    _slot_a(sidecar)
    inner: dict = {"action": "search"}
    for _ in range(identity_sidecar.MAX_GATEWAY_DEPTH + 1):
        inner = {"tool_name": "use_tool", "arguments": inner}

    status, payload = sidecar.tool_call(
        {"name": "use_tool", "arguments": inner},
        headers={},
    )

    assert status == 409
    assert "nested more than" in payload["error"]
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

    assert status == 409
    assert "arguments.arguments.client_session_id" in payload["error"]
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


def _trickle_until_closed(client: socket.socket, *, budget: float) -> bool:
    """Send one byte at a time; report whether the server closed the socket."""
    client.settimeout(0.2)
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        try:
            client.sendall(b"X")
            if client.recv(4096) == b"":
                return True
        except socket.timeout:
            continue
        except OSError:
            return True
    return False


@pytest.mark.parametrize("stalled_part", ["headers", "body"])
def test_slow_request_cannot_hold_a_worker_past_read_deadline(
    tmp_path: Path, fake_server: str, monkeypatch: pytest.MonkeyPatch, stalled_part: str
) -> None:
    monkeypatch.setattr(identity_sidecar, "REQUEST_READ_DEADLINE", 0.5)
    sidecar = _sidecar(tmp_path, fake_server)
    server = BoundedThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(sidecar), max_workers=1
    )
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    if stalled_part == "headers":
        opening = "GET /health HTTP/1.1\r\nX-Slow: "
    else:
        opening = (
            "POST /v1/tools/call HTTP/1.1\r\n"
            f"Authorization: Bearer {sidecar.capability_token}\r\n"
            "Content-Length: 4096\r\n\r\n"
        )
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(opening.encode("ascii"))
            assert _trickle_until_closed(client, budget=4.0)
        # The worker frees its slot just after the socket closes.
        released_by = time.monotonic() + 2.0
        while True:
            try:
                status, payload = _http_get_json(
                    f"{base_url}/health",
                    headers={"Authorization": f"Bearer {sidecar.capability_token}"},
                )
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 503 or time.monotonic() > released_by:
                    raise
                time.sleep(0.05)
    finally:
        _stop_server(server, serving)
    assert status == 200
    assert payload["success"] is True
    assert FakeGovernanceHandler.calls == []


def test_read_deadline_on_partial_request_line_is_not_reported_as_server_error(
    tmp_path: Path, fake_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(identity_sidecar, "REQUEST_READ_DEADLINE", 0.5)
    sidecar = _sidecar(tmp_path, fake_server)
    server = BoundedThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(sidecar), max_workers=1
    )
    handler_errors: list[BaseException | None] = []
    handled = threading.Event()

    def record_error(request: object, client_address: object) -> None:
        handler_errors.append(sys.exc_info()[1])

    original_shutdown = server.shutdown_request

    def note_shutdown(request: object) -> None:
        original_shutdown(request)
        handled.set()

    server.handle_error = record_error  # type: ignore[method-assign]
    server.shutdown_request = note_shutdown  # type: ignore[method-assign]
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    port = server.server_address[1]
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(b"G")
            assert _trickle_until_closed(client, budget=4.0)
        assert handled.wait(2.0)
    finally:
        _stop_server(server, serving)
    assert handler_errors == []


def test_post_headers_and_body_share_one_read_deadline(
    tmp_path: Path, fake_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(identity_sidecar, "REQUEST_READ_DEADLINE", 1.0)
    sidecar = _sidecar(tmp_path, fake_server)
    server = BoundedThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(sidecar), max_workers=1
    )
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    port = server.server_address[1]
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            started = time.monotonic()
            client.sendall(
                (
                    "POST /v1/tools/call HTTP/1.1\r\n"
                    f"Authorization: Bearer {sidecar.capability_token}\r\n"
                    "Content-Length: 4096\r\nX-Slow: "
                ).encode("ascii")
            )
            # Spend most of the deadline on the headers, then trickle the body.
            while time.monotonic() - started < 0.8:
                client.sendall(b"a")
                time.sleep(0.05)
            client.sendall(b"\r\n\r\n")
            assert _trickle_until_closed(client, budget=4.0)
            closed_after = time.monotonic() - started
    finally:
        _stop_server(server, serving)
    assert closed_after < 1.4
    assert FakeGovernanceHandler.calls == []


def _proof_exchange(client: socket.socket, challenge: str, *, keep_alive: bool) -> dict:
    """Send one startup-proof request on ``client`` and read its whole response."""
    connection = "Connection: keep-alive\r\n" if keep_alive else ""
    client.sendall(
        f"GET /startup-proof?challenge={challenge} HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\n{connection}\r\n".encode("ascii")
    )
    reader = client.makefile("rb")
    try:
        status_line = reader.readline()
        length = None
        while (line := reader.readline()) not in (b"\r\n", b"\n", b""):
            name, _, value = line.decode("latin-1").partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())
        assert b" 200 " in status_line
        assert length is not None
        return json.loads(reader.read(length))
    finally:
        reader.close()


def _server_closed(client: socket.socket, *, within: float = 2.0) -> bool:
    """True when the server closes the connection (EOF) while the client waits."""
    client.settimeout(within)
    try:
        return client.recv(1) == b""
    except socket.timeout:
        return False


def test_startup_proof_keeps_its_connection_for_exactly_one_follow_up(
    tmp_path: Path, fake_server: str
) -> None:
    sidecar = _sidecar(tmp_path, fake_server)
    sidecar.startup_secret = "startup-secret"
    server = BoundedThreadingHTTPServer(("127.0.0.1", 0), make_handler(sidecar))
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    port = server.server_address[1]

    def expected(challenge: str) -> str:
        return hmac.new(b"startup-secret", challenge.encode(), hashlib.sha256).hexdigest()

    try:
        # Keep-alive is opt-in: without it the server closes after the proof.
        with socket.create_connection(("127.0.0.1", port), timeout=5) as plain:
            assert _proof_exchange(plain, "c1", keep_alive=False)["proof"] == expected("c1")
            assert _server_closed(plain)

        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            assert _proof_exchange(client, "c2", keep_alive=True)["proof"] == expected("c2")
            assert not _server_closed(client, within=0.3), "the proven connection was not kept"
            # The one follow-up is answered, and a second proof on the same
            # connection does not extend it again: the server closes it.
            assert _proof_exchange(client, "c3", keep_alive=True)["proof"] == expected("c3")
            assert _server_closed(client)
    finally:
        _stop_server(server, serving)


def test_request_handling_after_the_last_body_byte_is_outside_the_read_deadline(
    tmp_path: Path, fake_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body that arrived in time is handled, however long decoding takes."""
    monkeypatch.setattr(identity_sidecar, "REQUEST_READ_DEADLINE", 0.5)
    real_loads = json.loads
    marker = "slow-to-decode-marker"
    slowed: list[bool] = []

    def slow_loads(text: object, *args: object, **kwargs: object) -> object:
        # Only the sidecar's decode of the request body is slowed.
        if isinstance(text, str) and marker in text and not slowed:
            slowed.append(True)
            time.sleep(1.0)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(identity_sidecar.json, "loads", slow_loads)
    sidecar = _sidecar(tmp_path, fake_server)
    server = BoundedThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(sidecar), max_workers=1
    )
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        status, payload = _http_post_json(
            f"http://127.0.0.1:{server.server_address[1]}/v1/tools/call",
            {"name": "knowledge", "arguments": {"action": "search", "query": marker}},
            headers={"Authorization": f"Bearer {sidecar.capability_token}"},
        )
    finally:
        _stop_server(server, serving)
    assert status == 200
    assert payload["result"]["success"] is True
    assert [call["name"] for call in FakeGovernanceHandler.calls] == ["onboard", "knowledge"]
