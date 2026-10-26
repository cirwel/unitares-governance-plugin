"""Contract test: Stop hook fires exactly one turn_stop check-in."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from _session_lookup import _slot_filename  # noqa: E402

# Reuse mock server + reusable TCP subclass from the session-start test file.
from tests.test_session_start_checkin import RecordingHandler, _ReusableTCPServer  # noqa: E402


class LazyOnboardHandler(RecordingHandler):
    """Mock governance server that returns a usable identity for onboard."""

    calls: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            data = {"raw": body.decode(errors="replace")}
        LazyOnboardHandler.calls.append(data)

        if data.get("name") == "onboard":
            response = {
                "result": {
                    "success": True,
                    "uuid": "11111111-2222-4333-8444-555555555555",
                    "agent_id": "Claude_20260615",
                    "client_session_id": "agent-11111111-222",
                    "continuity_token": "v1.transient-token",
                    "session_resolution_source": "force_new",
                    "continuity_token_supported": True,
                    "display_name": "claude-test#slot",
                }
            }
        else:
            response = {"result": {"success": True}}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


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
    cache = session_dir / _slot_filename(slot)
    cache.write_text(json.dumps({
        "uuid": "86ae619f-87e0-4040-8f29-eacece0c7904",
        "client_session_id": "agent-test1234",
        "schema_version": 2,
        "slot": slot,
    }))
    cache.chmod(0o600)

    # Current Claude Stop payload: the tool count is not included.
    stop_payload = json.dumps({
        "hook_event_name": "Stop",
        "session_id": slot,
        "stop_hook_active": False,
        "last_assistant_message": "Completed the refactor; all tests pass.",
        "model": "claude-opus-4-1-20250805",
        "model_provider": "anthropic",
        "harness_version": "1.0.83",
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
    assert "tool count unavailable" in text
    assert "Completed the refactor" in text
    assert checkins[0]["arguments"]["epistemic_class"] == "substrate_interpretation"
    assert "continuity_token" not in checkins[0]["arguments"]
    runtime = checkins[0]["arguments"]["provenance_context"]["runtime_provenance"]
    assert runtime["model"]["identifier"] == "claude-opus-4-1-20250805"
    assert runtime["model"]["provider"] == "anthropic"
    assert runtime["model"]["source"] == "harness_reported"
    assert runtime["model"]["exact"] is True
    assert runtime["harness"]["type"] == "claude-code"
    assert runtime["harness"]["version"] == "1.0.83"


def test_post_stop_lazy_onboards_when_cache_missing(tmp_path):
    """An un-onboarded Claude turn should get a slot-scoped identity before
    post-stop emits turn_stop. This is the fix for floor-only sessions that
    never called start_session manually."""
    LazyOnboardHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), LazyOnboardHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    slot = "lazy-slot-1234"
    stop_payload = json.dumps({
        "hook_event_name": "Stop",
        "session_id": slot,
        "stop_hook_active": False,
        "last_assistant_message": "Inspected governance status.",
    })

    try:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "UNITARES_SERVER_URL": f"http://127.0.0.1:{port}",
            "UNITARES_CHECKIN_LOG": str(tmp_path / "checkins.log"),
            "UNITARES_AUTO_ONBOARD": "on",
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PWD": str(tmp_path),
        }
        hook = PLUGIN_ROOT / "hooks" / "post-stop"
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

    tool_names = [c.get("name") for c in LazyOnboardHandler.calls]
    assert tool_names == ["onboard", "process_agent_update"]
    assert LazyOnboardHandler.calls[0]["arguments"]["model_type"] == "claude-code"
    assert (
        LazyOnboardHandler.calls[0]["arguments"]["onboard_origin"]
        == "harness_backstop"
    )

    checkin = LazyOnboardHandler.calls[1]["arguments"]
    assert checkin["client_session_id"] == "agent-11111111-222"
    assert checkin["metadata"]["event"] == "turn_stop"
    assert checkin["epistemic_class"] == "substrate_interpretation"
    assert "continuity_token" not in checkin

    cache_path = tmp_path / ".unitares" / _slot_filename(slot)
    cached = json.loads(cache_path.read_text())
    assert cached["uuid"] == "11111111-2222-4333-8444-555555555555"
    assert cached["client_session_id"] == "agent-11111111-222"
    assert "continuity_token" not in cached


def test_lazy_onboard_names_the_session_after_its_launch_directory(tmp_path):
    """The first Stop can fire while the shell is in another directory; the
    label follows where the session started, not where the shell wandered."""
    LazyOnboardHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), LazyOnboardHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    project = tmp_path / "unitares"
    elsewhere = tmp_path / "career"
    project.mkdir()
    elsewhere.mkdir()
    stop_payload = json.dumps({
        "hook_event_name": "Stop",
        "session_id": "label-slot-1234",
        "stop_hook_active": False,
        "last_assistant_message": "Checked a note in another folder.",
    })

    try:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "UNITARES_SERVER_URL": f"http://127.0.0.1:{port}",
            "UNITARES_CHECKIN_LOG": str(tmp_path / "checkins.log"),
            "UNITARES_AUTO_ONBOARD": "on",
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "CLAUDE_PROJECT_DIR": str(project),
            "PWD": str(elsewhere),
        }
        subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop")],
            env=env,
            cwd=str(elsewhere),
            input=stop_payload,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)

    onboard = [c for c in LazyOnboardHandler.calls if c.get("name") == "onboard"]
    assert onboard, LazyOnboardHandler.calls
    name = onboard[0]["arguments"]["name"]
    assert name.split("#", 1)[0] == "claude-unitares"
    assert "career" not in name
    # The vendor session is declared with its launch directory, which is
    # where `claude --resume` must run.
    assert onboard[0]["arguments"]["host_session"] == {
        "harness": "claude-code",
        "session_id": "label-slot-1234",
        "workspace": str(project.resolve()),
    }



def test_codex_host_ignores_an_inherited_claude_project_dir(tmp_path):
    LazyOnboardHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), LazyOnboardHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    claude_dir = tmp_path / "claude-launch"
    codex_dir = tmp_path / "codexwork"
    claude_dir.mkdir()
    codex_dir.mkdir()
    stop_payload = json.dumps({
        "hook_event_name": "Stop",
        "session_id": "codex-label-slot",
        "stop_hook_active": False,
        "last_assistant_message": "Did some work.",
    })
    try:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "UNITARES_SERVER_URL": f"http://127.0.0.1:{port}",
            "UNITARES_CHECKIN_LOG": str(tmp_path / "checkins.log"),
            "UNITARES_AUTO_ONBOARD": "on",
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PLUGIN_ROOT": str(PLUGIN_ROOT),
            "CLAUDE_PROJECT_DIR": str(claude_dir),
            "PWD": str(codex_dir),
        }
        subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", "codex"],
            env=env,
            cwd=str(codex_dir),
            input=stop_payload,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)

    onboard = [c for c in LazyOnboardHandler.calls if c.get("name") == "onboard"]
    assert onboard, LazyOnboardHandler.calls
    assert onboard[0]["arguments"]["name"].split("#", 1)[0] == "codex-codexwork"


def test_agy_post_stop_recovers_transcript_onboard_without_duplicate_mint(tmp_path):
    """When an agy session already called start_session via call_mcp_tool,
    post-stop --host agy recovers that identity from transcript.jsonl, does not
    lazy-onboard a duplicate, posts one turn_stop check-in, and emits byte-exact
    {}\\n on stdout."""
    LazyOnboardHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), LazyOnboardHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    ws = tmp_path / "agy-work"
    ws.mkdir()
    conv_id = "6dafb6a1-3e48-4229-92ce-7b6dc6f5756a"
    brain_dir = tmp_path / "brain" / conv_id
    logs_dir = brain_dir / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    transcript = logs_dir / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"step_index": 0, "type": "USER_INPUT", "content": "analyze"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "tool_calls": [
                        {
                            "name": "call_mcp_tool",
                            "args": {
                                "Arguments": '{"client_hint":"antigravity","force_new":true,"model_type":"gemini-4-argon"}',
                                "ServerName": '"unitares-governance"',
                                "ToolName": '"start_session"',
                            },
                        }
                    ],
                },
                {
                    "step_index": 2,
                    "type": "GENERIC",
                    "status": "DONE",
                    "content": (
                        "Created At: 2026-10-25T12:00:14-06:00\n"
                        '{"success":true,"tool":"start_session",'
                        '"client_session_id":"agent-536907ac-e3b-pxdvtl3cyn3ospu7suu2",'
                        '"agent_uuid":"536907ac-e3b1-4a65-88eb-694bc59e9b02",'
                        '"agent_id":"Antigravity_Gemini_4_Argon_20261025",'
                        '"display_name":"antigravity-gemini-4-argon_536907ac"}'
                    ),
                },
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "content": "Completed the Antigravity analysis.",
                },
            ]
        )
        + "\n"
    )
    stop_payload = json.dumps(
        {
            "conversationId": conv_id,
            "workspacePaths": [str(ws)],
            "artifactDirectoryPath": str(brain_dir),
            "modelName": "gemini-4-argon",
            "terminationReason": "NO_TOOL_CALL",
        }
    )

    try:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "UNITARES_SERVER_URL": f"http://127.0.0.1:{port}",
            "UNITARES_CHECKIN_LOG": str(tmp_path / "checkins.log"),
            "UNITARES_AUTO_ONBOARD": "on",
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PWD": str(tmp_path),
        }
        res = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", "agy"],
            env=env,
            cwd=str(tmp_path),
            input=stop_payload,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)

    assert res.returncode == 0
    assert res.stdout == "{}\n"
    tool_names = [c.get("name") for c in LazyOnboardHandler.calls]
    assert tool_names == ["process_agent_update"]
    checkin = LazyOnboardHandler.calls[0]["arguments"]
    assert checkin["client_session_id"] == "agent-536907ac-e3b-pxdvtl3cyn3ospu7suu2"
    assert "Completed the Antigravity analysis" in checkin["response_text"]
    assert "1 tool calls (call_mcp_tool)" in checkin["response_text"]
    runtime = checkin["provenance_context"]["runtime_provenance"]
    assert runtime["model"]["identifier"] == "gemini-4-argon"
    assert runtime["harness"]["type"] == "antigravity"


def test_agy_post_stop_emits_exact_json_stdout_on_invalid_input(tmp_path):
    res = subprocess.run(
        [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", "agy"],
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
        cwd=str(tmp_path),
        input="{not-json",
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res.returncode == 0
    assert res.stdout == "{}\n"


def test_agy_post_stop_skips_unbound_invoke_subagent_child(tmp_path):
    LazyOnboardHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), LazyOnboardHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    sub_id = "25f6617b-f0fa-4c1a-858a-dcc0496f36ac"
    brain_dir = tmp_path / "brain" / sub_id
    logs_dir = brain_dir / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    transcript = logs_dir / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"step_index": 0, "type": "SYSTEM_MESSAGE", "content": "subagent task"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "content": "Subagent review done.",
                    "tool_calls": [{"name": "read_file"}],
                },
            ]
        )
        + "\n"
    )
    stop_payload = json.dumps(
        {
            "conversationId": sub_id,
            "workspacePaths": [],
            "artifactDirectoryPath": str(brain_dir),
            "modelName": "gemini-4-argon",
        }
    )

    try:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "UNITARES_SERVER_URL": f"http://127.0.0.1:{port}",
            "UNITARES_CHECKIN_LOG": str(tmp_path / "checkins.log"),
            "UNITARES_AUTO_ONBOARD": "on",
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PWD": str(tmp_path),
        }
        res = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", "agy"],
            env=env,
            cwd=str(tmp_path),
            input=stop_payload,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)

    assert res.returncode == 0
    assert res.stdout == "{}\n"
    assert LazyOnboardHandler.calls == []
