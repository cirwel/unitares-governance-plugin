from __future__ import annotations

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from scripts import file_lease_hook


class _ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


class LeaseHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    acquire_response: dict = {}

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        self.__class__.calls.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization", ""),
                "body": body,
            }
        )
        if self.path == "/v1/lease/acquire":
            payload = self.__class__.acquire_response
            status = 409 if payload.get("error") == "held_by_other" else 200
        elif self.path in {"/v1/lease/heartbeat", "/v1/lease/release"}:
            payload = {"ok": True, "protocol_version": "v1.0"}
            status = 200
        else:
            payload = {"ok": False, "error": "not_found"}
            status = 404
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):  # noqa: A002
        return


@pytest.fixture
def lease_server():
    LeaseHandler.calls = []
    LeaseHandler.acquire_response = {
        "ok": True,
        "lease": {
            "lease_id": "11111111-1111-4111-8111-111111111111",
            "surface_id": "file:///tmp/example.py",
            "expires_at": "2026-05-28T00:00:00Z",
        },
        "idempotent": False,
        "protocol_version": "v1.0",
    }
    srv = _ReusableTCPServer(("127.0.0.1", 0), LeaseHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        thread.join(timeout=2)


def _payload(slot: str = "slot-1", path: str = "a.py") -> str:
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": slot,
            "tool_name": "Edit",
            "tool_input": {"file_path": path},
        }
    )


def _lease_env(monkeypatch, lease_server):
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", f"http://127.0.0.1:{lease_server.server_address[1]}")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")


def test_pre_edit_acquires_file_lease_and_records_state(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)

    rc = file_lease_hook.main(["pre-edit", "--workspace", str(tmp_path)], stdin_text=_payload())

    assert rc == 0
    acquire = LeaseHandler.calls[0]
    assert acquire["path"] == "/v1/lease/acquire"
    assert acquire["authorization"] == "Bearer lease-token"
    assert acquire["body"]["surface_id"] == f"file://{tmp_path / 'a.py'}"
    assert acquire["body"]["holder_kind"] == "remote_heartbeat"

    state = json.loads((tmp_path / ".unitares" / "file-leases-slot-1.json").read_text())
    assert state["version"] == 1
    assert state["leases"]["file:///tmp/example.py"]["lease_id"] == "11111111-1111-4111-8111-111111111111"


def test_pre_edit_blocks_on_held_by_other(tmp_path, monkeypatch, lease_server, capsys):
    _lease_env(monkeypatch, lease_server)
    LeaseHandler.acquire_response = {
        "ok": False,
        "error": "held_by_other",
        "surface_id": f"file://{tmp_path / 'a.py'}",
        "blocking_lease_id": "22222222-2222-4222-8222-222222222222",
        "held_by_uuid": "33333333-3333-4333-8333-333333333333",
        "expires_at": "2026-05-28T00:01:00Z",
        "retry_after_hint_ms": 1000,
        "protocol_version": "v1.0",
    }

    rc = file_lease_hook.main(["pre-edit", "--workspace", str(tmp_path)], stdin_text=_payload())

    assert rc == 2
    err = capsys.readouterr().err
    assert "BLOCKED: file lease held by another agent" in err
    # The block message must tell the operator the lease self-heals, so they
    # don't reflexively force-release a lease that would clear on its own.
    assert "self-heals" in err
    assert not (tmp_path / ".unitares" / "file-leases-slot-1.json").exists()


def test_pre_edit_missing_token_fails_open_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("UNITARES_LEASE_PLANE_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("GOVERNANCE_TOKEN", raising=False)
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(tmp_path / "missing.env"))

    rc = file_lease_hook.main(["pre-edit", "--workspace", str(tmp_path)], stdin_text=_payload())

    assert rc == 0
    assert not (tmp_path / ".unitares").exists()


