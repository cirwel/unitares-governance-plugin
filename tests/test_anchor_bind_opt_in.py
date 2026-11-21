"""The Stop hook and an orchestrated thread anchor: the scoped opt-in
(UNITARES_AUTO_ONBOARD=anchored) and what is reported when nothing binds."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from _session_lookup import _slot_filename  # noqa: E402
from anchor_bind_report import classify  # noqa: E402

from tests.test_session_start_checkin import RecordingHandler, _ReusableTCPServer  # noqa: E402

ANCHOR = "agent:/thread-4242"
OWNER = "424242"
SLOT = "anchor-slot-0001"


class AnchorHandler(RecordingHandler):
    calls: list[dict] = []
    onboard_result: dict = {}

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            data = {}
        AnchorHandler.calls.append(data)
        if data.get("name") == "onboard":
            response = {"result": AnchorHandler.onboard_result}
        else:
            response = {"result": {"success": True}}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


def _identity(**extra):
    return {
        "success": True,
        "uuid": "11111111-2222-4333-8444-555555555555",
        "agent_id": "Claude_20261121",
        "client_session_id": "agent-11111111-222",
        "session_resolution_source": "explicit_client_session_id",
        "display_name": "claude-thread-4242",
        "onboard_origin": "orchestrated_resume",
        **extra,
    }


def _stop(tmp_path, *, env, onboard_result=None, host_pid=OWNER):
    """Run one Stop for a turn in which the agent bound nothing."""
    AnchorHandler.calls = []
    AnchorHandler.onboard_result = onboard_result if onboard_result is not None else _identity()
    srv = _ReusableTCPServer(("127.0.0.1", 0), AnchorHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    log = tmp_path / "checkins.log"
    try:
        result = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop")],
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "HOME": str(tmp_path),
                "PWD": str(tmp_path),
                "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
                "UNITARES_SERVER_URL": f"http://127.0.0.1:{srv.server_address[1]}",
                "UNITARES_CHECKIN_LOG": str(log),
                "UNITARES_HOOK_HOST_PID": host_pid,
                **env,
            },
            cwd=str(tmp_path),
            input=json.dumps({
                "hook_event_name": "Stop",
                "session_id": SLOT,
                "stop_hook_active": False,
                "last_assistant_message": "Did the work.",
            }),
            text=True, capture_output=True, timeout=20, check=False,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)
    onboards = [c for c in AnchorHandler.calls if c.get("name") == "onboard"]
    lines = log.read_text().splitlines() if log.exists() else []
    anchor_lines = [line for line in lines if "event=anchor_bind" in line]
    return result, onboards, anchor_lines


OWNED = {
    "UNITARES_CLIENT_SESSION_ID": ANCHOR,
    "UNITARES_ORCHESTRATED": "1",
    "UNITARES_ORCHESTRATED_OWNER_PID": OWNER,
}


def test_anchored_mode_binds_the_owner_of_the_anchor(tmp_path):
    result, onboards, anchor_lines = _stop(
        tmp_path, env={**OWNED, "UNITARES_AUTO_ONBOARD": "anchored"},
        onboard_result=_identity(is_new=False),
    )
    assert result.returncode == 0
    assert len(onboards) == 1
    arguments = onboards[0]["arguments"]
    assert arguments["client_session_id"] == ANCHOR
    assert arguments["orchestrated"] is True
    assert arguments["onboard_origin"] == "orchestrated_resume"
    assert "force_new" not in arguments or arguments["force_new"] is False
    assert len(anchor_lines) == 1 and "status=anchor_resumed" in anchor_lines[0]
    assert "uuid=11111111" in anchor_lines[0]
    assert result.stderr.strip() == ""
    cached = json.loads((tmp_path / ".unitares" / _slot_filename(SLOT)).read_text())
    assert cached["uuid"] == "11111111-2222-4333-8444-555555555555"


@pytest.mark.parametrize("env", [
    # No anchor at all: an ordinary session that inherited the variable.
    {},
    # A CLI the anchored agent started. run-hook.cmd strips the anchor for it
    # before any hook runs; this is the environment it is left with.
    {"UNITARES_ANCHOR_IGNORED": "1"},
    # The same child reaching the hook without that launcher: the owner is
    # some other process.
    {**OWNED, "UNITARES_ORCHESTRATED_OWNER_PID": "1"},
    # An anchor without the orchestrated marker.
    {"UNITARES_CLIENT_SESSION_ID": ANCHOR, "UNITARES_ORCHESTRATED_OWNER_PID": OWNER},
])
def test_anchored_mode_is_off_for_every_other_process(tmp_path, env):
    result, onboards, anchor_lines = _stop(
        tmp_path, env={**env, "UNITARES_AUTO_ONBOARD": "anchored"},
    )
    assert result.returncode == 0
    assert onboards == [], "anchored mode minted an identity for a process that owns no anchor"
    assert anchor_lines == []
    assert not (tmp_path / ".unitares" / _slot_filename(SLOT)).exists()


def test_on_still_binds_a_process_that_owns_no_anchor(tmp_path):
    """The contrast that makes `anchored` worth having: `on` reaches everyone."""
    _, onboards, _ = _stop(tmp_path, env={"UNITARES_AUTO_ONBOARD": "on"})
    assert len(onboards) == 1
    assert onboards[0]["arguments"]["onboard_origin"] == "harness_backstop"


@pytest.mark.parametrize("env,why", [
    ({}, "auto-onboarding is off"),
    ({"UNITARES_AUTO_ONBOARD": "off"}, "auto-onboarding is off"),
    ({"UNITARES_AUTO_ONBOARD": "anchored", "UNITARES_DISABLE_AUTO_ONBOARD": "1"}, "auto-onboarding is off"),
    ({"UNITARES_AUTO_ONBOARD": "anchored", "UNITARES_CHECKINS": "off"}, "check-ins are off"),
])
def test_an_owned_anchor_nothing_bound_is_reported(tmp_path, env, why):
    result, onboards, anchor_lines = _stop(tmp_path, env={**OWNED, **env})
    assert result.returncode == 0
    assert onboards == []
    assert len(anchor_lines) == 1 and "status=anchor_unbound" in anchor_lines[0]
    assert why in anchor_lines[0]
    assert "no identity for agent:/thread-4242" in result.stderr
    assert "--bind" in result.stderr


def test_a_bind_that_fails_is_reported_and_not_called_unbound(tmp_path):
    result, onboards, anchor_lines = _stop(
        tmp_path, env={**OWNED, "UNITARES_AUTO_ONBOARD": "anchored"},
        onboard_result={"success": False, "error": "lineage_declaration_required"},
    )
    assert result.returncode == 0
    assert len(onboards) == 1
    assert len(anchor_lines) == 1 and "status=anchor_bind_fail" in anchor_lines[0]
    assert "could not bind agent:/thread-4242" in result.stderr


def test_a_bind_that_created_a_new_identity_says_so(tmp_path):
    _, _, anchor_lines = _stop(
        tmp_path, env={**OWNED, "UNITARES_AUTO_ONBOARD": "anchored"},
        onboard_result=_identity(is_new=True),
    )
    assert len(anchor_lines) == 1 and "status=anchor_minted" in anchor_lines[0]


def test_a_session_without_an_anchor_is_never_reported(tmp_path):
    for env in ({}, {"UNITARES_AUTO_ONBOARD": "on"}):
        result, _, anchor_lines = _stop(tmp_path, env=env)
        assert anchor_lines == []
        assert "anchor" not in result.stderr


@pytest.mark.parametrize("raw,status", [
    ('{"status": "ok", "uuid": "u", "is_new": false}', "anchor_resumed"),
    ('{"status": "ok", "uuid": "u", "is_new": true}', "anchor_minted"),
    ('{"status": "ok", "uuid": "u"}', "anchor_bound"),
    ('{"status": "ok", "uuid": ""}', "anchor_bind_fail"),
    ('{"status": "onboard_failed", "error": "nope"}', "anchor_bind_fail"),
    ("", "anchor_bind_fail"),
    ("not json", "anchor_bind_fail"),
    ("[]", "anchor_bind_fail"),
])
def test_classify(raw, status):
    assert classify(raw)[0] == status
