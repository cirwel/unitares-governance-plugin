from __future__ import annotations

import json
import os
import shutil
import socketserver
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from scripts import file_lease_hook


@pytest.fixture(autouse=True)
def _allow_explicit_test_loopback_http(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("UNITARES_ALLOW_INSECURE_LOOPBACK_LEASE_HTTP", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


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

    health_token: str = "lease-token"
    health_payload: dict = {"ok": True, "status": "ok", "protocol_version": "v1.0"}

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        self.__class__.calls.append(
            {"path": self.path, "authorization": self.headers.get("Authorization", "")}
        )
        if self.path != "/v1/health":
            status, payload = 404, {"ok": False, "error": "not_found"}
        elif self.headers.get("Authorization") == f"Bearer {self.__class__.health_token}":
            status, payload = 200, self.__class__.health_payload
        else:
            status, payload = 401, {"ok": False, "error": "unauthorized"}
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
    LeaseHandler.health_token = "lease-token"
    LeaseHandler.health_payload = {"ok": True, "status": "ok", "protocol_version": "v1.0"}
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


def _payload(
    slot: str = "slot-1",
    path: str = "a.py",
    tool_use_id: str = "",
) -> str:
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": slot,
        "tool_name": "Edit",
        "tool_input": {"file_path": path},
    }
    if tool_use_id:
        payload["tool_use_id"] = tool_use_id
    return json.dumps(payload)


def _codex_payload(
    *,
    slot: str = "codex-slot",
    tool_use_id: str = "call_1",
    paths: tuple[str, ...] = ("a.py",),
) -> str:
    directives = "\n".join(f"*** Update File: {path}" for path in paths)
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": slot,
            "tool_name": "apply_patch",
            "tool_use_id": tool_use_id,
            "tool_input": {"command": f"*** Begin Patch\n{directives}\n*** End Patch"},
        }
    )


def _lease_env(monkeypatch, lease_server):
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", f"http://127.0.0.1:{lease_server.server_address[1]}")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")


def _seed_state(
    workspace: Path,
    slot: str,
    tool_use_id: str,
    leases: dict[str, dict],
    *,
    holder_uuid: str = "33333333-3333-4333-8333-333333333333",
) -> Path:
    bound_leases = {}
    for surface, row in leases.items():
        bound_leases[surface] = {
            **row,
            "surface_id": surface,
            "holder_uuid": holder_uuid,
            "tool_use_id": tool_use_id,
        }
    state = {
        "version": file_lease_hook.STATE_VERSION,
        "slot": slot,
        "workspace": str(workspace.resolve()),
        "tool_use_id": tool_use_id,
        "holder_uuid": holder_uuid,
        "leases": bound_leases,
    }
    file_lease_hook._save_state(
        workspace,
        slot,
        state,
        tool_use_id,
        token="lease-token",
    )
    return file_lease_hook._state_path(workspace, slot, tool_use_id)


def test_configured_loopback_bearer_is_withheld_without_explicit_opt_in(
    tmp_path,
    monkeypatch,
    lease_server,
):
    _lease_env(monkeypatch, lease_server)
    monkeypatch.delenv("UNITARES_ALLOW_INSECURE_LOOPBACK_LEASE_HTTP", raising=False)

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(tool_use_id="toolu_untrusted_loopback"),
    )

    assert rc == 0
    assert LeaseHandler.calls == []


def test_required_mode_blocks_when_worktree_namespace_probe_fails(
    tmp_path,
    monkeypatch,
    lease_server,
    capsys,
):
    _lease_env(monkeypatch, lease_server)
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    monkeypatch.setattr(file_lease_hook, "_worktree_roots", lambda *_args, **_kwargs: None)

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(tool_use_id="toolu_no_namespace"),
    )

    assert rc == 2
    assert "could not verify the repository/worktree namespace" in capsys.readouterr().err
    assert LeaseHandler.calls == []


def test_pre_edit_acquires_file_lease_and_records_state(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    tool_use_id = "toolu_acquire"

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(tool_use_id=tool_use_id),
    )

    assert rc == 0
    acquire = LeaseHandler.calls[0]
    assert acquire["path"] == "/v1/lease/acquire"
    assert acquire["authorization"] == "Bearer lease-token"
    assert acquire["body"]["surface_id"] == f"file://{tmp_path / 'a.py'}"
    assert acquire["body"]["holder_kind"] == "remote_heartbeat"

    state = json.loads(
        file_lease_hook._state_path(tmp_path, "slot-1", tool_use_id).read_text()
    )
    assert state["version"] == file_lease_hook.STATE_VERSION
    assert len(state["integrity"]) == 64
    state_path = file_lease_hook._state_path(tmp_path, "slot-1", tool_use_id)
    assert state_path != file_lease_hook._legacy_state_path(
        tmp_path, "slot-1", tool_use_id
    )
    assert "file-lease-state" in state_path.parts
    if file_lease_hook.os.name != "nt":
        assert state_path.stat().st_mode & 0o077 == 0
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

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(tool_use_id="toolu_conflict"),
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "BLOCKED: file lease held by another agent" in err
    # The block message must tell the operator the lease self-heals, so they
    # don't reflexively force-release a lease that would clear on its own.
    assert "self-heals" in err
    assert not list((tmp_path / ".unitares").glob("file-leases-*.json"))


def test_pre_edit_missing_token_fails_open_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("UNITARES_LEASE_PLANE_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("GOVERNANCE_TOKEN", raising=False)
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(tmp_path / "missing.env"))
    # A remote lease plane: the loopback Compose default does not apply.
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", "http://lease-plane.invalid:8788")

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(tool_use_id="toolu_missing_token"),
    )

    assert rc == 0
    assert not (tmp_path / ".unitares").exists()


def test_disabled_leases_remain_disabled_when_not_required(tmp_path, monkeypatch):
    monkeypatch.setenv("UNITARES_FILE_LEASES_ENABLED", "0")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "0")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")

    def unexpected_http(*args, **kwargs):
        pytest.fail("disabled lease guard attempted an HTTP request")

    monkeypatch.setattr(file_lease_hook, "_http_json", unexpected_http)

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(tool_use_id="toolu_disabled"),
    )

    assert rc == 0
    assert not (tmp_path / ".unitares").exists()


def test_required_leases_override_disabled_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("UNITARES_FILE_LEASES_ENABLED", "0")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "YES")
    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("UNITARES_LEASE_PLANE_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("GOVERNANCE_TOKEN", raising=False)
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(tmp_path / "missing.env"))

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(tool_use_id="toolu_required_disabled"),
    )

    assert rc == 2
    assert "missing LEASE_PLANE_BEARER_TOKEN" in capsys.readouterr().err


def test_heartbeat_session_is_compatibility_noop(tmp_path, monkeypatch, lease_server):
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
    assert LeaseHandler.calls == []
    assert (lease_dir / "file-leases-slot-1.json").exists()


def test_release_session_releases_and_removes_state(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    state_path = _seed_state(
        tmp_path,
        "slot-1",
        "",
        {
            "file:///tmp/example.py": {
                "lease_id": "11111111-1111-4111-8111-111111111111",
                "path": "a.py",
            }
        },
    )

    rc = file_lease_hook.main(["release-session", "--workspace", str(tmp_path)], stdin_text=_payload())

    assert rc == 0
    assert [call["path"] for call in LeaseHandler.calls] == ["/v1/lease/release"]
    assert not state_path.exists()


def test_release_edit_releases_every_lease_the_event_holds(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    tool_use_id = "toolu_release"
    state_path = _seed_state(
        tmp_path,
        "slot-1",
        tool_use_id,
        {
            "file:///tmp/a.py": {
                "lease_id": "11111111-1111-4111-8111-111111111111",
                "path": "a.py",
            },
            "file:///tmp/b.py": {
                "lease_id": "22222222-2222-4222-8222-222222222222",
                "path": "b.py",
            },
        },
    )

    rc = file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(path="a.py", tool_use_id=tool_use_id),
    )

    assert rc == 0
    # The state belongs to this one completed edit, so every row it holds goes.
    assert [call["path"] for call in LeaseHandler.calls] == ["/v1/lease/release"] * 2
    assert sorted(call["body"]["lease_id"] for call in LeaseHandler.calls) == [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    ]
    assert not state_path.exists()


def test_release_edit_releases_event_lease_recorded_under_another_path(
    tmp_path, monkeypatch, lease_server
):
    _lease_env(monkeypatch, lease_server)
    tool_use_id = "toolu_unleased"
    _seed_state(
        tmp_path,
        "slot-1",
        tool_use_id,
        {
            "file:///tmp/b.py": {
                "lease_id": "22222222-2222-4222-8222-222222222222",
                "path": "b.py",
            }
        },
    )

    rc = file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(path="a.py", tool_use_id=tool_use_id),
    )

    assert rc == 0
    assert [call["body"]["lease_id"] for call in LeaseHandler.calls] == [
        "22222222-2222-4222-8222-222222222222"
    ]


