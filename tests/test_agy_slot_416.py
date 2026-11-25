"""Regression tests for cirwel/unitares-governance-plugin#416 (Antigravity slot binding).

Covers:
1. hooks/post-stop --host agy --session-start emitting Antigravity injectSteps
   on resolved or newly minted valid bindings, and {} otherwise / in Stop mode.
2. scripts/governance_call_inject.py + hooks/pre-governance-call --host agy
   for Antigravity PreToolUse (call_mcp_tool dict & JSON-string Arguments,
   direct MCP tool names, bound-slot duplicate/bare mint deny, overwrite
   client_session_id injection, fast allow for non-unitares tools/servers).
3. recover_agy_identity_from_transcript protection against the dbd17d25 flip
   (unparented force_new, mismatched parent_agent_id, foreign identity, plus
   legitimate declared lineage switch and >24h idle-binding replacement).
4. hooks/agy-hooks.json manifest schema and commands.
5. scripts/dev/review.sh advisory CLI behavior.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from _session_lookup import _slot_filename, load_usable_session_for_hook  # noqa: E402
from binding_activity import touch as touch_binding_activity  # noqa: E402
from scripts import governance_call_inject, stop_hook_event  # noqa: E402
from tests.test_post_stop_hook import LazyOnboardHandler  # noqa: E402
from tests.test_session_start_checkin import _ReusableTCPServer  # noqa: E402


UUID_A = "d02948ef-1111-4222-8333-444444444444"
CSID_A = "agent-d02948ef-111-backstop"
UUID_B = "5d79c3c6-f417-4840-a426-259a99b60497"
CSID_B = "agent-5d79c3c6-f41-fe77lvjluv7f5xtlx2xn"
UUID_C = "5fc42e7e-2222-4333-8444-555555555555"
CSID_C = "agent-5fc42e7e-222-backstop2"
UUID_D = "9ca4805d-e873-4055-8d6e-75349afe15f2"
CSID_D = "agent-9ca4805d-e87-emxjfz5f7vhfrwca6xux"


def _write_slot_cache(
    home_dir: Path,
    slot: str,
    *,
    uuid: str = UUID_A,
    csid: str = CSID_A,
    onboard_origin: str = "harness_backstop",
    prior_process: bool = False,
    mtime: float | None = None,
) -> Path:
    cache_dir = home_dir / ".unitares"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / _slot_filename(slot)
    cache.write_text(
        json.dumps(
            {
                "uuid": uuid,
                "client_session_id": csid,
                "onboard_origin": onboard_origin,
                "prior_process": prior_process,
                "schema_version": 2,
                "slot": slot,
            }
        ),
        encoding="utf-8",
    )
    cache.chmod(0o600)
    if mtime is not None:
        os.utime(cache, (mtime, mtime))
    return cache


def _write_transcript(path: Path, entries: list[dict]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )
    return str(path)


def _agy_pre_tool_payload(
    conv_id: str,
    *,
    tool_name: str = "call_mcp_tool",
    args: dict | None = None,
    workspace: str = "/tmp/ws",
) -> dict:
    return {
        "conversationId": conv_id,
        "workspacePaths": [workspace],
        "hookEventName": "PreToolUse",
        "stepIdx": 5,
        "toolCall": {
            "id": "toolu_agy_01",
            "name": tool_name,
            "args": {} if args is None else args,
        },
    }


def _run_inject_agy(
    payload: dict | str,
    *,
    home_dir: Path,
    extra_env: dict[str, str] | None = None,
    via_hook: bool = False,
) -> dict:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(home_dir),
        "PLUGIN_ROOT": str(PLUGIN_ROOT),
        **(extra_env or {}),
    }
    cmd = (
        [str(PLUGIN_ROOT / "hooks" / "pre-governance-call"), "--host", "agy"]
        if via_hook
        else [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "governance_call_inject.py"),
            "--host",
            "agy",
        ]
    )
    res = subprocess.run(
        cmd,
        input=raw,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res.returncode == 0, f"stderr={res.stderr!r}"
    return json.loads(res.stdout)


# ---------------------------------------------------------------------------
# 1. hooks/post-stop --host agy --session-start
# ---------------------------------------------------------------------------


def test_agy_session_start_emits_inject_steps_on_resolved_and_minted_bindings(tmp_path):
    LazyOnboardHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), LazyOnboardHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    home_dir = tmp_path / "home"
    home_dir.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()

    base_env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(home_dir),
        "UNITARES_SERVER_URL": f"http://127.0.0.1:{port}",
        "UNITARES_CHECKIN_LOG": str(tmp_path / "checkins.log"),
        "UNITARES_AUTO_ONBOARD": "on",
        "PLUGIN_ROOT": str(PLUGIN_ROOT),
        "PWD": str(ws),
    }

    try:
        # (a) Newly minted orchestrated SessionStart emits injectSteps only (no trailing {}).
        orch_cid = "11111111-aaaa-4bbb-8ccc-000000000001"
        orch_env = {
            **base_env,
            "UNITARES_CLIENT_SESSION_ID": "agent:/thread-416",
            "UNITARES_ORCHESTRATED": "1",
            "UNITARES_ORCHESTRATED_OWNER_PID": "91919",
            "UNITARES_HOOK_HOST_PID": "91919",
        }
        res_orch = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", "agy", "--session-start"],
            env=orch_env,
            cwd=str(ws),
            input=json.dumps({"conversationId": orch_cid, "workspacePaths": [str(ws)]}),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert res_orch.returncode == 0
        assert len(res_orch.stdout.strip().splitlines()) == 1
        orch_json = json.loads(res_orch.stdout)
        msg = orch_json["injectSteps"][0]["systemMessage"]["systemMessage"]
        assert "11111111-2222-4333-8444-555555555555" in msg
        assert "agent-11111111-222" in msg
        assert "start_session(force_new=true)" in msg
        assert "parent_agent_id" in msg

        # (b) Already-bound interactive slot resolves existing binding without minting.
        LazyOnboardHandler.calls = []
        bound_cid = "22222222-aaaa-4bbb-8ccc-000000000002"
        _write_slot_cache(home_dir, bound_cid, uuid=UUID_A, csid=CSID_A)
        res_bound = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", "agy", "--session-start"],
            env=base_env,
            cwd=str(ws),
            input=json.dumps({"conversationId": bound_cid, "workspacePaths": [str(ws)]}),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert res_bound.returncode == 0
        assert LazyOnboardHandler.calls == []
        bound_json = json.loads(res_bound.stdout)
        bound_msg = bound_json["injectSteps"][0]["systemMessage"]["systemMessage"]
        assert UUID_A in bound_msg
        assert CSID_A in bound_msg

        # (c) Unbound interactive slot (even when another slot's cache exists) stays {}.
        unbound_cid = "33333333-aaaa-4bbb-8ccc-000000000003"
        res_unbound = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", "agy", "--session-start"],
            env=base_env,
            cwd=str(ws),
            input=json.dumps({"conversationId": unbound_cid, "workspacePaths": [str(ws)]}),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert res_unbound.returncode == 0
        assert res_unbound.stdout == "{}\n"
        assert LazyOnboardHandler.calls == []

        # (d) Malformed CSID in cache falls back to {} instead of breaking JSON.
        bad_cid = "44444444-aaaa-4bbb-8ccc-000000000004"
        _write_slot_cache(home_dir, bad_cid, uuid=UUID_A, csid='bad "quoted" csid')
        res_bad = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", "agy", "--session-start"],
            env=base_env,
            cwd=str(ws),
            input=json.dumps({"conversationId": bad_cid, "workspacePaths": [str(ws)]}),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert res_bad.returncode == 0
        assert res_bad.stdout == "{}\n"

        # (e) Idle (>24h) slot at SessionStart is marked prior_process and emits {}.
        idle_cid = "55555555-aaaa-4bbb-8ccc-000000000005"
        old_mtime = time.time() - 30 * 3600
        _write_slot_cache(home_dir, idle_cid, uuid=UUID_A, csid=CSID_A, mtime=old_mtime)
        res_idle = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", "agy", "--session-start"],
            env=base_env,
            cwd=str(ws),
            input=json.dumps({"conversationId": idle_cid, "workspacePaths": [str(ws)]}),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert res_idle.returncode == 0
        assert res_idle.stdout == "{}\n"

        # (f) Stop mode (no --session-start) ALWAYS emits {} even on a bound slot.
        res_stop = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", "agy"],
            env=base_env,
            cwd=str(ws),
            input=json.dumps(
                {
                    "conversationId": bound_cid,
                    "workspacePaths": [str(ws)],
                    "last_assistant_message": "Finished step.",
                }
            ),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert res_stop.returncode == 0
        assert res_stop.stdout == "{}\n"
    finally:
        srv.shutdown()
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# 2. scripts/governance_call_inject.py + hooks/pre-governance-call --host agy
# ---------------------------------------------------------------------------


def test_agy_pre_tool_use_fast_allows_non_unitares_tools_and_servers(tmp_path):
    home_dir = tmp_path / "home"
    conv_id = "agy-conv-fast-allow"
    _write_slot_cache(home_dir, conv_id)

    for payload in (
        _agy_pre_tool_payload(conv_id, tool_name="run_command", args={"CommandLine": "ls"}),
        _agy_pre_tool_payload(
            conv_id,
            tool_name="call_mcp_tool",
            args={"ServerName": "anima", "ToolName": "get_state", "Arguments": {}},
        ),
        _agy_pre_tool_payload(
            conv_id,
            tool_name="call_mcp_tool",
            args={
                "ServerName": "unitares-governance-shadow",
                "ToolName": "start_session",
                "Arguments": {"force_new": True},
            },
        ),
    ):
        out = _run_inject_agy(payload, home_dir=home_dir, via_hook=True)
        assert out == {"decision": "allow"}


def test_agy_pre_tool_use_denies_duplicate_and_bare_mints_on_bound_slot(tmp_path):
    home_dir = tmp_path / "home"
    conv_id = "agy-conv-bound-deny"
    _write_slot_cache(home_dir, conv_id, uuid=UUID_A, csid=CSID_A)

    # force_new=true with dict Arguments -> deny
    out_dict = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            args={
                "ServerName": "unitares-governance",
                "ToolName": "start_session",
                "Arguments": {"force_new": True, "model_type": "gemini-4-argon"},
            },
        ),
        home_dir=home_dir,
        via_hook=True,
    )
    assert out_dict["decision"] == "deny"
    assert UUID_A in out_dict["reason"]
    assert CSID_A in out_dict["reason"]

    # force_new=true with JSON-string Arguments -> deny
    out_str = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            args={
                "ServerName": "unitares-governance",
                "ToolName": "onboard",
                "Arguments": json.dumps({"force_new": True}),
            },
        ),
        home_dir=home_dir,
    )
    assert out_str["decision"] == "deny"

    # Bare mint (no force_new, no proof) -> deny
    out_bare = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            args={
                "ServerName": "unitares-governance",
                "ToolName": "start_session",
                "Arguments": {"client_hint": "antigravity"},
            },
        ),
        home_dir=home_dir,
    )
    assert out_bare["decision"] == "deny"

    # Partial lineage (only parent_agent_id OR only spawn_reason) -> deny
    for partial_args in (
        {"force_new": True, "parent_agent_id": UUID_A},
        {"force_new": True, "spawn_reason": "explicit"},
        {"force_new": True, "parent_agent_id": UUID_A, "spawn_reason": "new_session"},
    ):
        out_partial = _run_inject_agy(
            _agy_pre_tool_payload(
                conv_id,
                args={
                    "ServerName": "unitares-governance",
                    "ToolName": "start_session",
                    "Arguments": partial_args,
                },
            ),
            home_dir=home_dir,
        )
        assert out_partial["decision"] == "deny", f"expected deny for {partial_args}"

    # use_tool gateway wrapping start_session(force_new=true) -> deny
    out_gw = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            args={
                "ServerName": "unitares-governance",
                "ToolName": "use_tool",
                "Arguments": {"tool_name": "start_session", "arguments": {"force_new": True}},
            },
        ),
        home_dir=home_dir,
    )
    assert out_gw["decision"] == "deny"

    # Explicit lineage with BOTH parent_agent_id and deliberate spawn_reason -> allow
    out_lineage = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            args={
                "ServerName": "unitares-governance",
                "ToolName": "start_session",
                "Arguments": {
                    "force_new": True,
                    "parent_agent_id": UUID_A,
                    "spawn_reason": "explicit",
                },
            },
        ),
        home_dir=home_dir,
    )
    assert out_lineage == {"decision": "allow"}

    # Declared subagent mint -> allow
    out_sub = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            args={
                "ServerName": "unitares-governance",
                "ToolName": "start_session",
                "Arguments": {"force_new": True, "spawn_reason": "subagent"},
            },
        ),
        home_dir=home_dir,
    )
    assert out_sub == {"decision": "allow"}


def test_agy_pre_tool_use_injects_csid_preserving_arguments_type_and_denies_foreign_proof(
    tmp_path,
):
    home_dir = tmp_path / "home"
    conv_id = "agy-conv-inject"
    _write_slot_cache(home_dir, conv_id, uuid=UUID_A, csid=CSID_A)

    # Dict Arguments -> overwrite.Arguments is dict with injected client_session_id
    out_dict = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            args={
                "ServerName": "unitares-governance",
                "ToolName": "sync_state",
                "Arguments": {"response_text": "Milestone reached.", "complexity": 0.4},
            },
        ),
        home_dir=home_dir,
        via_hook=True,
    )
    assert out_dict["decision"] == "allow"
    assert isinstance(out_dict["overwrite"]["Arguments"], dict)
    assert out_dict["overwrite"]["Arguments"]["client_session_id"] == CSID_A
    assert out_dict["overwrite"]["Arguments"]["response_text"] == "Milestone reached."

    # JSON-string Arguments -> overwrite.Arguments stays a JSON string
    out_str = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            args={
                "ServerName": "unitares-governance",
                "ToolName": "store_finding",
                "Arguments": json.dumps({"summary": "Bug fixed", "tags": ["Postgres"]}),
            },
        ),
        home_dir=home_dir,
    )
    assert out_str["decision"] == "allow"
    assert isinstance(out_str["overwrite"]["Arguments"], str)
    decoded = json.loads(out_str["overwrite"]["Arguments"])
    assert decoded["client_session_id"] == CSID_A
    assert decoded["summary"] == "Bug fixed"

    # Direct MCP tool name (mcp__unitares-governance__sync_state & mcp_unitares-governance_sync_state)
    for direct_name in (
        "mcp__unitares-governance__sync_state",
        "mcp_unitares-governance_sync_state",
    ):
        out_direct = _run_inject_agy(
            _agy_pre_tool_payload(
                conv_id,
                tool_name=direct_name,
                args={"response_text": "Direct call", "complexity": 0.3},
            ),
            home_dir=home_dir,
        )
        assert out_direct["decision"] == "allow"
        assert out_direct["overwrite"]["client_session_id"] == CSID_A
        assert out_direct["overwrite"]["response_text"] == "Direct call"

    # Foreign client_session_id conflicting with active slot -> deny
    out_foreign = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            args={
                "ServerName": "unitares-governance",
                "ToolName": "sync_state",
                "Arguments": {"client_session_id": CSID_B, "response_text": "stale id"},
            },
        ),
        home_dir=home_dir,
    )
    assert out_foreign["decision"] == "deny"

    # Matching client_session_id -> plain allow (no overwrite needed)
    out_match = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            args={
                "ServerName": "unitares-governance",
                "ToolName": "sync_state",
                "Arguments": {"client_session_id": CSID_A, "response_text": "ok"},
            },
        ),
        home_dir=home_dir,
    )
    assert out_match == {"decision": "allow"}


def test_agy_pre_tool_use_allows_mint_on_prior_process_idle_or_unbound_and_fails_open(tmp_path):
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    # Unbound slot -> allow start_session(force_new=true)
    out_unbound = _run_inject_agy(
        _agy_pre_tool_payload(
            "slot-unbound",
            args={
                "ServerName": "unitares-governance",
                "ToolName": "start_session",
                "Arguments": {"force_new": True},
            },
        ),
        home_dir=home_dir,
    )
    assert out_unbound == {"decision": "allow"}

    # prior_process=True slot -> allow start_session(force_new=true) and do not inject CSID
    _write_slot_cache(home_dir, "slot-prior", prior_process=True)
    assert _run_inject_agy(
        _agy_pre_tool_payload(
            "slot-prior",
            args={
                "ServerName": "unitares-governance",
                "ToolName": "start_session",
                "Arguments": {"force_new": True},
            },
        ),
        home_dir=home_dir,
    ) == {"decision": "allow"}
    assert _run_inject_agy(
        _agy_pre_tool_payload(
            "slot-prior",
            args={
                "ServerName": "unitares-governance",
                "ToolName": "sync_state",
                "Arguments": {"response_text": "hi"},
            },
        ),
        home_dir=home_dir,
    ) == {"decision": "allow"}

    # Idle (>24h) slot -> allow start_session(force_new=true) and do not inject expired CSID
    _write_slot_cache(home_dir, "slot-idle", mtime=time.time() - 30 * 3600)
    assert _run_inject_agy(
        _agy_pre_tool_payload(
            "slot-idle",
            args={
                "ServerName": "unitares-governance",
                "ToolName": "start_session",
                "Arguments": {"force_new": True},
            },
        ),
        home_dir=home_dir,
    ) == {"decision": "allow"}
    assert _run_inject_agy(
        _agy_pre_tool_payload(
            "slot-idle",
            args={
                "ServerName": "unitares-governance",
                "ToolName": "sync_state",
                "Arguments": {"response_text": "after idle"},
            },
        ),
        home_dir=home_dir,
    ) == {"decision": "allow"}

    # Malformed / empty stdin through shell hook fails open with valid allow JSON
    for raw in ("", "not-json", "[]"):
        assert _run_inject_agy(raw, home_dir=home_dir, via_hook=True) == {"decision": "allow"}


# ---------------------------------------------------------------------------
# 3. dbd17d25 flip regression in recover_agy_identity_from_transcript
# ---------------------------------------------------------------------------


def test_dbd17d25_regression_live_slot_rejects_unparented_and_mismatched_switches(
    tmp_path, monkeypatch
):
    """Reproduces the dbd17d25 flip sequence from issue #416:
    - Switch 1: live backstop d02948ef must NOT be overwritten by unparented
      start_session(force_new=true) minting 5d79c3c6.
    - Switch 3: live backstop 5fc42e7e must NOT be overwritten by a compaction
      mint naming stale 5d79c3c6 as parent_agent_id instead of active 5fc42e7e.
    - Legitimate declared lineage switch naming the active UUID as parent DOES commit.
    - Idle (>24h) slot replacement in the resumed turn DOES commit and refresh activity.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    ws = tmp_path / "ws"
    ws.mkdir()

    slot = "dbd17d25-2800-46b3-a743-70f06358658b"
    transcript_path = tmp_path / "transcript.jsonl"
    payload = {"conversationId": slot, "transcriptPath": str(transcript_path)}

    # Switch 1 scenario: live harness_backstop (UUID_A), turn has unparented start_session -> UUID_B.
    _write_slot_cache(fake_home, slot, uuid=UUID_A, csid=CSID_A, onboard_origin="harness_backstop")
    _write_transcript(
        transcript_path,
        [
            {"step_index": 0, "type": "USER_INPUT", "content": "turn 3"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [
                    {
                        "name": "call_mcp_tool",
                        "args": {
                            "ServerName": '"unitares-governance"',
                            "ToolName": '"start_session"',
                            "Arguments": json.dumps(
                                {"force_new": True, "name": "antigravity-crux-brief"}
                            ),
                        },
                    }
                ],
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "DONE",
                "content": json.dumps(
                    {
                        "success": True,
                        "tool": "start_session",
                        "agent_uuid": UUID_B,
                        "client_session_id": CSID_B,
                        "is_new": True,
                    }
                ),
            },
        ],
    )
    assert (
        stop_hook_event.recover_agy_identity_from_transcript(payload, workspace=ws, slot=slot)
        is False
    )
    bound = load_usable_session_for_hook(ws, json.dumps(payload))
    assert bound["uuid"] == UUID_A
    assert bound["client_session_id"] == CSID_A

    # Switch 3 scenario: live backstop (UUID_C), compaction mint names stale UUID_B as parent -> UUID_D.
    _write_slot_cache(fake_home, slot, uuid=UUID_C, csid=CSID_C, onboard_origin="harness_backstop")
    _write_transcript(
        transcript_path,
        [
            {"step_index": 10, "type": "USER_INPUT", "content": "wrap up"},
            {
                "step_index": 11,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [
                    {
                        "name": "call_mcp_tool",
                        "args": {
                            "ServerName": '"unitares-governance"',
                            "ToolName": '"start_session"',
                            "Arguments": json.dumps(
                                {
                                    "force_new": True,
                                    "parent_agent_id": UUID_B,
                                    "spawn_reason": "compaction",
                                }
                            ),
                        },
                    }
                ],
            },
            {
                "step_index": 12,
                "type": "GENERIC",
                "status": "DONE",
                "content": json.dumps(
                    {
                        "success": True,
                        "tool": "start_session",
                        "agent_uuid": UUID_D,
                        "client_session_id": CSID_D,
                        "is_new": True,
                    }
                ),
            },
        ],
    )
    assert (
        stop_hook_event.recover_agy_identity_from_transcript(payload, workspace=ws, slot=slot)
        is False
    )
    bound_after_mismatch = load_usable_session_for_hook(ws, json.dumps(payload))
    assert bound_after_mismatch["uuid"] == UUID_C
    assert bound_after_mismatch["client_session_id"] == CSID_C

    # Declared lineage switch naming the active slot UUID_C as parent_agent_id -> COMMITS UUID_D.
    _write_transcript(
        transcript_path,
        [
            {"step_index": 20, "type": "USER_INPUT", "content": "deliberate handoff"},
            {
                "step_index": 21,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [
                    {
                        "name": "call_mcp_tool",
                        "args": {
                            "ServerName": '"unitares-governance"',
                            "ToolName": '"start_session"',
                            "Arguments": json.dumps(
                                {
                                    "force_new": True,
                                    "parent_agent_id": UUID_C,
                                    "spawn_reason": "explicit",
                                }
                            ),
                        },
                    }
                ],
            },
            {
                "step_index": 22,
                "type": "GENERIC",
                "status": "DONE",
                "content": json.dumps(
                    {
                        "success": True,
                        "tool": "start_session",
                        "agent_uuid": UUID_D,
                        "client_session_id": CSID_D,
                        "is_new": True,
                    }
                ),
            },
        ],
    )
    assert (
        stop_hook_event.recover_agy_identity_from_transcript(payload, workspace=ws, slot=slot)
        is True
    )
    bound_switched = load_usable_session_for_hook(ws, json.dumps(payload))
    assert bound_switched["uuid"] == UUID_D
    assert bound_switched["client_session_id"] == CSID_D

    # Idle (>24h) slot not yet marked prior_process=True -> new-turn start_session replaces it cleanly.
    old_ts = time.time() - 40 * 3600
    _write_slot_cache(fake_home, slot, uuid=UUID_B, csid=CSID_B, onboard_origin="agent", mtime=old_ts)
    touch_binding_activity(slot, UUID_B, CSID_B, at=old_ts)
    _write_transcript(
        transcript_path,
        [
            {"step_index": 30, "type": "USER_INPUT", "content": "resumed after 40h"},
            {
                "step_index": 31,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [
                    {
                        "name": "call_mcp_tool",
                        "args": {
                            "ServerName": '"unitares-governance"',
                            "ToolName": '"start_session"',
                            "Arguments": json.dumps({"force_new": True}),
                        },
                    }
                ],
            },
            {
                "step_index": 32,
                "type": "GENERIC",
                "status": "DONE",
                "content": json.dumps(
                    {
                        "success": True,
                        "tool": "start_session",
                        "agent_uuid": UUID_D,
                        "client_session_id": CSID_D,
                        "is_new": True,
                    }
                ),
            },
        ],
    )
    assert (
        stop_hook_event.recover_agy_identity_from_transcript(payload, workspace=ws, slot=slot)
        is True
    )
    bound_after_idle = load_usable_session_for_hook(ws, json.dumps(payload))
    assert bound_after_idle["uuid"] == UUID_D
    assert bound_after_idle["client_session_id"] == CSID_D