def test_heartbeat_session_renews_existing_leases(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    lease_dir = tmp_path / ".unitares"
    lease_dir.mkdir()
    (lease_dir / "file-leases-slot-1.json").write_text(
        json.dumps(
            {
                "version": 1,
                "slot": "slot-1",
                "workspace": str(tmp_path),
                "holder_uuid": "33333333-3333-4333-8333-333333333333",
                "leases": {
                    "file:///tmp/example.py": {
                        "lease_id": "11111111-1111-4111-8111-111111111111",
                        "path": "a.py",
                        "surface_id": "file:///tmp/example.py",
                    }
                },
            }
        )
    )

    rc = file_lease_hook.main(["heartbeat-session", "--workspace", str(tmp_path)], stdin_text=_payload())

    assert rc == 0
    assert [call["path"] for call in LeaseHandler.calls] == ["/v1/lease/heartbeat"]


def test_release_session_releases_and_removes_state(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    lease_dir = tmp_path / ".unitares"
    lease_dir.mkdir()
    state_path = lease_dir / "file-leases-slot-1.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "slot": "slot-1",
                "workspace": str(tmp_path),
                "holder_uuid": "33333333-3333-4333-8333-333333333333",
                "leases": {
                    "file:///tmp/example.py": {
                        "lease_id": "11111111-1111-4111-8111-111111111111",
                        "path": "a.py",
                        "surface_id": "file:///tmp/example.py",
                    }
                },
            }
        )
    )

    rc = file_lease_hook.main(["release-session", "--workspace", str(tmp_path)], stdin_text=_payload())

    assert rc == 0
    assert [call["path"] for call in LeaseHandler.calls] == ["/v1/lease/release"]
    assert not state_path.exists()


def test_release_edit_releases_only_edited_file_and_keeps_others(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    lease_dir = tmp_path / ".unitares"
    lease_dir.mkdir()
    state_path = lease_dir / "file-leases-slot-1.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "slot": "slot-1",
                "workspace": str(tmp_path),
                "holder_uuid": "33333333-3333-4333-8333-333333333333",
                "leases": {
                    "file:///tmp/a.py": {
                        "lease_id": "11111111-1111-4111-8111-111111111111",
                        "path": "a.py",
                        "surface_id": "file:///tmp/a.py",
                    },
                    "file:///tmp/b.py": {
                        "lease_id": "22222222-2222-4222-8222-222222222222",
                        "path": "b.py",
                        "surface_id": "file:///tmp/b.py",
                    },
                },
            }
        )
    )

    rc = file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path)], stdin_text=_payload(path="a.py")
    )

    assert rc == 0
    # Only the edited file's lease is released.
    assert [call["path"] for call in LeaseHandler.calls] == ["/v1/lease/release"]
    assert LeaseHandler.calls[0]["body"]["lease_id"] == "11111111-1111-4111-8111-111111111111"
    # State keeps the still-held b.py lease, drops a.py.
    state = json.loads(state_path.read_text())
    assert "file:///tmp/a.py" not in state["leases"]
    assert "file:///tmp/b.py" in state["leases"]


def test_release_edit_noop_when_file_not_leased(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    lease_dir = tmp_path / ".unitares"
    lease_dir.mkdir()
    (lease_dir / "file-leases-slot-1.json").write_text(
        json.dumps(
            {
                "version": 1,
                "slot": "slot-1",
                "workspace": str(tmp_path),
                "holder_uuid": "33333333-3333-4333-8333-333333333333",
                "leases": {
                    "file:///tmp/b.py": {
                        "lease_id": "22222222-2222-4222-8222-222222222222",
                        "path": "b.py",
                        "surface_id": "file:///tmp/b.py",
                    }
                },
            }
        )
    )

    rc = file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path)], stdin_text=_payload(path="a.py")
    )

    assert rc == 0
    assert LeaseHandler.calls == []  # nothing released for an unleased file


def test_hooks_json_wires_pretooluse_edit_guard():
    config = json.loads((Path(__file__).parent.parent / "hooks" / "hooks.json").read_text())

    pre_hooks = config["hooks"]["PreToolUse"]
    assert pre_hooks[0]["matcher"] == "Edit|Write|MultiEdit"
    assert "pre-edit" in pre_hooks[0]["hooks"][0]["command"]