def test_release_edit_does_not_adopt_workspace_legacy_state(
    tmp_path, monkeypatch, lease_server
):
    _lease_env(monkeypatch, lease_server)
    legacy_path = file_lease_hook._legacy_state_path(tmp_path, "slot-1")
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "slot": "slot-1",
                "workspace": str(tmp_path),
                "leases": {
                    "file:///tmp/a.py": {
                        "lease_id": "11111111-1111-4111-8111-111111111111",
                        "path": "a.py",
                        "surface_id": "file:///tmp/a.py",
                    }
                },
            }
        )
    )

    rc = file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path), "--host", "claude"],
        stdin_text=_payload(path="a.py", tool_use_id="toolu_after_upgrade"),
    )

    assert rc == 0
    assert LeaseHandler.calls == []
    assert legacy_path.exists()


def test_workspace_state_cannot_substitute_foreign_lease(
    tmp_path, monkeypatch, lease_server
):
    _lease_env(monkeypatch, lease_server)
    tool_use_id = "toolu_workspace_substitution"
    owned_id = "11111111-1111-4111-8111-111111111111"
    foreign_id = "22222222-2222-4222-8222-222222222222"
    _seed_state(
        tmp_path,
        "slot-1",
        tool_use_id,
        {
            "file:///tmp/a.py": {
                "lease_id": owned_id,
                "path": "a.py",
            }
        },
    )
    workspace_state = file_lease_hook._legacy_state_path(
        tmp_path, "slot-1", tool_use_id
    )
    workspace_state.parent.mkdir(parents=True, exist_ok=True)
    workspace_state.write_text(
        json.dumps(
            {
                "version": file_lease_hook.STATE_VERSION,
                "slot": "slot-1",
                "workspace": str(tmp_path),
                "tool_use_id": tool_use_id,
                "holder_uuid": "foreign-holder",
                "leases": {
                    "file:///tmp/a.py": {
                        "lease_id": foreign_id,
                        "path": "a.py",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    rc = file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(path="a.py", tool_use_id=tool_use_id),
    )

    assert rc == 0
    assert [call["body"]["lease_id"] for call in LeaseHandler.calls] == [owned_id]
    assert workspace_state.exists()


def test_tampered_protected_row_cannot_release_foreign_lease(
    tmp_path, monkeypatch, lease_server
):
    _lease_env(monkeypatch, lease_server)
    tool_use_id = "toolu_tampered_row"
    state_path = _seed_state(
        tmp_path,
        "slot-1",
        tool_use_id,
        {
            "file:///tmp/a.py": {
                "lease_id": "11111111-1111-4111-8111-111111111111",
                "path": "a.py",
            }
        },
    )
    tampered = json.loads(state_path.read_text(encoding="utf-8"))
    tampered["leases"]["file:///tmp/a.py"]["lease_id"] = (
        "22222222-2222-4222-8222-222222222222"
    )
    file_lease_hook._write_json(state_path, tampered)

    rc = file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(path="a.py", tool_use_id=tool_use_id),
    )

    assert rc == 0
    assert LeaseHandler.calls == []
    assert state_path.exists()


def test_unknown_state_version_is_preserved(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    tool_use_id = "toolu_future_state"
    state_path = file_lease_hook._state_path(tmp_path, "slot-1", tool_use_id)
    state_path.parent.mkdir(parents=True)
    original = {
        "version": 99,
        "slot": "slot-1",
        "leases": {
            "file:///tmp/a.py": {"lease_id": "future-lease", "path": "a.py"}
        },
    }
    state_path.write_text(json.dumps(original, sort_keys=True))
    before = state_path.read_text()

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path), "--host", "claude"],
        stdin_text=_payload(path="a.py", tool_use_id=tool_use_id),
    )

    assert rc == 0
    assert LeaseHandler.calls == []
    assert state_path.read_text() == before


def test_claude_missing_tool_id_never_acquires_or_releases_shared_state(
    tmp_path, monkeypatch, lease_server
):
    _lease_env(monkeypatch, lease_server)
    args = ["pre-edit", "--workspace", str(tmp_path), "--host", "claude"]
    payload = _payload()

    assert file_lease_hook.main(args, stdin_text=payload) == 0
    assert file_lease_hook.main(args, stdin_text=payload) == 0
    assert file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path), "--host", "claude"],
        stdin_text=payload,
    ) == 0

    assert LeaseHandler.calls == []
    assert not (tmp_path / ".unitares").exists()


def test_claude_missing_tool_id_blocks_when_leases_are_required(
    tmp_path, monkeypatch, lease_server, capsys
):
    _lease_env(monkeypatch, lease_server)
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path), "--host", "claude"],
        stdin_text=_payload(),
    )

    assert rc == 2
    assert "missing tool_use_id" in capsys.readouterr().err
    assert LeaseHandler.calls == []