# ---------------------------------------------------------------------------
# 4. hooks/agy-hooks.json contract
# ---------------------------------------------------------------------------


def test_agy_hooks_manifest_matches_antigravity_schema_and_avoids_local_paths():
    manifest_path = PLUGIN_ROOT / "hooks" / "agy-hooks.json"
    raw = manifest_path.read_text(encoding="utf-8")
    assert "/Users/" not in raw

    data = json.loads(raw)
    assert "unitares-governance" in data
    entry = data["unitares-governance"]

    # SessionStart is flat list of handler objects
    assert isinstance(entry["SessionStart"], list) and len(entry["SessionStart"]) == 1
    assert "post-stop --host agy --session-start" in entry["SessionStart"][0]["command"]

    # PreToolUse is grouped under the anchored matcher the helper derives
    assert isinstance(entry["PreToolUse"], list) and len(entry["PreToolUse"]) == 1
    pre = entry["PreToolUse"][0]
    from governance_call_inject import agy_pre_tool_use_matcher

    assert pre["matcher"] == agy_pre_tool_use_matcher()
    assert "pre-governance-call --host agy" in pre["hooks"][0]["command"]

    # Stop is flat list of handler objects
    assert isinstance(entry["Stop"], list) and len(entry["Stop"]) == 1
    assert "post-stop --host agy" in entry["Stop"][0]["command"]
    assert "--session-start" not in entry["Stop"][0]["command"]


