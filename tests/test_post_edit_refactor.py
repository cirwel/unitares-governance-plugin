"""Regression test: post-edit threshold check-in routes through checkin.py
and carries metadata.source='plugin_hook', metadata.event='auto_edit'.

Before the refactor, post-edit posted inline and did not stamp metadata.
This test pins the new contract.
"""

from __future__ import annotations

import json
import os
import socketserver
import subprocess
import threading
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent

from tests.test_session_start_checkin import RecordingHandler, _ReusableTCPServer  # noqa: E402


def test_post_edit_routes_through_checkin_py_with_plugin_hook_metadata(tmp_path):
    """Threshold-triggered auto-checkin posts via scripts/checkin.py with
    metadata.source='plugin_hook' and metadata.event='auto_edit'."""
    RecordingHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), RecordingHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    # Pre-populate session cache and milestone so the decision helper fires.
    # Per S20.1a, the slot is derived from the stdin session_id (not the
    # cache `slot` field), so the slotted cache filename and the stdin slot
    # must match for the hook to find the cache.
    slot = "edit-slot"
    unitares_dir = tmp_path / ".unitares"
    unitares_dir.mkdir()
    (unitares_dir / f"session-{slot}.json").write_text(json.dumps({
        "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
        "client_session_id": "agent-edit-test",
        "continuity_token": "v1.edit-tok",
        "slot": slot,
        "last_checkin_ts": int(time.time()) - 10_000,  # long past threshold
    }))
    (unitares_dir / "last-milestone.json").write_text(json.dumps({
        "edit_count": 10,  # well past default threshold of 5
        "files_touched": ["a.py", "b.py"],
        "last_edit_ts": int(time.time()),
        "first_edit_ts": int(time.time()) - 10_000,
    }))

    # Realistic PostToolUse hook payload on stdin (Claude Code passes
    # session_id; S20.1a strictly requires it for slot-scoped writes).
    hook_payload = json.dumps({
        "hook_event_name": "PostToolUse",
        "session_id": slot,
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "c.py")},
    })

    try:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "UNITARES_SERVER_URL": f"http://127.0.0.1:{port}",
            "UNITARES_CHECKIN_LOG": str(tmp_path / "cl.log"),
            "UNITARES_AUTO_CHECKIN_ENABLED": "1",
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PWD": str(tmp_path),
        }
        hook = PLUGIN_ROOT / "hooks" / "post-edit"
        subprocess.run(
            [str(hook)],
            env=env,
            input=hook_payload,
            text=True,
            timeout=15,
            check=False,
            cwd=str(tmp_path),
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)

    checkins = [
        c for c in RecordingHandler.calls
        if c.get("name") == "process_agent_update"
    ]
    # We need at least one check-in with auto_edit metadata
    auto_edit = [c for c in checkins if c["arguments"].get("metadata", {}).get("event") == "auto_edit"]
    assert len(auto_edit) >= 1, (
        f"expected at least one auto_edit check-in; got {len(checkins)} total check-ins, "
        f"events: {[c['arguments'].get('metadata', {}).get('event') for c in checkins]}"
    )
    assert auto_edit[0]["arguments"]["metadata"]["source"] == "plugin_hook"
    assert auto_edit[0]["arguments"]["epistemic_class"] == "substrate_interpretation"
    assert "continuity_token" not in auto_edit[0]["arguments"]