def test_codex_patch_acquires_all_paths_in_deterministic_order(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    calls = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        calls.append((path, body, timeout_s))
        surface = body["surface_id"]
        return {
            "ok": True,
            "lease": {
                "lease_id": f"lease-{len(calls)}",
                "surface_id": surface,
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    payload = _codex_payload(paths=("z.py", "a.py"))

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=payload,
    )

    assert rc == 0
    acquired = [body["surface_id"] for path, body, _ in calls if path.endswith("/acquire")]
    assert acquired == [f"file://{tmp_path / 'a.py'}", f"file://{tmp_path / 'z.py'}"]
    assert {
        body["ttl_s"] for path, body, _ in calls if path.endswith("/acquire")
    } == {30}
    state_path = file_lease_hook._state_path(tmp_path, "codex-slot", "call_1")
    state = json.loads(state_path.read_text())
    assert len(state["leases"]) == 2
    assert {row["tool_use_id"] for row in state["leases"].values()} == {"call_1"}
    assert len({row["holder_uuid"] for row in state["leases"].values()}) == 1


def test_codex_batch_conflict_rolls_back_new_leases(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    calls = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        calls.append((path, body))
        if path.endswith("/release"):
            return {"ok": True}
        surface = body["surface_id"]
        if surface.endswith("/b.py"):
            return {
                "ok": False,
                "error": "held_by_other",
                "surface_id": surface,
                "blocking_lease_id": "other-lease",
                "held_by_uuid": "other-agent",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        return {
            "ok": True,
            "lease": {
                "lease_id": "new-lease-a",
                "surface_id": surface,
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    payload = _codex_payload(paths=("a.py", "b.py"), tool_use_id="call_conflict")

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=payload,
    )

    assert rc == 2
    assert "held by another agent" in capsys.readouterr().err
    assert [path for path, _ in calls] == [
        "/v1/lease/acquire",
        "/v1/lease/acquire",
        "/v1/lease/release",
    ]
    assert calls[-1][1]["lease_id"] == "new-lease-a"
    assert not file_lease_hook._state_path(
        tmp_path, "codex-slot", "call_conflict"
    ).exists()


def test_acquire_persistence_failure_releases_and_applies_required_policy(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    calls = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        calls.append((path, body))
        if path.endswith("/release"):
            return {"ok": True}
        return {
            "ok": True,
            "lease": {
                "lease_id": "lease-without-state",
                "surface_id": body["surface_id"],
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    def fail_save(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    monkeypatch.setattr(file_lease_hook, "_save_state", fail_save)
    monkeypatch.setattr(
        file_lease_hook,
        "_worktree_roots",
        lambda *_args, **_kwargs: (tmp_path, tmp_path),
    )

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=_codex_payload(tool_use_id="call_save_failure"),
    )

    assert rc == 2
    assert "lease state persistence failed after acquire" in capsys.readouterr().err
    assert [path for path, _ in calls] == [
        "/v1/lease/acquire",
        "/v1/lease/release",
    ]
    assert calls[-1][1]["lease_id"] == "lease-without-state"
    assert not file_lease_hook._state_path(
        tmp_path, "codex-slot", "call_save_failure"
    ).exists()


def test_codex_batch_conflict_rolls_back_persisted_event_lease(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    calls = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        calls.append((path, body))
        if path.endswith("/heartbeat"):
            return {"ok": True}
        if path.endswith("/release"):
            return {"ok": True}
        surface = body["surface_id"]
        if surface.endswith("/b.py"):
            return {
                "ok": False,
                "error": "held_by_other",
                "surface_id": surface,
                "blocking_lease_id": "other-lease",
                "held_by_uuid": "other-agent",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        return {
            "ok": True,
            "lease": {
                "lease_id": "persisted-lease-a",
                "surface_id": surface,
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    args = ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"]
    tool_use_id = "call_retry"

    assert file_lease_hook.main(
        args,
        stdin_text=_codex_payload(paths=("a.py",), tool_use_id=tool_use_id),
    ) == 0
    calls.clear()

    rc = file_lease_hook.main(
        args,
        stdin_text=_codex_payload(paths=("a.py", "b.py"), tool_use_id=tool_use_id),
    )

    assert rc == 2
    assert "held by another agent" in capsys.readouterr().err
    assert [path for path, _ in calls] == [
        "/v1/lease/heartbeat",
        "/v1/lease/acquire",
        "/v1/lease/release",
    ]
    assert calls[-1][1]["lease_id"] == "persisted-lease-a"
    assert not file_lease_hook._state_path(
        tmp_path, "codex-slot", tool_use_id
    ).exists()


def test_codex_tool_use_ids_are_distinct_lease_holders(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    holder_by_surface = {}
    acquire_holders = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        if path.endswith("/release"):
            return {"ok": True}
        surface = body["surface_id"]
        holder = body["holder_agent_uuid"]
        acquire_holders.append(holder)
        if surface in holder_by_surface and holder_by_surface[surface] != holder:
            return {
                "ok": False,
                "error": "held_by_other",
                "surface_id": surface,
                "blocking_lease_id": "first-lease",
                "held_by_uuid": holder_by_surface[surface],
                "expires_at": "2099-01-01T00:00:00Z",
            }
        holder_by_surface[surface] = holder
        return {
            "ok": True,
            "lease": {
                "lease_id": "first-lease",
                "surface_id": surface,
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    args = ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"]

    assert file_lease_hook.main(args, stdin_text=_codex_payload(tool_use_id="call_a")) == 0
    assert file_lease_hook.main(args, stdin_text=_codex_payload(tool_use_id="call_b")) == 2
    assert len(set(acquire_holders)) == 2
    assert file_lease_hook._state_path(tmp_path, "codex-slot", "call_a").exists()
    assert not file_lease_hook._state_path(tmp_path, "codex-slot", "call_b").exists()


def test_claude_sync_release_allows_next_tool_holder(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    holder_by_surface = {}
    lease_surface = {}
    acquire_holders = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        if path.endswith("/release"):
            surface = lease_surface.pop(body["lease_id"], "")
            holder_by_surface.pop(surface, None)
            return {"ok": True}
        surface = body["surface_id"]
        holder = body["holder_agent_uuid"]
        acquire_holders.append(holder)
        prior = holder_by_surface.get(surface)
        if prior is not None and prior != holder:
            return {
                "ok": False,
                "error": "held_by_other",
                "surface_id": surface,
                "blocking_lease_id": "prior-lease",
                "held_by_uuid": prior,
                "expires_at": "2099-01-01T00:00:00Z",
            }
        holder_by_surface[surface] = holder
        lease_id = f"lease-{len(acquire_holders)}"
        lease_surface[lease_id] = surface
        return {
            "ok": True,
            "lease": {
                "lease_id": lease_id,
                "surface_id": surface,
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    args = ["pre-edit", "--workspace", str(tmp_path), "--host", "claude"]

    assert file_lease_hook.main(
        args,
        stdin_text=_payload(tool_use_id="toolu_1"),
    ) == 0
    assert file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path), "--host", "claude"],
        stdin_text=_payload(tool_use_id="toolu_1"),
    ) == 0
    assert file_lease_hook.main(
        args,
        stdin_text=_payload(tool_use_id="toolu_2"),
    ) == 0
    assert len(set(acquire_holders)) == 2


def _edit_lock_sidecars(workspace: Path) -> list[str]:
    state_dir = file_lease_hook._state_path(workspace, "probe").parent
    return sorted(
        p.name
        for p in state_dir.glob("file-leases-*-edit-*.lock")
    )


def test_release_edit_removes_its_lock_sidecar(tmp_path, monkeypatch):
    """Per-edit flock sidecars must not outlive the edit they serialized.

    Before this test existed every Claude edit left one zero-byte
    ``file-leases-<slot>-edit-<id>.lock`` behind forever.
    """
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        if path.endswith("/release"):
            return {"ok": True}
        return {
            "ok": True,
            "lease": {
                "lease_id": "lease-1",
                "surface_id": body["surface_id"],
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    assert file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path), "--host", "claude"],
        stdin_text=_payload(tool_use_id="toolu_1"),
    ) == 0
    assert len(_edit_lock_sidecars(tmp_path)) == 1

    assert file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path), "--host", "claude"],
        stdin_text=_payload(tool_use_id="toolu_1"),
    ) == 0

    assert file_lease_hook._state_paths(tmp_path, "slot-1") == []
    assert _edit_lock_sidecars(tmp_path) == []


def test_release_session_removes_per_edit_lock_sidecars(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        if path.endswith("/release"):
            return {"ok": True}
        surface = body["surface_id"]
        return {
            "ok": True,
            "lease": {
                "lease_id": f"lease-{Path(surface).name}",
                "surface_id": surface,
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    args = ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"]
    assert file_lease_hook.main(
        args, stdin_text=_codex_payload(tool_use_id="call_a", paths=("a.py",))
    ) == 0
    assert file_lease_hook.main(
        args, stdin_text=_codex_payload(tool_use_id="call_b", paths=("b.py",))
    ) == 0
    assert len(_edit_lock_sidecars(tmp_path)) == 2

    assert file_lease_hook.main(
        ["release-session", "--workspace", str(tmp_path)],
        stdin_text=json.dumps({"session_id": "codex-slot"}),
    ) == 0

    assert file_lease_hook._state_paths(tmp_path, "codex-slot") == []
    assert _edit_lock_sidecars(tmp_path) == []


def test_session_wide_lock_sidecar_is_left_in_place(tmp_path):
    """The reusable session-slot sidecar keeps its documented contract."""
    state_path = tmp_path / ".unitares" / "file-leases-slot-1.json"
    state_path.parent.mkdir()
    state_path.with_suffix(".lock").touch()

    file_lease_hook._remove_edit_lock_sidecar(state_path)

    assert state_path.with_suffix(".lock").exists()


def test_session_end_discovers_and_releases_per_tool_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    released = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        if path.endswith("/release"):
            released.append(body["lease_id"])
            return {"ok": True}
        surface = body["surface_id"]
        return {
            "ok": True,
            "lease": {
                "lease_id": f"lease-{Path(surface).name}",
                "surface_id": surface,
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    args = ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"]
    assert file_lease_hook.main(
        args, stdin_text=_codex_payload(tool_use_id="call_a", paths=("a.py",))
    ) == 0
    assert file_lease_hook.main(
        args, stdin_text=_codex_payload(tool_use_id="call_b", paths=("b.py",))
    ) == 0

    rc = file_lease_hook.main(
        ["release-session", "--workspace", str(tmp_path)],
        stdin_text=json.dumps({"session_id": "codex-slot"}),
    )

    assert rc == 0
    assert sorted(released) == ["lease-a.py", "lease-b.py"]
    assert file_lease_hook._state_paths(tmp_path, "codex-slot") == []


def test_claude_batch_releases_only_completed_edit_tool_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    slot = "shared-driver-slot"

    def seed(tool_use_id: str, lease_id: str, path: str) -> Path:
        return _seed_state(
            tmp_path,
            slot,
            tool_use_id,
            {
                f"file://{tmp_path / path}": {
                    "lease_id": lease_id,
                    "path": path,
                }
            },
        )

    completed = seed("toolu_completed", "lease-completed", "done.py")
    still_running = seed("toolu_background", "lease-background", "active.py")
    released: list[str] = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        assert method == "POST"
        assert path == "/v1/lease/release"
        released.append(body["lease_id"])
        return {"ok": True}

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)
    payload = json.dumps(
        {
            "hook_event_name": "PostToolBatch",
            "session_id": slot,
            "tool_calls": [
                {"tool_name": "Write", "tool_use_id": "toolu_completed"},
                # A sibling may share the session id but is not proven complete
                # by this batch's edit-call set.
                {"tool_name": "Read", "tool_use_id": "toolu_background"},
            ],
        }
    )

    rc = file_lease_hook.main(
        ["release-batch", "--workspace", str(tmp_path), "--host", "claude"],
        stdin_text=payload,
    )

    assert rc == 0
    assert released == ["lease-completed"]
    assert not completed.exists()
    assert still_running.exists()
    assert json.loads(still_running.read_text(encoding="utf-8"))["leases"]


def test_state_discovery_rejects_sanitized_slot_collision(tmp_path):
    slot_a = "session?one"
    slot_b = "session!one"
    assert file_lease_hook._safe_slot(slot_a) == file_lease_hook._safe_slot(slot_b)
    assert file_lease_hook._state_path(tmp_path, slot_a) != file_lease_hook._state_path(
        tmp_path, slot_b
    )

    legacy = file_lease_hook._legacy_state_path(tmp_path, slot_b, "call_b")
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"version": 1, "slot": slot_b, "leases": {}}))

    assert legacy not in file_lease_hook._state_paths(tmp_path, slot_a)
    assert legacy not in file_lease_hook._state_paths(tmp_path, slot_b)


def test_codex_post_edit_releases_every_patch_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    tool_use_id = "call_release_all"
    state_path = _seed_state(
        tmp_path,
        "codex-slot",
        tool_use_id,
        {
            f"file://{tmp_path / 'a.py'}": {
                "lease_id": "lease-a",
                "path": "a.py",
            },
            f"file://{tmp_path / 'b.py'}": {
                "lease_id": "lease-b",
                "path": "b.py",
            },
        },
    )
    released = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        assert path == "/v1/lease/release"
        released.append(body["lease_id"])
        return {"ok": True}

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)

    rc = file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=_codex_payload(tool_use_id=tool_use_id, paths=("b.py", "a.py")),
    )

    assert rc == 0
    assert sorted(released) == ["lease-a", "lease-b"]
    assert not state_path.exists()


def test_claude_hooks_wires_pretooluse_edit_guard():
    config = json.loads(
        (Path(__file__).parent.parent / "hooks" / "claude-hooks.json").read_text()
    )

    pre_hooks = config["hooks"]["PreToolUse"]
    assert pre_hooks[0]["matcher"] == "Edit|Write|MultiEdit"
    assert "pre-edit" in pre_hooks[0]["hooks"][0]["command"]


# ---------------------------------------------------------------------------
# _surface_id: worktrees of one repo must collapse to ONE surface so concurrent
# agents in different worktrees see each other's file lease.
# ---------------------------------------------------------------------------

def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_surface_id_collapses_worktrees_to_one_surface(tmp_path):
    main = tmp_path / "repo"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "src").mkdir()
    (main / "src" / "f.py").write_text("x\n")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)

    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", str(wt), "HEAD", cwd=main)
    (wt / "src" / "f.py").write_text("x\n")

    s_main = file_lease_hook._surface_id("src/f.py", main)
    s_wt = file_lease_hook._surface_id("src/f.py", wt)

    # Same logical file in two worktrees -> identical surface (the collision fix)
    assert s_main == s_wt
    # And it canonicalizes onto the main checkout, not the worktree dir
    assert str(main.resolve()) in s_main
    assert "/wt/" not in s_wt


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_surface_id_collapses_nonexistent_add_path_across_worktrees(tmp_path):
    main = tmp_path / "repo"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "seed.txt").write_text("seed\n")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", str(wt), "HEAD", cwd=main)

    relative = "not-created-yet/deep/new.py"
    s_main = file_lease_hook._surface_id(relative, main)
    s_wt = file_lease_hook._surface_id(relative, wt)

    assert s_main == s_wt
    assert str(main.resolve()) in s_main


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_surface_id_uses_target_roots_for_absolute_sibling_worktree_path(tmp_path):
    main = tmp_path / "repo"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("config", "user.email", "t@t", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "src").mkdir()
    (main / "src" / "f.py").write_text("x\n")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    sibling = tmp_path / "sibling-wt"
    _git("worktree", "add", "-q", str(sibling), "HEAD", cwd=main)

    cached_roots = file_lease_hook._worktree_roots(main)
    s_main = file_lease_hook._surface_id(str(main / "src" / "f.py"), main, cached_roots)
    s_sibling = file_lease_hook._surface_id(
        str(sibling / "src" / "f.py"), main, cached_roots
    )

    assert s_main == s_sibling
    assert str(main.resolve()) in s_sibling
    assert str(sibling.resolve()) not in s_sibling


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_surface_id_keeps_separate_git_dirs_distinct(tmp_path):
    surfaces = []
    for name in ("one", "two"):
        checkout = tmp_path / name
        metadata = tmp_path / f"{name}.gitdata"
        _git(
            "init",
            "-q",
            "--separate-git-dir",
            str(metadata),
            str(checkout),
            cwd=tmp_path,
        )
        (checkout / "src").mkdir()
        (checkout / "src" / "f.py").write_text(name)
        surfaces.append(file_lease_hook._surface_id("src/f.py", checkout))

    assert surfaces[0] != surfaces[1]
    assert "one.gitdata" in surfaces[0]
    assert "two.gitdata" in surfaces[1]


def test_pre_edit_path_setup_obeys_shared_batch_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    clock = [0.0]
    root_calls = []

    def monotonic():
        return clock[0]

    def slow_roots(start, *, timeout_s=0.75):
        root_calls.append((Path(start), timeout_s))
        clock[0] += timeout_s
        return None

    def unexpected_http(*args, **kwargs):
        raise AssertionError("lease HTTP must not start after path setup exhausts its budget")

    monkeypatch.setattr(file_lease_hook.time, "monotonic", monotonic)
    monkeypatch.setattr(file_lease_hook, "_worktree_roots", slow_roots)
    monkeypatch.setattr(file_lease_hook, "_http_json", unexpected_http)
    paths = tuple(f"/outside/repo-{index}/f.py" for index in range(256))

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=_codex_payload(paths=paths, tool_use_id="call_deadline"),
    )

    assert rc == 0
    assert clock[0] <= file_lease_hook.DEFAULT_BATCH_TIMEOUT_S
    assert len(root_calls) < 10


def test_release_edit_obeys_codex_deadline_without_probing(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    tool_use_id = "call_release_deadline"
    paths = tuple(f"/outside/repo-{index}/f.py" for index in range(256))
    _seed_state(
        tmp_path,
        "codex-slot",
        tool_use_id,
        {
            f"file://legacy/{index}": {
                "lease_id": f"lease-{index}",
                    "path": f"unrelated-{index}.py",
            }
                for index, _path in enumerate(paths)
        },
    )
    clock = [0.0]
    root_calls = []

    def monotonic():
        return clock[0]

    def slow_roots(start, *, timeout_s=0.75):
        root_calls.append((Path(start), timeout_s))
        clock[0] += timeout_s
        return None

    released = []

    def slow_release(method, path, *, token, body=None, timeout_s=None):
        released.append(body["lease_id"])
        clock[0] += 0.1
        return {"ok": True}

    monkeypatch.setattr(file_lease_hook.time, "monotonic", monotonic)
    monkeypatch.setattr(file_lease_hook, "_worktree_roots", slow_roots)
    monkeypatch.setattr(file_lease_hook, "_http_json", slow_release)

    rc = file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=_codex_payload(paths=paths, tool_use_id=tool_use_id),
    )

    assert rc == 0
    assert root_calls == []
    assert clock[0] <= 1.5 + 0.1
    assert 0 < len(released) < len(paths)


def test_release_session_discovery_obeys_codex_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.setenv("UNITARES_FILE_LEASE_BATCH_TIMEOUT_S", "0.3")
    slot = "codex-many-states"
    state_dir = file_lease_hook._state_path(tmp_path, slot).parent
    for index in range(50):
        _seed_state(
            tmp_path,
            slot,
            f"call-{index}",
            {
                f"file://repo/f-{index}.py": {
                    "lease_id": f"lease-{index}",
                    "path": f"f-{index}.py",
                }
            },
        )

    clock = [0.0]
    reads = []
    original_read_json = file_lease_hook._read_json

    def monotonic():
        return clock[0]

    def slow_read_json(path):
        reads.append(path)
        clock[0] += 0.11
        return original_read_json(path)

    def unexpected_release(*args, **kwargs):
        raise AssertionError("release HTTP must not start after discovery exhausts its budget")

    monkeypatch.setattr(file_lease_hook.time, "monotonic", monotonic)
    monkeypatch.setattr(file_lease_hook, "_read_json", slow_read_json)
    monkeypatch.setattr(file_lease_hook, "_release", unexpected_release)

    rc = file_lease_hook.main(
        ["release-session", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=json.dumps({"session_id": slot}),
    )

    assert rc == 0
    assert len(reads) < 50
    assert clock[0] <= 0.4
    assert len(list(state_dir.glob("file-leases-*.json"))) == 50


def test_release_state_lock_wait_is_bounded_by_cleanup_deadline(tmp_path, monkeypatch):
    state_path = _seed_state(
        tmp_path,
        "slot-lock",
        "call-lock",
        {"file://repo/a.py": {"lease_id": "lease-lock", "path": "a.py"}},
    )
    monkeypatch.setattr(
        file_lease_hook,
        "_release",
        lambda *args, **kwargs: pytest.fail("HTTP release started while state was locked"),
    )

    with file_lease_hook.session_cache_lock(state_path):
        started = time.monotonic()
        deadline_reached = file_lease_hook._release_state_file(
            state_path,
            workspace=tmp_path,
            token="lease-token",
            deadline=started + 0.08,
            expected_slot="slot-lock",
        )
        elapsed = time.monotonic() - started

    assert deadline_reached is True
    assert elapsed < 0.25
    assert state_path.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_surface_id_fail_open_outside_git(tmp_path):
    # Not a git repo -> degrade to the raw absolute path (never break an edit).
    s = file_lease_hook._surface_id("a/b.py", tmp_path)
    assert s == f"file://{tmp_path / 'a' / 'b.py'}"


# ---------------------------------------------------------------------------
# Required mode: a target whose repository/worktree namespace cannot be
# verified must not fall back to a raw per-worktree surface. A target that is
# genuinely outside every git repository keeps working with its raw path.
# ---------------------------------------------------------------------------

def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    return path


def _recording_http(calls):
    def fake_http(method, path, *, token, body=None, timeout_s=None):
        calls.append((path, body))
        if path.endswith("/heartbeat") or path.endswith("/release"):
            return {"ok": True}
        return {
            "ok": True,
            "lease": {
                "lease_id": f"lease-{len(calls)}",
                "surface_id": body["surface_id"],
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "idempotent": False,
        }

    return fake_http


def _target_probe_fails(workspace: Path, real_roots):
    """Real roots for the workspace, PROBE_FAILED (None) for any other path."""

    def roots(start, *, timeout_s=0.75):
        if Path(start).resolve() == workspace.resolve():
            return real_roots(start, timeout_s=timeout_s)
        return None

    return roots


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_required_mode_blocks_when_target_namespace_probe_fails(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    main = _git_repo(tmp_path / "main")
    sibling = tmp_path / "wt"
    sibling.mkdir()
    calls = []
    monkeypatch.setattr(file_lease_hook, "_http_json", _recording_http(calls))
    monkeypatch.setattr(
        file_lease_hook,
        "_worktree_roots",
        _target_probe_fails(main, file_lease_hook._worktree_roots),
    )

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(main), "--host", "codex"],
        stdin_text=_codex_payload(paths=(str(sibling / "a.py"),), tool_use_id="call_ns"),
    )

    assert rc == 2
    assert "could not verify the repository/worktree namespace" in capsys.readouterr().err
    assert calls == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_required_mode_target_probe_failure_rolls_back_event_leases(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    main = _git_repo(tmp_path / "main")
    sibling = tmp_path / "wt"
    sibling.mkdir()
    calls = []
    monkeypatch.setattr(file_lease_hook, "_http_json", _recording_http(calls))
    args = ["pre-edit", "--workspace", str(main), "--host", "codex"]
    tool_use_id = "call_ns_retry"

    assert file_lease_hook.main(
        args, stdin_text=_codex_payload(paths=("a.py",), tool_use_id=tool_use_id)
    ) == 0
    persisted = calls[-1]
    assert persisted[0] == "/v1/lease/acquire"
    calls.clear()
    monkeypatch.setattr(
        file_lease_hook,
        "_worktree_roots",
        _target_probe_fails(main, file_lease_hook._worktree_roots),
    )

    rc = file_lease_hook.main(
        args,
        stdin_text=_codex_payload(
            paths=("a.py", str(sibling / "b.py")), tool_use_id=tool_use_id
        ),
    )

    assert rc == 2
    assert "could not verify the repository/worktree namespace" in capsys.readouterr().err
    assert [path for path, _ in calls] == ["/v1/lease/release"]
    assert not file_lease_hook._state_path(main, "codex-slot", tool_use_id).exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_default_mode_target_probe_failure_keeps_raw_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.delenv("UNITARES_FILE_LEASES_REQUIRED", raising=False)
    main = _git_repo(tmp_path / "main")
    sibling = tmp_path / "wt"
    sibling.mkdir()
    calls = []
    monkeypatch.setattr(file_lease_hook, "_http_json", _recording_http(calls))
    monkeypatch.setattr(
        file_lease_hook,
        "_worktree_roots",
        _target_probe_fails(main, file_lease_hook._worktree_roots),
    )

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(main), "--host", "codex"],
        stdin_text=_codex_payload(paths=(str(sibling / "a.py"),), tool_use_id="call_raw"),
    )

    assert rc == 0
    assert [path for path, _ in calls] == ["/v1/lease/acquire"]
    assert calls[0][1]["surface_id"] == f"file://{(sibling / 'a.py').resolve()}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_required_mode_allows_target_outside_any_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    main = _git_repo(tmp_path / "main")
    notes = tmp_path / "notes"
    notes.mkdir()
    calls = []
    monkeypatch.setattr(file_lease_hook, "_http_json", _recording_http(calls))

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(main), "--host", "codex"],
        stdin_text=_codex_payload(
            paths=(str(notes / "todo.md"), "src/new.py"), tool_use_id="call_notes"
        ),
    )

    assert rc == 0
    surfaces = sorted(body["surface_id"] for path, body in calls if path.endswith("/acquire"))
    assert surfaces == sorted(
        [
            f"file://{(main / 'src' / 'new.py').resolve()}",
            f"file://{(notes / 'todo.md').resolve()}",
        ]
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_required_mode_blocks_when_target_probe_times_out(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    main = _git_repo(tmp_path / "main")
    sibling = tmp_path / "wt"
    sibling.mkdir()
    calls = []
    monkeypatch.setattr(file_lease_hook, "_http_json", _recording_http(calls))
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "-C", str(sibling.resolve())]:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 0.01)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(main), "--host", "codex"],
        stdin_text=_codex_payload(paths=(str(sibling / "a.py"),), tool_use_id="call_slow"),
    )

    assert rc == 2
    assert "could not verify the repository/worktree namespace" in capsys.readouterr().err
    assert calls == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_required_mode_blocks_when_git_refuses_target_ownership(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    main = _git_repo(tmp_path / "main")
    sibling = tmp_path / "wt"
    sibling.mkdir()
    calls = []
    monkeypatch.setattr(file_lease_hook, "_http_json", _recording_http(calls))
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "-C", str(sibling.resolve())]:
            return subprocess.CompletedProcess(
                cmd,
                128,
                stdout="",
                stderr=(
                    f"fatal: detected dubious ownership in repository at '{sibling}'\n"
                    "fatal: not a git repository (or any of the parent directories): .git\n"
                ),
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(main), "--host", "codex"],
        stdin_text=_codex_payload(paths=(str(sibling / "a.py"),), tool_use_id="call_owner"),
    )

    assert rc == 2
    assert "could not verify the repository/worktree namespace" in capsys.readouterr().err
    assert calls == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_worktree_roots_classifies_only_verified_non_repositories(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert file_lease_hook._worktree_roots(plain) is file_lease_hook._NOT_A_REPO

    # A .git entry the probe cannot use is a failed probe, not "no repository".
    broken = tmp_path / "broken"
    (broken / "src").mkdir(parents=True)
    (broken / ".git").write_text("gitdir: /nonexistent/unitares-test\n")
    assert file_lease_hook._worktree_roots(broken / "src") is None

    # Inherited repository-selection variables do not take part in the probe,
    # so they cannot turn a plain directory into a repository either.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing.git"))
    assert file_lease_hook._worktree_roots(plain) is file_lease_hook._NOT_A_REPO


def _committed_repo(path: Path) -> Path:
    _git_repo(path)
    _git("config", "user.email", "t@t", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    return path


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"])
def test_worktree_roots_ignore_inherited_repository_selection(
    tmp_path, monkeypatch, variable
):
    main = _committed_repo(tmp_path / "main")
    (main / "src").mkdir()
    other = _committed_repo(tmp_path / "other")
    expected = file_lease_hook._worktree_roots(main / "src")
    assert expected == (main.resolve(), main.resolve())

    values = {
        "GIT_DIR": str(other / ".git"),
        "GIT_COMMON_DIR": str(other / ".git"),
        "GIT_WORK_TREE": str(other),
    }
    monkeypatch.setenv(variable, values[variable])

    assert file_lease_hook._worktree_roots(main / "src") == expected


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_nested_checkout_inside_workspace_uses_its_own_namespace(tmp_path):
    outer = _committed_repo(tmp_path / "outer")
    other = _committed_repo(tmp_path / "other")
    nested = outer / "nested-wt"
    _git("worktree", "add", "-q", str(nested), "HEAD", cwd=other)
    target = nested / "src" / "f.py"
    expected = f"file://{(other / 'src' / 'f.py').resolve()}"

    # A session opened in the nested checkout and one opened in its main
    # checkout agree on the surface ...
    assert file_lease_hook._surface_id(
        str(target), nested, file_lease_hook._worktree_roots(nested), [], strict=True
    ) == expected
    # ... and so must a session whose workspace merely contains the checkout.
    outer_roots = file_lease_hook._worktree_roots(outer)
    for strict in (True, False):
        assert file_lease_hook._surface_id(
            str(target), outer, outer_roots, [], strict=strict
        ) == expected


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_workspace_targets_reuse_cached_roots_without_probing(tmp_path, monkeypatch):
    main = _committed_repo(tmp_path / "main")
    (main / "src" / "deep").mkdir(parents=True)
    roots = file_lease_hook._worktree_roots(main)
    probes = []

    def no_probe(start, *, timeout_s=0.75):
        probes.append(start)
        return None

    monkeypatch.setattr(file_lease_hook, "_worktree_roots", no_probe)
    for rel in ("a.py", "src/b.py", "src/deep/c.py", "src/new/d.py"):
        assert file_lease_hook._surface_id(
            rel, main, roots, [], strict=True
        ) == f"file://{(main / rel).resolve()}"
    assert probes == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_same_event_retry_moves_raw_surface_lease_to_verified_surface(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.delenv("UNITARES_FILE_LEASES_REQUIRED", raising=False)
    main = _committed_repo(tmp_path / "main")
    sibling = tmp_path / "wt"
    _git("worktree", "add", "-q", str(sibling), "HEAD", cwd=main)
    calls = []
    monkeypatch.setattr(file_lease_hook, "_http_json", _recording_http(calls))
    real_roots = file_lease_hook._worktree_roots
    args = ["pre-edit", "--workspace", str(main), "--host", "codex"]
    tool_use_id = "call_raw_then_verified"
    target = str(sibling / "a.py")

    # First attempt: the target probe fails, so default mode leases the raw path.
    monkeypatch.setattr(
        file_lease_hook, "_worktree_roots", _target_probe_fails(main, real_roots)
    )
    assert file_lease_hook.main(
        args, stdin_text=_codex_payload(paths=(target,), tool_use_id=tool_use_id)
    ) == 0
    raw_surface = f"file://{(sibling / 'a.py').resolve()}"
    assert calls[-1][1]["surface_id"] == raw_surface
    raw_lease_id = f"lease-{len(calls)}"
    calls.clear()

    # Retry of the same event, now with a verified namespace, in required mode.
    monkeypatch.setattr(file_lease_hook, "_worktree_roots", real_roots)
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    assert file_lease_hook.main(
        args, stdin_text=_codex_payload(paths=(target,), tool_use_id=tool_use_id)
    ) == 0

    verified_surface = f"file://{(main / 'a.py').resolve()}"
    assert [path for path, _ in calls] == ["/v1/lease/release", "/v1/lease/acquire"]
    assert calls[0][1]["lease_id"] == raw_lease_id
    assert calls[1][1]["surface_id"] == verified_surface
    state = json.loads(
        file_lease_hook._state_path(main, "codex-slot", tool_use_id).read_text()
    )
    assert sorted(state["leases"]) == [verified_surface]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_namespace_failure_releases_event_lease_named_by_another_spelling(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    main = _git_repo(tmp_path / "main")
    sibling = tmp_path / "wt"
    sibling.mkdir()
    calls = []
    monkeypatch.setattr(file_lease_hook, "_http_json", _recording_http(calls))
    args = ["pre-edit", "--workspace", str(main), "--host", "codex"]
    tool_use_id = "call_respelled"

    assert file_lease_hook.main(
        args, stdin_text=_codex_payload(paths=("a.py",), tool_use_id=tool_use_id)
    ) == 0
    first_lease_id = f"lease-{len(calls)}"
    calls.clear()
    monkeypatch.setattr(
        file_lease_hook,
        "_worktree_roots",
        _target_probe_fails(main, file_lease_hook._worktree_roots),
    )

    rc = file_lease_hook.main(
        args,
        stdin_text=_codex_payload(
            paths=(str(main / "a.py"), str(sibling / "b.py")), tool_use_id=tool_use_id
        ),
    )

    assert rc == 2
    assert "could not verify the repository/worktree namespace" in capsys.readouterr().err
    assert [(path, body["lease_id"]) for path, body in calls] == [
        ("/v1/lease/release", first_lease_id)
    ]
    assert not file_lease_hook._state_path(main, "codex-slot", tool_use_id).exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
@pytest.mark.parametrize(
    "variable", ["GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM"]
)
def test_worktree_roots_ignore_inherited_discovery_limits(
    tmp_path, monkeypatch, variable
):
    main = _committed_repo(tmp_path / "main")
    sibling = tmp_path / "wt"
    _git("worktree", "add", "-q", str(sibling), "HEAD", cwd=main)
    (sibling / "src").mkdir()
    expected = file_lease_hook._worktree_roots(sibling / "src")
    assert isinstance(expected, tuple)

    values = {
        "GIT_CEILING_DIRECTORIES": str(sibling.resolve()),
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
    }
    monkeypatch.setenv(variable, values[variable])

    assert file_lease_hook._worktree_roots(sibling / "src") == expected
    assert variable not in file_lease_hook._git_probe_env()


def test_release_edit_releases_rows_recorded_under_other_spellings(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    tool_use_id = "call_two_spellings"
    state_path = _seed_state(
        tmp_path,
        "codex-slot",
        tool_use_id,
        {
            "file:///raw/wt/src/a.py": {"lease_id": "lease-raw", "path": "./src/a.py"},
            f"file://{tmp_path / 'src' / 'a.py'}": {
                "lease_id": "lease-verified",
                "path": "src/a.py",
            },
        },
    )
    released = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        released.append(body["lease_id"])
        return {"ok": True}

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)

    assert file_lease_hook.main(
        ["release-edit", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=_codex_payload(paths=("src/a.py",), tool_use_id=tool_use_id),
    ) == 0

    assert sorted(released) == ["lease-raw", "lease-verified"]
    assert not state_path.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
@pytest.mark.parametrize("required", [True, False])
def test_retry_with_unreleasable_mismatched_lease_does_not_acquire(
    tmp_path, monkeypatch, capsys, required
):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    if required:
        monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    else:
        monkeypatch.delenv("UNITARES_FILE_LEASES_REQUIRED", raising=False)
    main = _committed_repo(tmp_path / "main")
    tool_use_id = "call_unreleasable"
    state_path = _seed_state(
        main,
        "codex-slot",
        tool_use_id,
        {"file:///raw/elsewhere/a.py": {"lease_id": "lease-raw", "path": "a.py"}},
        holder_uuid=file_lease_hook._holder_uuid(
            main,
            file_lease_hook.normalize_edit_hook(
                _codex_payload(paths=("a.py",), tool_use_id=tool_use_id), host="codex"
            ),
            {},
        ),
    )
    calls = []

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        calls.append((path, body))
        return {"ok": False, "error": "service_unavailable"}

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(main), "--host", "codex"],
        stdin_text=_codex_payload(paths=("a.py",), tool_use_id=tool_use_id),
    )

    assert rc == (2 if required else 0)
    assert [path for path, _ in calls] == ["/v1/lease/release"]
    assert json.loads(state_path.read_text())["leases"][
        "file:///raw/elsewhere/a.py"
    ]["lease_id"] == "lease-raw"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_retry_keeps_renewed_lease_when_a_symlink_now_resolves_elsewhere(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    main = _committed_repo(tmp_path / "main")
    (main / "x.py").write_text("x\n")
    (main / "y.py").write_text("y\n")
    (main / "l.py").symlink_to("x.py")
    calls = []
    monkeypatch.setattr(file_lease_hook, "_http_json", _recording_http(calls))
    args = ["pre-edit", "--workspace", str(main), "--host", "codex"]
    tool_use_id = "call_symlink_moved"

    assert file_lease_hook.main(
        args, stdin_text=_codex_payload(paths=("l.py",), tool_use_id=tool_use_id)
    ) == 0
    calls.clear()
    (main / "l.py").unlink()
    (main / "l.py").symlink_to("y.py")

    assert file_lease_hook.main(
        args, stdin_text=_codex_payload(paths=("x.py", "l.py"), tool_use_id=tool_use_id)
    ) == 0

    assert [path for path, _ in calls] == ["/v1/lease/heartbeat", "/v1/lease/acquire"]
    state = json.loads(
        file_lease_hook._state_path(main, "codex-slot", tool_use_id).read_text()
    )
    assert sorted(state["leases"]) == [
        f"file://{(main / 'x.py').resolve()}",
        f"file://{(main / 'y.py').resolve()}",
    ]


def _case_insensitive(directory: Path) -> bool:
    alias = directory.parent / directory.name.swapcase()
    try:
        return alias != directory and os.path.samefile(alias, directory)
    except OSError:
        return False


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_case_alias_of_worktree_root_maps_to_the_stored_casing(tmp_path):
    main = _committed_repo(tmp_path / "Repo")
    if not _case_insensitive(main):
        pytest.skip("volume is case-sensitive")
    alias = tmp_path / "rEPO"
    expected = f"file://{(main / 'src' / 'a.py').resolve()}"

    for workspace in (main, alias):
        roots = file_lease_hook._worktree_roots(workspace)
        for strict in (True, False):
            assert file_lease_hook._surface_id(
                str(alias / "src" / "a.py"), workspace, roots, [], strict=strict
            ) == expected
    # A probe-only path (no cached roots) is accepted the same way.
    assert file_lease_hook._surface_id(
        str(alias / "src" / "a.py"), tmp_path, None, [], strict=True
    ) == expected


def test_relative_to_root_rejects_a_case_alias_that_is_another_directory(tmp_path):
    (tmp_path / "Repo").mkdir()
    other = tmp_path / "REPO"
    if _case_insensitive(tmp_path / "Repo"):
        pytest.skip("volume is case-insensitive")
    other.mkdir()
    with pytest.raises(ValueError):
        file_lease_hook._relative_to_root(other / "a.py", tmp_path / "Repo")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_retry_rollback_keeps_the_event_lock_inode_while_reacquiring(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    main = _committed_repo(tmp_path / "main")
    tool_use_id = "call_lock_inode"
    stdin_text = _codex_payload(paths=("a.py",), tool_use_id=tool_use_id)
    state_path = _seed_state(
        main,
        "codex-slot",
        tool_use_id,
        {"file:///raw/elsewhere/a.py": {"lease_id": "lease-raw", "path": "a.py"}},
        holder_uuid=file_lease_hook._holder_uuid(
            main, file_lease_hook.normalize_edit_hook(stdin_text, host="codex"), {}
        ),
    )
    lock_path = state_path.with_suffix(".lock")
    inodes = {}
    recording = _recording_http([])

    def fake_http(method, path, *, token, body=None, timeout_s=None):
        try:
            inodes.setdefault(path, os.stat(lock_path).st_ino)
        except FileNotFoundError:
            inodes.setdefault(path, None)
        return recording(method, path, token=token, body=body, timeout_s=timeout_s)

    monkeypatch.setattr(file_lease_hook, "_http_json", fake_http)

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(main), "--host", "codex"], stdin_text=stdin_text
    )

    assert rc == 0
    assert list(inodes) == ["/v1/lease/release", "/v1/lease/acquire"]
    assert inodes["/v1/lease/release"] is not None
    assert inodes["/v1/lease/acquire"] == inodes["/v1/lease/release"]
    assert os.stat(lock_path).st_ino == inodes["/v1/lease/release"]
    assert sorted(json.loads(state_path.read_text())["leases"]) == [
        f"file://{(main / 'a.py').resolve()}"
    ]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
@pytest.mark.parametrize("channel", ["GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"])
def test_worktree_roots_honor_safe_directory_granted_through_the_environment(
    tmp_path, monkeypatch, channel
):
    main = _committed_repo(tmp_path / "main")
    (main / "src").mkdir()
    # Git's own switch for treating the repository as owned by someone else.
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    if file_lease_hook._worktree_roots(main / "src") is not None:
        # This git (or its system config) trusts the checkout regardless, so
        # the refusal the grant is meant to lift cannot be produced here.
        pytest.skip("git does not refuse this checkout as differently owned")

    if channel == "GIT_CONFIG_COUNT":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(main))
    else:
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'safe.directory'='{main}'")

    assert file_lease_hook._worktree_roots(main / "src") == (
        main.resolve(),
        main.resolve(),
    )


def test_git_probe_env_keeps_config_grants_and_drops_repository_selection(monkeypatch):
    # Independent of how the local git treats ownership: the probe must pass
    # config variables through (operators grant safe.directory with them) and
    # must not inherit anything that selects or bounds the repository.
    kept = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "/srv/shared",
        "GIT_CONFIG_PARAMETERS": "'safe.directory'='/srv/shared'",
    }
    dropped = {
        "GIT_DIR": "/elsewhere/.git",
        "GIT_WORK_TREE": "/elsewhere",
        "GIT_COMMON_DIR": "/elsewhere/.git",
        "GIT_CEILING_DIRECTORIES": "/srv",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
    }
    for name, value in {**kept, **dropped}.items():
        monkeypatch.setenv(name, value)

    env = file_lease_hook._git_probe_env()

    for name, value in kept.items():
        assert env.get(name) == value
    for name in dropped:
        assert name not in env
    assert env["LC_ALL"] == "C"


def test_http_json_never_raises_when_an_error_body_cannot_be_read(monkeypatch):
    import urllib.error

    def refusing_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)

    def unreadable(response):
        raise TimeoutError("error body read timed out")

    monkeypatch.setattr(file_lease_hook, "authorization_safe_urlopen", refusing_urlopen)
    monkeypatch.setattr(file_lease_hook, "read_limited_response", unreadable)

    result = file_lease_hook._http_json(
        "POST", "/v1/lease/release", token="t", body={"lease_id": "x"}
    )

    assert result["ok"] is False


def test_required_mode_unexpandable_target_rolls_back_event_leases(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    main = _git_repo(tmp_path / "main")
    calls = []
    monkeypatch.setattr(file_lease_hook, "_http_json", _recording_http(calls))
    args = ["pre-edit", "--workspace", str(main), "--host", "codex"]
    tool_use_id = "call_unexpandable"

    assert file_lease_hook.main(
        args, stdin_text=_codex_payload(paths=("a.py",), tool_use_id=tool_use_id)
    ) == 0
    first_lease_id = f"lease-{len(calls)}"
    calls.clear()

    rc = file_lease_hook.main(
        args,
        stdin_text=_codex_payload(
            paths=("a.py", "~unitares-no-such-user-1120/b.py"), tool_use_id=tool_use_id
        ),
    )

    assert rc == 2
    assert "BLOCKED" in capsys.readouterr().err
    assert [(path, body["lease_id"]) for path, body in calls] == [
        ("/v1/lease/release", first_lease_id)
    ]
    assert not file_lease_hook._state_path(main, "codex-slot", tool_use_id).exists()


def test_default_mode_unexpandable_target_keeps_literal_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.delenv("UNITARES_FILE_LEASES_REQUIRED", raising=False)
    calls = []
    monkeypatch.setattr(file_lease_hook, "_http_json", _recording_http(calls))
    target = "~unitares-no-such-user-1120/b.py"

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path), "--host", "codex"],
        stdin_text=_codex_payload(paths=(target,), tool_use_id="call_literal"),
    )

    assert rc == 0
    assert [body["surface_id"] for _, body in calls] == [
        f"file://{(tmp_path / target).resolve()}"
    ]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_required_mode_heartbeat_state_failure_rolls_back(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "lease-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", "/dev/null")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    main = _git_repo(tmp_path / "main")
    calls = []
    monkeypatch.setattr(file_lease_hook, "_http_json", _recording_http(calls))
    args = ["pre-edit", "--workspace", str(main), "--host", "codex"]
    tool_use_id = "call_heartbeat_persist"
    stdin_text = _codex_payload(paths=("a.py",), tool_use_id=tool_use_id)

    assert file_lease_hook.main(args, stdin_text=stdin_text) == 0
    first_lease_id = f"lease-{len(calls)}"
    calls.clear()
    real_save = file_lease_hook._save_state

    def failing_save(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(file_lease_hook, "_save_state", failing_save)

    rc = file_lease_hook.main(args, stdin_text=stdin_text)

    monkeypatch.setattr(file_lease_hook, "_save_state", real_save)
    assert rc == 2
    assert "BLOCKED" in capsys.readouterr().err
    assert [path for path, _ in calls] == ["/v1/lease/heartbeat", "/v1/lease/release"]
    assert calls[-1][1]["lease_id"] == first_lease_id


def test_http_json_reports_an_unusable_base_url_instead_of_raising(monkeypatch):
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", "not a url")

    result = file_lease_hook._http_json("POST", "/v1/lease/acquire", token="t", body={})

    assert result["ok"] is False


def _no_configured_bearer(monkeypatch, tmp_path):
    for name in ("LEASE_PLANE_BEARER_TOKEN", "UNITARES_LEASE_PLANE_BEARER_TOKEN", "GOVERNANCE_TOKEN",
                 "UNITARES_FILE_LEASES_REQUIRED", "UNITARES_FILE_LEASES_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(tmp_path / "missing.env"))


def test_loopback_lease_plane_uses_the_compose_default_bearer(tmp_path, monkeypatch, lease_server):
    """A default Compose install configures no bearer on the host; the hook
    must still take leases from the loopback lease plane Compose started."""
    _no_configured_bearer(monkeypatch, tmp_path)
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", f"http://127.0.0.1:{lease_server.server_address[1]}")

    rc = file_lease_hook.main(
        ["pre-edit", "--workspace", str(tmp_path)],
        stdin_text=_payload(tool_use_id="toolu_compose_default"),
    )

    assert rc == 0
    acquires = [c for c in LeaseHandler.calls if c["path"] == "/v1/lease/acquire"]
    assert acquires
    assert acquires[0]["authorization"] == f"Bearer {file_lease_hook.COMPOSE_DEFAULT_BEARER}"


def test_configured_bearer_wins_over_the_compose_default(tmp_path, monkeypatch, lease_server):
    _no_configured_bearer(monkeypatch, tmp_path)
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", f"http://127.0.0.1:{lease_server.server_address[1]}")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "operator-token")
    assert file_lease_hook._bearer_token_with_source() == ("operator-token", "configured")


def test_remote_lease_plane_never_gets_the_compose_default(tmp_path, monkeypatch):
    _no_configured_bearer(monkeypatch, tmp_path)
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", "https://lease.example.com")
    assert file_lease_hook._bearer_token_with_source() == ("", "none")


def test_required_mode_never_uses_the_compose_default(tmp_path, monkeypatch):
    _no_configured_bearer(monkeypatch, tmp_path)
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    assert file_lease_hook._bearer_token_with_source() == ("", "none")


def _write_secret(path, token):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"LEASE_PLANE_BEARER_TOKEN={token}\n", encoding="utf-8")


def test_secrets_file_resolution_order(tmp_path, monkeypatch):
    _no_configured_bearer(monkeypatch, tmp_path)
    monkeypatch.delenv("UNITARES_SECRETS_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", "https://lease.example.com")
    neutral = tmp_path / ".config" / "unitares" / "secrets.env"
    legacy = tmp_path / ".config" / "cirwel" / "secrets.env"

    assert file_lease_hook._secrets_env_path() == neutral

    _write_secret(legacy, "legacy-token")
    assert file_lease_hook._secrets_env_path() == legacy
    assert file_lease_hook._configured_bearer_token() == "legacy-token"

    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)
    _write_secret(neutral, "neutral-token")
    assert file_lease_hook._secrets_env_path() == neutral
    assert file_lease_hook._configured_bearer_token() == "neutral-token"

    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)
    override = tmp_path / "elsewhere.env"
    _write_secret(override, "override-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(override))
    assert file_lease_hook._configured_bearer_token() == "override-token"


@pytest.mark.parametrize(
    "line",
    [
        'export LEASE_PLANE_BEARER_TOKEN="file-token"',
        "export\tLEASE_PLANE_BEARER_TOKEN=file-token",
        "  export   LEASE_PLANE_BEARER_TOKEN = 'file-token'",
    ],
)
def test_secrets_file_lines_may_carry_a_shell_export_prefix(tmp_path, monkeypatch, line):
    """A secrets file that shells source writes `export KEY=value`. A hook
    started without that shell's environment must still find the bearer there
    rather than fall back to the Compose default and be refused."""
    _no_configured_bearer(monkeypatch, tmp_path)
    # Record the name as absent so teardown removes what the loader inserts.
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "placeholder")
    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN")
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:8788")
    secrets = tmp_path / "secrets.env"
    secrets.write_text(f"# shell-sourced\n{line}\n", encoding="utf-8")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(secrets))

    assert file_lease_hook._bearer_token_with_source() == ("file-token", "configured")
    assert "export LEASE_PLANE_BEARER_TOKEN" not in os.environ