# ---------------------------------------------------------------------------
# 5. scripts/dev/review.sh advisory helper
# ---------------------------------------------------------------------------


def test_advisory_review_script_help_dry_run_and_cli_invocation(tmp_path):
    script = PLUGIN_ROOT / "scripts" / "dev" / "review.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK)

    res_help = subprocess.run(
        [str(script), "--help"],
        cwd=str(PLUGIN_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res_help.returncode == 0
    assert "--reviewer" in res_help.stdout

    res_dry = subprocess.run(
        [str(script), "--dry-run", "--base", "HEAD", "--reviewer", "claude"],
        cwd=str(PLUGIN_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res_dry.returncode == 0
    assert "base: HEAD" in res_dry.stdout
    assert "reviewer: claude" in res_dry.stdout

    # Missing reviewer exits 2 without invoking any CLI
    res_no_rev = subprocess.run(
        [str(script), "--base", "HEAD"],
        cwd=str(PLUGIN_ROOT),
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res_no_rev.returncode == 2
    assert "no reviewer selected" in res_no_rev.stderr

    # Shim claude CLI in a tiny temp git repo with a real diff against master
    repo = tmp_path / "repo"
    (repo / "scripts" / "dev").mkdir(parents=True)
    (repo / "scripts" / "dev" / "review.sh").write_text(
        script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo / "scripts" / "dev" / "review.sh").chmod(0o755)
    for git_args in (
        ["init", "-b", "master"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.com"],
    ):
        subprocess.run(["git", "-C", str(repo), *git_args], check=True, capture_output=True)
    (repo / "hello.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "feat/test"], check=True, capture_output=True
    )
    (repo / "hello.txt").write_text("v2\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-am", "change"], check=True, capture_output=True
    )

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    capture_file = tmp_path / "captured.txt"
    shim = shim_dir / "claude"
    argv_file = tmp_path / "argv.txt"
    shim.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {str(argv_file)!r}\ncat > {str(capture_file)!r}\necho 'CLEAN'\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    res_run = subprocess.run(
        [str(repo / "scripts" / "dev" / "review.sh"), "--reviewer", "claude", "--base", "master"],
        cwd=str(repo),
        env={"PATH": f"{shim_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_run.returncode == 0
    assert "CLEAN" in res_run.stdout
    captured = capture_file.read_text(encoding="utf-8")
    assert "+v2" in captured
    claude_argv = argv_file.read_text(encoding="utf-8").splitlines()
    assert "--permission-mode" in claude_argv and "plan" in claude_argv
    assert "--tools" in claude_argv and "Read,Grep,Glob" in claude_argv
    assert "--strict-mcp-config" in claude_argv
    assert "--mcp-config" in claude_argv and '{"mcpServers":{}}' in claude_argv
    assert "--disallowedTools" in claude_argv and "mcp__*" in claude_argv

    # Verify codex and agy shims receive read-only enforcement flags
    codex_shim = shim_dir / "codex"
    codex_argv_file = tmp_path / "codex_argv.txt"
    codex_shim.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {str(codex_argv_file)!r}\ncat >/dev/null\necho 'CLEAN'\n",
        encoding="utf-8",
    )
    codex_shim.chmod(0o755)
    res_codex = subprocess.run(
        [str(repo / "scripts" / "dev" / "review.sh"), "--reviewer", "codex", "--base", "master"],
        cwd=str(repo),
        env={"PATH": f"{shim_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_codex.returncode == 0
    codex_argv = codex_argv_file.read_text(encoding="utf-8").splitlines()
    for tok in ("exec", "-s", "read-only"):
        assert tok in codex_argv

    # agy shim with --model + --sandbox (NO --mode!) must fail closed with exit 2
    agy_shim = shim_dir / "agy"
    agy_shim.write_text(
        "#!/usr/bin/env bash\nif [[ \"${1:-}\" == \"--help\" ]]; then echo 'flags: --model --sandbox'; exit 0; fi\necho 'SHOULD_NOT_RUN'\n",
        encoding="utf-8",
    )
    agy_shim.chmod(0o755)
    res_agy_unsafe = subprocess.run(
        [
            str(repo / "scripts" / "dev" / "review.sh"),
            "--reviewer",
            "antigravity",
            "--base",
            "master",
        ],
        cwd=str(repo),
        env={"PATH": f"{shim_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_agy_unsafe.returncode == 2
    assert "SHOULD_NOT_RUN" not in res_agy_unsafe.stdout

    # agy shim advertising --mode + --sandbox runs with full read-only flags
    agy_argv_file = tmp_path / "agy_argv.txt"
    agy_shim.write_text(
        f"#!/usr/bin/env bash\nif [[ \"${{1:-}}\" == \"--help\" ]]; then echo 'flags: --mode --sandbox'; exit 0; fi\nprintf '%s\\n' \"$@\" > {str(agy_argv_file)!r}\ncat >/dev/null\necho 'CLEAN'\n",
        encoding="utf-8",
    )
    res_agy_safe = subprocess.run(
        [
            str(repo / "scripts" / "dev" / "review.sh"),
            "--reviewer",
            "antigravity",
            "--base",
            "master",
        ],
        cwd=str(repo),
        env={"PATH": f"{shim_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_agy_safe.returncode == 0
    agy_argv = agy_argv_file.read_text(encoding="utf-8").splitlines()
    for tok in ("-p", "--mode", "plan", "--sandbox", "--disable-slash-commands"):
        assert tok in agy_argv

    # gemini shim lacking read-only flags in --help fails closed with exit 2
    gem_unsafe = shim_dir / "gemini"
    gem_unsafe.write_text(
        "#!/usr/bin/env bash\nif [[ \"${1:-}\" == \"--help\" ]]; then echo 'usage: gemini -p PROMPT'; exit 0; fi\necho 'SHOULD_NOT_RUN'\n",
        encoding="utf-8",
    )
    gem_unsafe.chmod(0o755)
    res_gem_unsafe = subprocess.run(
        [str(repo / "scripts" / "dev" / "review.sh"), "--reviewer", "gemini", "--base", "master"],
        cwd=str(repo),
        env={"PATH": f"{shim_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_gem_unsafe.returncode == 2
    assert "SHOULD_NOT_RUN" not in res_gem_unsafe.stdout
    assert "read-only" in res_gem_unsafe.stderr

    # gemini shim advertising --approval-mode + --sandbox runs with both enforced
    gem_argv_file = tmp_path / "gemini_argv.txt"
    gem_unsafe.write_text(
        f"#!/usr/bin/env bash\nif [[ \"${{1:-}}\" == \"--help\" ]]; then echo 'flags: --approval-mode --sandbox'; exit 0; fi\nprintf '%s\\n' \"$@\" > {str(gem_argv_file)!r}\ncat >/dev/null\necho 'CLEAN'\n",
        encoding="utf-8",
    )
    res_gem_safe = subprocess.run(
        [str(repo / "scripts" / "dev" / "review.sh"), "--reviewer", "gemini", "--base", "master"],
        cwd=str(repo),
        env={"PATH": f"{shim_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_gem_safe.returncode == 0
    gem_argv = gem_argv_file.read_text(encoding="utf-8").splitlines()
    assert "--approval-mode" in gem_argv and "plan" in gem_argv and "--sandbox" in gem_argv


# ---------------------------------------------------------------------------
# 6. Codex review hardening regressions (PR #434)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lineage_state", ["rejected_coincidental", "rejected_cross_role"])
def test_recover_refuses_live_slot_overwrite_when_lineage_disposition_rejected(
    tmp_path, monkeypatch, lineage_state
):
    """A server-rejected lineage switch must not clobber the active live slot binding."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    ws = tmp_path / "ws"
    ws.mkdir()

    slot = f"slot-rejected-{lineage_state}"
    _write_slot_cache(fake_home, slot, uuid=UUID_C, csid=CSID_C, onboard_origin="agent")
    transcript_path = tmp_path / f"{slot}.jsonl"
    _write_transcript(
        transcript_path,
        [
            {"step_index": 0, "type": "USER_INPUT", "content": "switch"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [
                    {
                        "name": "call_mcp_tool",
                        "args": {
                            "ServerName": '"unitares-governance"',
                            "ToolName": '"start_session"',
                            "Arguments": json.dumps(
                                {
                                    "force_new": True,
                                    "parent_agent_id": UUID_C,
                                    "spawn_reason": "explicit",
                                }
                            ),
                        },
                    }
                ],
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "DONE",
                "content": json.dumps(
                    {
                        "success": True,
                        "tool": "start_session",
                        "agent_uuid": UUID_D,
                        "client_session_id": CSID_D,
                        "lineage_state": lineage_state,
                        "is_new": True,
                    }
                ),
            },
        ],
    )
    payload = {"conversationId": slot, "transcriptPath": str(transcript_path)}
    assert (
        stop_hook_event.recover_agy_identity_from_transcript(payload, workspace=ws, slot=slot)
        is False
    )
    bound = load_usable_session_for_hook(ws, json.dumps(payload))
    assert bound["uuid"] == UUID_C
    assert bound["client_session_id"] == CSID_C


def test_same_turn_double_unparented_mint_blocked_in_pretooluse_and_first_wins_in_recovery(
    tmp_path, monkeypatch
):
    """On an initially unbound slot:
    - Second unparented start_session in the same turn is denied in PreToolUse
      (both via pending-turn marker before flush and via transcript inspection).
    - If two unparented mints appear in an unbound turn's transcript, recovery
      adopts the FIRST valid mint rather than letting the duplicate override it.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    ws = tmp_path / "ws"
    ws.mkdir()

    slot = "slot-double-mint-turn1"
    call1 = _agy_pre_tool_payload(
        slot,
        args={
            "ServerName": "unitares-governance",
            "ToolName": "start_session",
            "Arguments": {"force_new": True},
        },
    )
    call1["toolCall"]["id"] = "toolu_first"
    call2 = _agy_pre_tool_payload(
        slot,
        args={
            "ServerName": "unitares-governance",
            "ToolName": "start_session",
            "Arguments": {"force_new": True},
        },
    )
    call2["toolCall"]["id"] = "toolu_second"

    # First unparented mint on unbound slot passes; second in same turn is denied.
    assert _run_inject_agy(call1, home_dir=fake_home) == {"decision": "allow"}
    out2 = _run_inject_agy(call2, home_dir=fake_home)
    assert out2["decision"] == "deny"
    assert "already in progress" in out2["reason"]

    # Also test transcript-based detection on a fresh unbound slot when call 1 already flushed.
    slot_tx = "slot-double-mint-tx"
    tx_path = tmp_path / "double_mint.jsonl"
    _write_transcript(
        tx_path,
        [
            {"step_index": 0, "type": "USER_INPUT", "content": "turn 1"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [
                    {
                        "name": "call_mcp_tool",
                        "args": {
                            "ServerName": '"unitares-governance"',
                            "ToolName": '"start_session"',
                            "Arguments": json.dumps({"force_new": True}),
                        },
                    }
                ],
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "DONE",
                "content": json.dumps(
                    {
                        "success": True,
                        "tool": "start_session",
                        "agent_uuid": UUID_A,
                        "client_session_id": CSID_A,
                        "is_new": True,
                    }
                ),
            },
        ],
    )
    call_after_tx = _agy_pre_tool_payload(
        slot_tx,
        args={
            "ServerName": "unitares-governance",
            "ToolName": "start_session",
            "Arguments": {"force_new": True},
        },
    )
    call_after_tx["transcriptPath"] = str(tx_path)
    out_tx = _run_inject_agy(call_after_tx, home_dir=fake_home)
    assert out_tx["decision"] == "deny"
    assert UUID_A in out_tx["reason"]
    assert CSID_A in out_tx["reason"]

    # Recovery encountering two unparented mints on an unbound slot keeps the FIRST (UUID_A).
    tx_both = tmp_path / "both_mints.jsonl"
    _write_transcript(
        tx_both,
        [
            {"step_index": 0, "type": "USER_INPUT", "content": "turn 1"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [
                    {
                        "name": "call_mcp_tool",
                        "args": {
                            "ServerName": '"unitares-governance"',
                            "ToolName": '"start_session"',
                            "Arguments": json.dumps({"force_new": True}),
                        },
                    },
                    {
                        "name": "call_mcp_tool",
                        "args": {
                            "ServerName": '"unitares-governance"',
                            "ToolName": '"start_session"',
                            "Arguments": json.dumps({"force_new": True}),
                        },
                    },
                ],
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "DONE",
                "content": json.dumps(
                    {
                        "success": True,
                        "tool": "start_session",
                        "agent_uuid": UUID_A,
                        "client_session_id": CSID_A,
                        "is_new": True,
                    }
                ),
            },
            {
                "step_index": 3,
                "type": "GENERIC",
                "status": "DONE",
                "content": json.dumps(
                    {
                        "success": True,
                        "tool": "start_session",
                        "agent_uuid": UUID_B,
                        "client_session_id": CSID_B,
                        "is_new": True,
                    }
                ),
            },
        ],
    )
    rec_payload = {"conversationId": slot_tx, "transcriptPath": str(tx_both)}
    assert (
        stop_hook_event.recover_agy_identity_from_transcript(
            rec_payload, workspace=ws, slot=slot_tx
        )
        is True
    )
    bound = load_usable_session_for_hook(ws, json.dumps(rec_payload))
    assert bound["uuid"] == UUID_A
    assert bound["client_session_id"] == CSID_A


def test_agy_use_tool_gateway_handles_orchestrated_anchor_and_bare_mint_guards(tmp_path):
    """use_tool(tool_name='start_session'|'onboard') must match direct mint rules."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    # (a) Unbound orchestrated session calling bare start_session via use_tool gets anchor injected.
    orch_env = {
        "UNITARES_ORCHESTRATED": "1",
        "UNITARES_CLIENT_SESSION_ID": "agent:/thread-gw-416",
        "UNITARES_ORCHESTRATED_OWNER_PID": "77777",
        "UNITARES_HOOK_HOST_PID": "77777",
    }
    out_dict = _run_inject_agy(
        _agy_pre_tool_payload(
            "slot-gw-orch-dict",
            args={
                "ServerName": "unitares-governance",
                "ToolName": "use_tool",
                "Arguments": {"tool_name": "start_session", "arguments": {}},
            },
        ),
        home_dir=home_dir,
        extra_env=orch_env,
    )
    assert out_dict["decision"] == "allow"
    inner_out = out_dict["overwrite"]["Arguments"]["arguments"]
    assert inner_out["client_session_id"] == "agent:/thread-gw-416"
    assert inner_out["orchestrated"] is True

    # JSON-string nested arguments preserves string type on overwrite.
    out_str = _run_inject_agy(
        _agy_pre_tool_payload(
            "slot-gw-orch-str",
            args={
                "ServerName": "unitares-governance",
                "ToolName": "use_tool",
                "Arguments": {
                    "tool_name": "onboard",
                    "arguments": json.dumps({"model_type": "gemini-4-argon"}),
                },
            },
        ),
        home_dir=home_dir,
        extra_env=orch_env,
    )
    assert out_str["decision"] == "allow"
    assert isinstance(out_str["overwrite"]["Arguments"]["arguments"], str)
    decoded_inner = json.loads(out_str["overwrite"]["Arguments"]["arguments"])
    assert decoded_inner["client_session_id"] == "agent:/thread-gw-416"
    assert decoded_inner["orchestrated"] is True

    # (b) Bound slot denies bare use_tool(start_session) and unparseable arguments fail closed.
    _write_slot_cache(home_dir, "slot-gw-bound", uuid=UUID_A, csid=CSID_A)
    out_bare = _run_inject_agy(
        _agy_pre_tool_payload(
            "slot-gw-bound",
            args={
                "ServerName": "unitares-governance",
                "ToolName": "use_tool",
                "Arguments": {"tool_name": "start_session", "arguments": {}},
            },
        ),
        home_dir=home_dir,
    )
    assert out_bare["decision"] == "deny"

    out_bad = _run_inject_agy(
        _agy_pre_tool_payload(
            "slot-gw-bound",
            args={
                "ServerName": "unitares-governance",
                "ToolName": "use_tool",
                "Arguments": {"tool_name": "start_session", "arguments": "{not-json"},
            },
        ),
        home_dir=home_dir,
    )
    assert out_bad["decision"] == "deny"


def test_install_agy_hooks_merges_into_user_hooks_json_without_claude_collision(tmp_path):
    """scripts/install_agy_hooks.py syncs hooks/agy-hooks.json into target hooks.json."""
    assert not (PLUGIN_ROOT / "hooks" / "hooks.json").exists()
    installer = PLUGIN_ROOT / "scripts" / "install_agy_hooks.py"
    assert installer.is_file() and os.access(installer, os.X_OK)

    target = tmp_path / ".gemini" / "config" / "hooks.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"agent-presence": {"Stop": [{"type": "command", "command": "true"}]}}),
        encoding="utf-8",
    )

    # --check fails before install
    res_check_pre = subprocess.run(
        [sys.executable, str(installer), "--target", str(target), "--check"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res_check_pre.returncode == 1

    # Install merges unitares-governance and preserves sibling agent-presence
    res_install = subprocess.run(
        [sys.executable, str(installer), "--target", str(target)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res_install.returncode == 0
    merged = json.loads(target.read_text(encoding="utf-8"))
    assert "agent-presence" in merged
    ug = merged["unitares-governance"]
    resolved_root = str(PLUGIN_ROOT.resolve())
    assert resolved_root in ug["SessionStart"][0]["command"]
    assert "${PLUGIN_ROOT" not in ug["SessionStart"][0]["command"]
    assert ug["PreToolUse"][0]["matcher"] == pre_manifest_matcher()
    assert resolved_root in ug["PreToolUse"][0]["hooks"][0]["command"]
    assert resolved_root in ug["Stop"][0]["command"]
    assert "UNITARES_AUTO_ONBOARD=" not in ug["Stop"][0]["command"]

    # --check passes after install
    res_check_post = subprocess.run(
        [sys.executable, str(installer), "--target", str(target), "--check"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res_check_post.returncode == 0

    # --auto-onboard prefixes SessionStart and Stop only (never PreToolUse)
    res_auto = subprocess.run(
        [sys.executable, str(installer), "--target", str(target), "--auto-onboard"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res_auto.returncode == 0
    auto_ug = json.loads(target.read_text(encoding="utf-8"))["unitares-governance"]
    assert auto_ug["SessionStart"][0]["command"].startswith("UNITARES_AUTO_ONBOARD=on ")
    assert auto_ug["Stop"][0]["command"].startswith("UNITARES_AUTO_ONBOARD=on ")
    assert "UNITARES_AUTO_ONBOARD=" not in auto_ug["PreToolUse"][0]["hooks"][0]["command"]

    # Symlink target is preserved (updates referent in place) and file mode is preserved.
    real_file = tmp_path / "real-hooks.json"
    real_file.write_text("{}\n", encoding="utf-8")
    real_file.chmod(0o640)
    link_file = tmp_path / "link-hooks.json"
    link_file.symlink_to(real_file)
    res_link = subprocess.run(
        [sys.executable, str(installer), "--target", str(link_file)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res_link.returncode == 0
    assert link_file.is_symlink()
    assert (real_file.stat().st_mode & 0o777) == 0o640
    assert "unitares-governance" in json.loads(real_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 7. Claude review hardening regressions (PR #434)
# ---------------------------------------------------------------------------


def test_failed_first_mint_allows_retry_in_same_turn_without_marker_lockout(
    tmp_path, monkeypatch
):
    """If the first start_session fails on the server, a retry with a new toolCall.id
    in the same turn must be allowed once the transcript records the failed attempt."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    slot = "slot-retry-after-fail"
    tx_path = tmp_path / "retry_turn.jsonl"
    _write_transcript(tx_path, [{"step_index": 0, "type": "USER_INPUT", "content": "start"}])

    call1 = _agy_pre_tool_payload(
        slot,
        args={
            "ServerName": "unitares-governance",
            "ToolName": "start_session",
            "Arguments": {"force_new": True},
        },
    )
    call1["toolCall"]["id"] = "toolu_attempt_1"
    call1["transcriptPath"] = str(tx_path)

    # First attempt passes and claims the turn marker.
    assert _run_inject_agy(call1, home_dir=fake_home) == {"decision": "allow"}

    # While call 1 is still in flight (no failure recorded yet), a parallel duplicate is denied.
    call_dup = _agy_pre_tool_payload(
        slot,
        args={
            "ServerName": "unitares-governance",
            "ToolName": "start_session",
            "Arguments": {"force_new": True},
        },
    )
    call_dup["toolCall"]["id"] = "toolu_parallel_dup"
    call_dup["transcriptPath"] = str(tx_path)
    assert _run_inject_agy(call_dup, home_dir=fake_home)["decision"] == "deny"

    # Now record call 1 finishing with a server error (no valid uuid/csid).
    _write_transcript(
        tx_path,
        [
            {"step_index": 0, "type": "USER_INPUT", "content": "start"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [
                    {
                        "id": "toolu_attempt_1",
                        "name": "call_mcp_tool",
                        "args": {
                            "ServerName": '"unitares-governance"',
                            "ToolName": '"start_session"',
                            "Arguments": json.dumps({"force_new": True}),
                        },
                    }
                ],
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "ERROR",
                "content": json.dumps({"success": False, "error": "connection timeout"}),
            },
        ],
    )

    # Retry with a new toolCall.id is allowed because attempt 1 failed without binding.
    call_retry = _agy_pre_tool_payload(
        slot,
        args={
            "ServerName": "unitares-governance",
            "ToolName": "start_session",
            "Arguments": {"force_new": True},
        },
    )
    call_retry["toolCall"]["id"] = "toolu_attempt_2"
    call_retry["transcriptPath"] = str(tx_path)
    assert _run_inject_agy(call_retry, home_dir=fake_home) == {"decision": "allow"}

    # A third call while attempt 2 is in flight is blocked again.
    call3 = _agy_pre_tool_payload(
        slot,
        args={
            "ServerName": "unitares-governance",
            "ToolName": "start_session",
            "Arguments": {"force_new": True},
        },
    )
    call3["toolCall"]["id"] = "toolu_attempt_3"
    call3["transcriptPath"] = str(tx_path)
    assert _run_inject_agy(call3, home_dir=fake_home)["decision"] == "deny"


def test_prior_failed_identity_resume_does_not_allow_parallel_double_start_session(
    tmp_path, monkeypatch
):
    """An earlier failed identity(resume=true) in the same turn sets baseline_failed_count=1
    so parallel unparented start_session calls A and B allow A and deny B."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    slot = "slot-prior-failed-identity"
    tx_path = tmp_path / "prior_identity_fail.jsonl"
    _write_transcript(
        tx_path,
        [
            {"step_index": 0, "type": "USER_INPUT", "content": "resume then mint"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [
                    {
                        "id": "toolu_resume_fail",
                        "name": "call_mcp_tool",
                        "args": {
                            "ServerName": '"unitares-governance"',
                            "ToolName": '"identity"',
                            "Arguments": json.dumps({"resume": True}),
                        },
                    }
                ],
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "DONE",
                "content": json.dumps({"success": False, "error": "no matching identity"}),
            },
        ],
    )

    call_a = _agy_pre_tool_payload(
        slot,
        args={
            "ServerName": "unitares-governance",
            "ToolName": "start_session",
            "Arguments": {"force_new": True},
        },
    )
    call_a["toolCall"]["id"] = "toolu_start_a"
    call_a["transcriptPath"] = str(tx_path)

    call_b = _agy_pre_tool_payload(
        slot,
        args={
            "ServerName": "unitares-governance",
            "ToolName": "start_session",
            "Arguments": {"force_new": True},
        },
    )
    call_b["toolCall"]["id"] = "toolu_start_b"
    call_b["transcriptPath"] = str(tx_path)

    assert _run_inject_agy(call_a, home_dir=fake_home) == {"decision": "allow"}
    assert _run_inject_agy(call_b, home_dir=fake_home)["decision"] == "deny"


def test_unrelated_identity_failure_after_pending_mint_does_not_unlock_second_mint(
    tmp_path, monkeypatch
):
    """If an unrelated identity(resume=true) fails AFTER a start_session mint with a
    call_id is pending in the same turn, a second start_session must still be denied."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    slot = "slot-unrelated-fail-after-claim"
    tx_path = tmp_path / "unrelated_fail_after_claim.jsonl"
    _write_transcript(tx_path, [{"step_index": 0, "type": "USER_INPUT", "content": "start"}])

    # Call 1 claims the turn marker with call_id="toolu_start_pending".
    call1 = _agy_pre_tool_payload(
        slot,
        args={
            "ServerName": "unitares-governance",
            "ToolName": "start_session",
            "Arguments": {"force_new": True},
        },
    )
    call1["toolCall"]["id"] = "toolu_start_pending"
    call1["transcriptPath"] = str(tx_path)
    assert _run_inject_agy(call1, home_dir=fake_home) == {"decision": "allow"}

    # While call 1 is still in flight, an unrelated identity(resume=true) fails
    # (and even a different start_session call id failing must not match call 1).
    _write_transcript(
        tx_path,
        [
            {"step_index": 0, "type": "USER_INPUT", "content": "start"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [
                    {
                        "id": "toolu_unrelated_identity",
                        "name": "call_mcp_tool",
                        "args": {
                            "ServerName": '"unitares-governance"',
                            "ToolName": '"identity"',
                            "Arguments": json.dumps({"resume": True}),
                        },
                    },
                    {
                        "id": "toolu_other_start",
                        "name": "call_mcp_tool",
                        "args": {
                            "ServerName": '"unitares-governance"',
                            "ToolName": '"start_session"',
                            "Arguments": json.dumps({"force_new": True}),
                        },
                    },
                ],
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "DONE",
                "content": json.dumps({"success": False, "error": "not found"}),
            },
            {
                "step_index": 3,
                "type": "GENERIC",
                "status": "ERROR",
                "content": json.dumps({"success": False, "error": "other failure"}),
            },
        ],
    )

    call2 = _agy_pre_tool_payload(
        slot,
        args={
            "ServerName": "unitares-governance",
            "ToolName": "start_session",
            "Arguments": {"force_new": True},
        },
    )
    call2["toolCall"]["id"] = "toolu_start_dup"
    call2["transcriptPath"] = str(tx_path)
    assert _run_inject_agy(call2, home_dir=fake_home)["decision"] == "deny"


def test_live_slot_denies_mismatched_parent_agent_id_and_non_session_proof_and_longest_alias(
    tmp_path,
):
    """Verify P2-2, P3-4, and P3-6 hardenings on governance_call_inject --host agy."""
    home_dir = tmp_path / "home"
    conv_id = "slot-live-parent-check"
    _write_slot_cache(home_dir, conv_id, uuid=UUID_A, csid=CSID_A)

    # Mismatched parent_agent_id != active UUID_A is denied even with spawn_reason="explicit".
    out_mismatch = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            args={
                "ServerName": "unitares-governance",
                "ToolName": "start_session",
                "Arguments": {
                    "force_new": True,
                    "parent_agent_id": UUID_B,
                    "spawn_reason": "explicit",
                },
            },
        ),
        home_dir=home_dir,
    )
    assert out_mismatch["decision"] == "deny"
    assert UUID_A in out_mismatch["reason"]
    assert CSID_A in out_mismatch["reason"]
    assert 'spawn_reason="explicit"' not in out_mismatch["reason"]
    assert 'spawn_reason="subagent"' in out_mismatch["reason"]

    # P3-6: agent_id / agent_uuid alone (without client_session_id/continuity_token)
    # does NOT bypass bare-mint denial on a live bound slot.
    for weak_args in (
        {"agent_id": "some-label"},
        {"agent_uuid": UUID_A},
    ):
        out_weak = _run_inject_agy(
            _agy_pre_tool_payload(
                conv_id,
                args={
                    "ServerName": "unitares-governance",
                    "ToolName": "start_session",
                    "Arguments": weak_args,
                },
            ),
            home_dir=home_dir,
        )
        assert out_weak["decision"] == "deny", f"expected deny for {weak_args}"
        assert 'spawn_reason="explicit"' not in out_weak["reason"]

    # P3-4: longest-first alias matching resolves mcp_unitares_governance_sync_state -> sync_state
    # (never shadowed by the shorter "unitares" alias returning "governance_sync_state").
    assert (
        governance_call_inject._agy_direct_mcp_suffix("mcp_unitares_governance_sync_state")
        == "sync_state"
    )
    out_direct = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            tool_name="mcp_unitares_governance_sync_state",
            args={"response_text": "check", "complexity": 0.2},
        ),
        home_dir=home_dir,
    )
    assert out_direct["decision"] == "allow"
    assert out_direct["overwrite"]["client_session_id"] == CSID_A


# ---------------------------------------------------------------------------
# 6. Antigravity decision contract (#445)
# ---------------------------------------------------------------------------
#
# Antigravity's hook reference documents `allow` as deferring to the next hook
# or the default policy, and `auto_approve` as the value that skips a prompt.
# The agy path must only ever print `allow`, `allow` + `overwrite`, or `deny`.

_AGY_ALLOWED_DECISIONS = {"allow", "deny"}
_AGY_FORBIDDEN_KEYS = {"permissionOverrides", "permissionDecision", "hookSpecificOutput"}


def _assert_agy_contract(out: dict) -> None:
    assert isinstance(out, dict), out
    assert out.get("decision") in _AGY_ALLOWED_DECISIONS, out
    assert not (_AGY_FORBIDDEN_KEYS & set(out)), out
    if out["decision"] == "deny":
        assert set(out) == {"decision", "reason"}, out
        assert isinstance(out["reason"], str) and out["reason"], out
    else:
        assert set(out) <= {"decision", "overwrite"}, out
        if "overwrite" in out:
            assert isinstance(out["overwrite"], dict) and out["overwrite"], out


def test_agy_pre_tool_use_never_emits_auto_approve_or_permission_overrides(tmp_path):
    home_dir = tmp_path / "home"
    conv_id = "agy-conv-contract"
    _write_slot_cache(home_dir, conv_id, uuid=UUID_A, csid=CSID_A)

    def mcp(server: str, tool: str, arguments: object) -> dict:
        return _agy_pre_tool_payload(
            conv_id,
            args={"ServerName": server, "ToolName": tool, "Arguments": arguments},
        )

    payloads: list[dict | str] = [
        "",
        "not json",
        "[]",
        "{}",
        json.dumps({"toolCall": "not-a-dict"}),
        _agy_pre_tool_payload(conv_id, tool_name="run_command", args={"CommandLine": "ls"}),
        mcp("github", "create_issue", {"title": "x"}),
        mcp("unitares-governance-shadow", "start_session", {"force_new": True}),
        mcp("unitares-governance", "sync_state", {"response_text": "ok"}),
        mcp("unitares-governance", "sync_state", json.dumps({"response_text": "ok"})),
        mcp("unitares-governance", "sync_state", {"client_session_id": CSID_A}),
        mcp("unitares-governance", "sync_state", {"client_session_id": CSID_B}),
        mcp("unitares-governance", "start_session", {}),
        mcp("unitares-governance", "start_session", "{not json"),
        mcp("unitares-governance", "identity", {}),
        mcp("unitares-governance", "unknown_tool", {"x": 1}),
        mcp("unitares-governance", "use_tool", {"tool_name": "start_session", "arguments": {}}),
        mcp("unitares-governance", "use_tool", {"tool_name": "start_session", "arguments": "{bad"}),
        mcp("unitares-governance", "use_tool", {"tool_name": "sync_state", "arguments": {}}),
        _agy_pre_tool_payload(
            conv_id, tool_name="mcp_unitares-governance_sync_state", args={"response_text": "d"}
        ),
        _agy_pre_tool_payload(
            conv_id, tool_name="mcp_unitares-governance_start_session", args={}
        ),
    ]
    seen: set[str] = set()
    for payload in payloads:
        for via_hook in (False, True):
            out = _run_inject_agy(payload, home_dir=home_dir, via_hook=via_hook)
            _assert_agy_contract(out)
            seen.add("overwrite" if "overwrite" in out else out["decision"])
    # The sweep must reach every response shape, or it proves nothing.
    assert seen == {"allow", "overwrite", "deny"}, seen

    # The rewrite keeps every unrelated argument (overwrite is a shallow merge
    # into toolCall.args, so Arguments is replaced wholesale).
    rewrite = _run_inject_agy(
        mcp("unitares-governance", "sync_state", {"response_text": "r", "complexity": 0.2}),
        home_dir=home_dir,
    )
    assert rewrite["overwrite"]["Arguments"] == {
        "response_text": "r",
        "complexity": 0.2,
        "client_session_id": CSID_A,
    }


@pytest.mark.parametrize("helper", ["missing", "silent", "crash"])
def test_agy_wrapper_fallback_prints_plain_allow(tmp_path, helper):
    """The shell wrapper prints `allow` itself when the helper gives nothing."""
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    (root / "scripts").mkdir()
    wrapper = root / "hooks" / "pre-governance-call"
    wrapper.write_text(
        (PLUGIN_ROOT / "hooks" / "pre-governance-call").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    helper_path = root / "scripts" / "governance_call_inject.py"
    if helper == "silent":
        helper_path.write_text("", encoding="utf-8")
    elif helper == "crash":
        helper_path.write_text("raise SystemExit(3)\n", encoding="utf-8")

    # On a bound slot the real helper denies a bare start_session, so plain
    # `allow` here can only come from the wrapper's own fallback.
    home_dir = tmp_path / "home"
    conv_id = "agy-conv-wrapper"
    _write_slot_cache(home_dir, conv_id, uuid=UUID_A, csid=CSID_A)
    payload = _agy_pre_tool_payload(
        conv_id,
        args={"ServerName": "unitares-governance", "ToolName": "start_session", "Arguments": {}},
    )
    real = _run_inject_agy(payload, home_dir=home_dir, via_hook=True)
    assert real["decision"] == "deny", real
    res = subprocess.run(
        [str(wrapper), "--host", "agy"],
        input=json.dumps(payload),
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(home_dir)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out == {"decision": "allow"}
    _assert_agy_contract(out)


def pre_manifest_matcher() -> str:
    manifest = json.loads((PLUGIN_ROOT / "hooks" / "agy-hooks.json").read_text(encoding="utf-8"))
    return manifest["unitares-governance"]["PreToolUse"][0]["matcher"]


# ---------------------------------------------------------------------------
# 7. Eager MCP dispatch coverage and unreadable identity Arguments (#445)
# ---------------------------------------------------------------------------


def test_agy_matcher_covers_eager_governance_names_and_rejects_lookalikes():
    import re

    from governance_call_inject import (
        ANCHORED_MINT_SUFFIXES,
        IDENTITY_ASSERT_SUFFIXES,
        INJECT_SUFFIXES,
        _agy_direct_mcp_suffix,
        agy_pre_tool_use_matcher,
    )
    from governance_tool_name import governance_server_aliases

    matcher = pre_manifest_matcher()
    assert matcher == agy_pre_tool_use_matcher()
    assert "\\" not in matcher  # plain characters only
    pattern = re.compile(matcher)
    handled = INJECT_SUFFIXES | ANCHORED_MINT_SUFFIXES | IDENTITY_ASSERT_SUFFIXES

    assert pattern.search("call_mcp_tool")
    # Every eager name the matcher admits resolves, in the helper, to the
    # same handled suffix: the manifest and the handler agree.
    for server in governance_server_aliases("agy"):
        for suffix in handled:
            name = f"mcp_{server}_{suffix}"
            assert pattern.search(name), name
            assert _agy_direct_mcp_suffix(name) == suffix, name

    for name in (
        "run_command",
        "xcall_mcp_tool",
        "call_mcp_tool_x",
        "mcp_github_create_issue",
        "mcp_anima_get_state",
        "mcp_unitares-governance-shadow_sync_state",
        "mcp_unitares-governance_sync_statex",
        "mcp_unitares-governance_delete_everything",
        "mcp__unitares-governance__sync_state",
        "xmcp_unitares-governance_sync_state",
    ):
        assert not pattern.search(name), name


def test_agy_eager_mint_on_bound_slot_is_denied(tmp_path):
    home_dir = tmp_path / "home"
    conv_id = "agy-conv-eager"
    _write_slot_cache(home_dir, conv_id, uuid=UUID_A, csid=CSID_A)
    out = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id, tool_name="mcp_unitares-governance_start_session", args={}
        ),
        home_dir=home_dir,
        via_hook=True,
    )
    assert out["decision"] == "deny", out


@pytest.mark.parametrize("tool", ["start_session", "onboard", "identity", "use_tool"])
@pytest.mark.parametrize("arguments", ["{not json", "[1, 2]", "42", ["list"]])
@pytest.mark.parametrize("bound", [True, False])
def test_agy_unreadable_identity_arguments_are_denied(tmp_path, tool, arguments, bound):
    home_dir = tmp_path / "home"
    conv_id = "agy-conv-unreadable"
    if bound:
        _write_slot_cache(home_dir, conv_id, uuid=UUID_A, csid=CSID_A)
    out = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            args={"ServerName": "unitares-governance", "ToolName": tool, "Arguments": arguments},
        ),
        home_dir=home_dir,
        via_hook=True,
    )
    assert out["decision"] == "deny", out
    assert "unreadable Arguments" in out["reason"]
    _assert_agy_contract(out)


def test_agy_unreadable_arguments_keep_neutral_path_off_identity(tmp_path):
    home_dir = tmp_path / "home"
    conv_id = "agy-conv-unreadable-neutral"
    _write_slot_cache(home_dir, conv_id, uuid=UUID_A, csid=CSID_A)
    for server, tool in (
        ("unitares-governance", "sync_state"),
        ("unitares-governance", "unknown_tool"),
        ("github", "start_session"),
        ("unitares-governance-shadow", "start_session"),
    ):
        out = _run_inject_agy(
            _agy_pre_tool_payload(
                conv_id,
                args={"ServerName": server, "ToolName": tool, "Arguments": "{not json"},
            ),
            home_dir=home_dir,
        )
        assert out == {"decision": "allow"}, (server, tool, out)


def test_agy_json_null_arguments_behave_like_absent_arguments(tmp_path):
    home_dir = tmp_path / "home"
    conv_id = "agy-conv-null-args"
    unbound = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            args={"ServerName": "unitares-governance", "ToolName": "start_session", "Arguments": "null"},
        ),
        home_dir=home_dir,
    )
    _assert_agy_contract(unbound)
    assert unbound["decision"] == "allow", unbound

    _write_slot_cache(home_dir, conv_id, uuid=UUID_A, csid=CSID_A)
    bound = _run_inject_agy(
        _agy_pre_tool_payload(
            conv_id,
            args={"ServerName": "unitares-governance", "ToolName": "start_session", "Arguments": "null"},
        ),
        home_dir=home_dir,
    )
    assert bound["decision"] == "deny", bound
