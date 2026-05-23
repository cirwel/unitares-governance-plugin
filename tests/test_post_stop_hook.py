"""Contract test: Stop hook fires exactly one turn_stop check-in."""

from __future__ import annotations

import json
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from _session_lookup import _slot_filename  # noqa: E402

# Reuse mock server + reusable TCP subclass from the session-start test file.
from tests.test_session_start_checkin import RecordingHandler, _ReusableTCPServer  # noqa: E402


def test_post_stop_emits_turn_stop_checkin(tmp_path):
    """post-stop hook posts a check-in with event='turn_stop'."""
    RecordingHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), RecordingHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    # Pre-populate a slot-scoped session cache in $PWD/.unitares.
    session_dir = tmp_path / ".unitares"
    session_dir.mkdir()
    slot = "test-slot"
    (session_dir / _slot_filename(slot)).write_text(json.dumps({
        "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
        "client_session_id": "agent-test1234",
        "continuity_token": "v1.faketoken",
        "slot": slot,
    }))

    # Minimal Stop hook payload: tool_calls list + final_text
    stop_payload = json.dumps({
        "hook_event_name": "Stop",
        "session_id": slot,
        "tool_calls": [
            {"name": "Read"}, {"name": "Edit"}, {"name": "Bash"}
        ],
        "final_text": "Completed the refactor; all tests pass.",
    })

    try:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "UNITARES_SERVER_URL": f"http://127.0.0.1:{port}",
            "UNITARES_CHECKIN_LOG": str(tmp_path / "checkins.log"),
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PWD": str(tmp_path),
        }
        hook = PLUGIN_ROOT / "hooks" / "post-stop"
        # cwd=tmp_path is REQUIRED: bash overwrites $PWD at startup to
        # match the actual working directory, so env PWD alone doesn't
        # tell the hook where to find the session cache.
        subprocess.run(
            [str(hook)],
            env=env,
            cwd=str(tmp_path),
            input=stop_payload,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)

    checkins = [
        c for c in RecordingHandler.calls
        if c.get("name") == "process_agent_update"
        and c["arguments"].get("metadata", {}).get("event") == "turn_stop"
    ]
    assert len(checkins) == 1, (
        f"expected exactly 1 turn_stop check-in; got {len(checkins)}: "
        f"{[c.get('name') for c in RecordingHandler.calls]}"
    )
    text = checkins[0]["arguments"]["response_text"]
    assert "3 tool call" in text
    assert "Completed the refactor" in text