def test_a_key_that_only_starts_with_export_is_kept_whole(tmp_path, monkeypatch):
    _no_configured_bearer(monkeypatch, tmp_path)
    monkeypatch.setenv("EXPORTED_TEST_KEY", "placeholder")
    monkeypatch.delenv("EXPORTED_TEST_KEY")
    monkeypatch.setenv("exportEXPORTED_TEST_KEY", "placeholder")
    monkeypatch.delenv("exportEXPORTED_TEST_KEY")
    secrets = tmp_path / "secrets.env"
    secrets.write_text("exportEXPORTED_TEST_KEY=kept\n", encoding="utf-8")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(secrets))

    file_lease_hook._load_env_file()

    assert os.environ.get("exportEXPORTED_TEST_KEY") == "kept"
    assert "EXPORTED_TEST_KEY" not in os.environ


def test_status_on_when_the_bearer_authenticates(tmp_path, monkeypatch, lease_server):
    _lease_env(monkeypatch, lease_server)
    status = file_lease_hook.lease_status()
    assert status["state"] == "on"
    assert status["bearer"] == "configured"
    assert file_lease_hook._status_line(status) == ""


@pytest.mark.parametrize(
    "payload",
    [{}, {"ok": True}, {"ok": True, "protocol_version": "v2.0"}, {"ok": "yes", "protocol_version": "v1.0"}],
)
def test_status_needs_a_lease_plane_health_body(tmp_path, monkeypatch, lease_server, payload):
    """A 2xx from some other service on that port must not read as protected."""
    _lease_env(monkeypatch, lease_server)
    LeaseHandler.health_payload = payload
    status = file_lease_hook.lease_status()
    assert status["state"] == "off"
    assert status["reason"] == "not_a_lease_plane"
    assert "does not answer as a v1 lease plane" in file_lease_hook._status_line(status)


