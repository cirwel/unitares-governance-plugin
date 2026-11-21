"""The plugin as a client of the server's harness-onboarding policy: asking by
default, accepting a decline, and saying where a nested process ran."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from _session_lookup import _slot_filename  # noqa: E402
from onboard_helper import (  # noqa: E402
    _spawned_under,
    is_harness_onboarding_declined,
    run_onboard,
)

from tests.test_anchor_bind_opt_in import OWNED, SLOT, AnchorHandler, _identity, _stop  # noqa: E402
from tests.test_session_start_checkin import RecordingHandler, _ReusableTCPServer  # noqa: E402

DECLINED = {
    "success": True,
    "refused": True,
    "status": "harness_onboarding_declined",
    "tool": "onboard",
    "policy": "agent_only",
    "hint": "This server accepts identities an agent claims.",
}


def test_decline_is_recognised_and_nothing_else_is():
    assert is_harness_onboarding_declined(DECLINED)
    for other in (
        {"success": True, "uuid": "u"},
        {"refused": True, "status": "lineage_declaration_required"},
        {"status": "harness_onboarding_declined"},
        {"refused": "true", "status": "harness_onboarding_declined"},
        None, [], "harness_onboarding_declined",
    ):
        assert not is_harness_onboarding_declined(other)


def test_a_declined_onboard_is_reported_as_declined_and_caches_nothing(tmp_path):
    written = []
    result = run_onboard(
        server_url="http://localhost:8767",
        agent_name="claude-test",
        model_type="claude-code",
        workspace=tmp_path,
        slot=SLOT,
        post_json=lambda *a, **k: {"result": DECLINED},
        read_cache=lambda *a, **k: {},
        write_cache=lambda *a, **k: written.append(a),
    )
    assert result["status"] == "harness_declined"
    assert "uuid" not in result
    assert written == []


@pytest.mark.parametrize("kwargs", [
    {},
    {"client_session_id": "agent:/thread-4242", "orchestrated": True},
])
def test_every_harness_made_call_says_it_is_one(tmp_path, kwargs):
    sent = []

    def post_json(url, payload, timeout, auth_token=None):
        sent.append(payload)
        return {"result": _identity()}

    run_onboard(
        server_url="http://localhost:8767", agent_name="claude-test",
        model_type="claude-code", workspace=tmp_path, slot=SLOT,
        post_json=post_json, read_cache=lambda *a, **k: {}, write_cache=lambda *a, **k: None,
        **kwargs,
    )
    assert sent[0]["arguments"]["harness_request"] is True


@pytest.mark.parametrize("value,expected", [
    ("agent:/thread-4242", {"anchor": "agent:/thread-4242"}),
    ("agent:_thread-4242", {"anchor": "agent:_thread-4242"}),
    ("  agent:/thread-4242\n", {"anchor": "agent:/thread-4242"}),
    ("", None),
    ("thread-4242", None),
    ("agent:/thread-", None),
    ("agent:/thread-a b", None),
    ("11111111-2222-4333-8444-555555555555", None),
])
def test_spawned_under_declaration(value, expected):
    assert _spawned_under(value) == expected


def test_spawned_under_rides_a_fresh_mint_and_is_never_a_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("UNITARES_SPAWNED_UNDER", "agent:/thread-4242")
    sent = []

    def post_json(url, payload, timeout, auth_token=None):
        sent.append(payload)
        return {"result": _identity()}

    common = dict(
        server_url="http://localhost:8767", agent_name="claude-test",
        model_type="claude-code", workspace=tmp_path,
        post_json=post_json, read_cache=lambda *a, **k: {}, write_cache=lambda *a, **k: None,
    )
    run_onboard(slot=SLOT, **common)
    fresh = sent[0]["arguments"]
    assert fresh["spawned_under"] == {"anchor": "agent:/thread-4242"}
    assert "parent_agent_id" not in fresh and "client_session_id" not in fresh

    # The owner of an anchor resumes; it was not spawned under itself.
    run_onboard(slot="other-slot", client_session_id="agent:/thread-4242", orchestrated=True, **common)
    assert "spawned_under" not in sent[1]["arguments"]


def test_a_nested_cli_declares_the_conversation_it_ran_under(tmp_path):
    """Through the real launcher: a process that inherits an anchor it does not
    own is refused the anchor, binds its own identity, and says where it ran."""
    AnchorHandler.calls = []
    AnchorHandler.onboard_result = _identity()
    srv = _ReusableTCPServer(("127.0.0.1", 0), AnchorHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", f'exec "{PLUGIN_ROOT / "hooks" / "run-hook.cmd"}" post-stop --host claude'],
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "HOME": str(tmp_path),
                "PWD": str(tmp_path),
                "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
                "UNITARES_SERVER_URL": f"http://127.0.0.1:{srv.server_address[1]}",
                "UNITARES_CHECKIN_LOG": str(tmp_path / "checkins.log"),
                "UNITARES_STOP_DETACH": "0",
                # Inherited from the conversation's CLI, whose PID this is not.
                "UNITARES_CLIENT_SESSION_ID": "agent:/thread-4242",
                "UNITARES_ORCHESTRATED": "1",
                "UNITARES_ORCHESTRATED_OWNER_PID": "1",
                "UNITARES_PARENT_AGENT_ID": "11111111-2222-4333-8444-555555555555",
            },
            cwd=str(tmp_path),
            input=json.dumps({
                "hook_event_name": "Stop", "session_id": "nested-slot-0001",
                "stop_hook_active": False, "last_assistant_message": "Reviewed.",
            }),
            text=True, capture_output=True, timeout=90, check=False,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)
    assert result.returncode == 0, result.stderr
    onboards = [c for c in AnchorHandler.calls if c.get("name") == "onboard"]
    assert len(onboards) == 1, AnchorHandler.calls
    arguments = onboards[0]["arguments"]
    assert arguments["spawned_under"] == {"anchor": "agent:/thread-4242"}
    assert arguments["force_new"] is True and arguments["harness_request"] is True
    # Refused the anchor, and given no lineage it did not declare itself.
    assert "client_session_id" not in arguments
    assert "parent_agent_id" not in arguments
    assert "orchestrated" not in arguments


def test_an_anchored_turn_the_server_declines_is_reported_as_declined(tmp_path):
    result, onboards, anchor_lines = _stop(
        tmp_path, env={**OWNED, "UNITARES_AUTO_ONBOARD": "anchored"}, onboard_result=DECLINED,
    )
    assert result.returncode == 0
    assert len(onboards) == 1
    assert len(anchor_lines) == 1 and "status=anchor_declined" in anchor_lines[0]
    assert "creates identities only for agents that claim one" in result.stderr
    assert "start_session()" in result.stderr
    assert not (tmp_path / ".unitares" / _slot_filename(SLOT)).exists()


def test_an_unanchored_turn_the_server_declines_stays_quiet_and_unbound(tmp_path):
    result, onboards, anchor_lines = _stop(tmp_path, env={}, onboard_result=DECLINED)
    assert result.returncode == 0
    assert len(onboards) == 1  # the default asked
    assert anchor_lines == []
    assert "anchor" not in result.stderr and "UNITARES" not in result.stderr
    assert not (tmp_path / ".unitares" / _slot_filename(SLOT)).exists()
    # The turn still leaves its identity-free observation; it is not lost.
    floor = [c for c in AnchorHandler.calls if c.get("name") != "onboard"]
    assert floor, AnchorHandler.calls


def test_the_default_asks_a_local_server_and_binds_what_it_gives(tmp_path):
    result, onboards, _ = _stop(tmp_path, env={}, onboard_result=_identity(is_new=True))
    assert len(onboards) == 1
    assert onboards[0]["arguments"]["harness_request"] is True
    cached = json.loads((tmp_path / ".unitares" / _slot_filename(SLOT)).read_text())
    assert cached["uuid"] == "11111111-2222-4333-8444-555555555555"


class _PolicyHealthHandler(RecordingHandler):
    policy = "agent_only"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "status": "ok", "onboarding": {"harness": self.policy, "basis": "explicit"},
        }).encode())


def _session_start(tmp_path, *, policy, env=None):
    _PolicyHealthHandler.policy = policy
    RecordingHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), _PolicyHealthHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "session-start"), "--host", "claude"],
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "HOME": str(tmp_path),
                "PWD": str(tmp_path),
                "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
                "UNITARES_SERVER_URL": f"http://127.0.0.1:{srv.server_address[1]}",
                "UNITARES_HOOK_SKIP_WORKSPACE_BRIEFING": "1",
                "UNITARES_FILE_LEASES_ENABLED": "0",
                "UNITARES_EAGER_BIND": "off",
                **(env or {}),
            },
            cwd=str(tmp_path), input=json.dumps({"session_id": "policy-slot-0001"}),
            text=True, capture_output=True, timeout=90, check=False,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def test_session_start_does_not_promise_an_identity_the_server_will_not_create(tmp_path):
    context = _session_start(tmp_path, policy="agent_only")
    assert "this server creates identities only for agents that claim one" in context
    assert "lazily create" not in context


def test_session_start_promises_the_bind_on_a_welcoming_server(tmp_path):
    context = _session_start(tmp_path, policy="welcome")
    assert "lazily create" in context
    assert "only for agents that claim one" not in context


def test_an_anchored_session_on_an_agent_only_server_is_told_to_claim_first(tmp_path):
    context = _session_start(tmp_path, policy="agent_only", env={
        **OWNED, "UNITARES_HOOK_HOST_PID": OWNED["UNITARES_ORCHESTRATED_OWNER_PID"],
    })
    assert "This server creates identities only for agents that claim one" in context
    assert "the Stop hook binds this conversation's anchor" not in context
    assert "start_session()" in context
