"""A fresh install with no UNITARES server must not break the host session.

Every bundled hook has to exit 0 without writing to stderr when the server is
unreachable. Directory installs reach people who have not stood a server up yet.
"""
import json
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

HOOKS = [
    "session-start",
    "post-stop",
    "pre-edit",
    "pre-push",
    "pre-governance-call",
    "user-prompt-submit",
    "session-end",
    "post-edit",
    "post-tool-failure",
]


@pytest.mark.parametrize("hook", HOOKS)
def test_hook_is_silent_and_nonblocking_when_server_down(hook, tmp_path):
    payload = json.dumps({
        "session_id": "server-down-slot",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "x")},
        "cwd": str(tmp_path),
        "prompt": "hello",
        "last_assistant_message": "done",
    })
    result = subprocess.run(
        [str(PLUGIN_ROOT / "hooks" / hook), "--host", "claude"],
        input=payload,
        text=True,
        capture_output=True,
        timeout=60,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "PWD": str(tmp_path),
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            # Port 9 (discard): nothing listens, connections are refused.
            "UNITARES_SERVER_URL": "http://127.0.0.1:9",
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