def test_status_off_when_the_bearer_is_rejected(tmp_path, monkeypatch, lease_server):
    _no_configured_bearer(monkeypatch, tmp_path)
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", f"http://127.0.0.1:{lease_server.server_address[1]}")
    status = file_lease_hook.lease_status()
    assert status == {
        "state": "off",
        "reason": "bearer_rejected",
        "url": f"http://127.0.0.1:{lease_server.server_address[1]}",
        "bearer": "compose_default",
    }
    line = file_lease_hook._status_line(status)
    assert line.startswith("File leases: OFF (the lease plane at ")
    assert "LEASE_PLANE_BEARER_TOKEN" in line


def test_status_off_when_no_lease_plane_answers(tmp_path, monkeypatch):
    _no_configured_bearer(monkeypatch, tmp_path)
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:1")
    status = file_lease_hook.lease_status()
    assert status["state"] == "off"
    assert status["reason"] == "unreachable"
    line = file_lease_hook._status_line(status)
    assert "no lease plane answered at http://127.0.0.1:1" in line
    assert "UNITARES_FILE_LEASES_ENABLED=0" in line


def test_status_is_silent_when_leases_are_switched_off(tmp_path, monkeypatch):
    _no_configured_bearer(monkeypatch, tmp_path)
    monkeypatch.setenv("UNITARES_FILE_LEASES_ENABLED", "0")
    status = file_lease_hook.lease_status()
    assert status["state"] == "disabled"
    assert file_lease_hook._status_line(status) == ""


def test_status_command_reads_no_stdin(tmp_path, monkeypatch, capsys):
    _no_configured_bearer(monkeypatch, tmp_path)
    monkeypatch.setenv("LEASE_PLANE_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setattr("sys.stdin", None)
    assert file_lease_hook.main(["status", "--format", "line"]) == 0
    assert "File leases: OFF" in capsys.readouterr().out


def test_session_start_says_when_file_leases_are_off(tmp_path):
    from tests.test_session_start_golden import _render

    context = _render(
        tmp_path,
        extra_env={
            "UNITARES_FILE_LEASES_ENABLED": "1",
            "LEASE_PLANE_BASE_URL": "http://127.0.0.1:1",
        },
    )
    assert "File leases: OFF (no lease plane answered at http://127.0.0.1:1)" in context


def test_session_start_adds_nothing_when_file_leases_work(tmp_path, lease_server):
    from tests.test_session_start_golden import _render

    context = _render(
        tmp_path,
        extra_env={
            "UNITARES_FILE_LEASES_ENABLED": "1",
            "LEASE_PLANE_BASE_URL": f"http://127.0.0.1:{lease_server.server_address[1]}",
            "LEASE_PLANE_BEARER_TOKEN": "lease-token",
            "UNITARES_ALLOW_INSECURE_LOOPBACK_LEASE_HTTP": "1",
        },
    )
    assert "File leases" not in context


def test_an_environment_alias_beats_the_secrets_file(tmp_path, monkeypatch):
    """GOVERNANCE_TOKEN in the environment must not be shadowed by the secrets
    file's LEASE_PLANE_BEARER_TOKEN, which the loader inserts into os.environ."""
    _no_configured_bearer(monkeypatch, tmp_path)
    secrets = tmp_path / "secrets.env"
    _write_secret(secrets, "file-token")
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(secrets))
    monkeypatch.setenv("GOVERNANCE_TOKEN", "env-alias-token")
    assert file_lease_hook._configured_bearer_token() == "env-alias-token"


def test_status_is_bounded_by_a_wall_clock_deadline(tmp_path, monkeypatch):
    _no_configured_bearer(monkeypatch, tmp_path)

    def stall(_timeout_s):
        time.sleep(5)
        return {"state": "on"}

    monkeypatch.setattr(file_lease_hook, "_lease_status_unbounded", stall)
    started = time.monotonic()
    status = file_lease_hook.lease_status(deadline_s=0.2)
    assert time.monotonic() - started < 1.0
    assert status["state"] == "off"
    assert status["error"] == "deadline_exceeded"
