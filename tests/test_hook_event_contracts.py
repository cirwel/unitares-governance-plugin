from __future__ import annotations

import json

import pytest

from scripts import edit_hook_event, stop_hook_event


def test_claude_edit_contract_uses_scalar_file_path():
    event = edit_hook_event.normalize_edit_hook(
        {
            "session_id": "claude-slot",
            "tool_name": "MultiEdit",
            "tool_use_id": "toolu_123",
            "tool_input": {"file_path": "src/a.py", "edits": [{"old": "a", "new": "b"}]},
        },
        host="claude",
    )

    assert event.host == "claude"
    assert event.tool_use_id == "toolu_123"
    assert event.paths == ("src/a.py",)


def test_codex_edit_contract_parses_patch_envelope_in_order():
    event = edit_hook_event.normalize_edit_hook(
        {
            "session_id": "codex-slot",
            "tool_name": "apply_patch",
            "tool_use_id": "call_456",
            "tool_input": {
                "command": """*** Begin Patch
*** Add File: docs/new file.md
+new
*** Update File: src/a.py
*** Move to: src/b.py
@@
-old
+new
*** Delete File: old.py
*** Update File: src/a.py
@@
+again
*** End Patch"""
            },
        },
        host="codex",
    )

    assert event.host == "codex"
    assert event.tool_name == "apply_patch"
    assert event.tool_use_id == "call_456"
    assert event.paths == ("docs/new file.md", "src/a.py", "src/b.py", "old.py")


@pytest.mark.parametrize(
    "command",
    [
        "*** Update File: a.py\n*** End Patch",
        "*** Begin Patch\n*** Move to: b.py\n*** End Patch",
        "*** Begin Patch\n*** Add File: \n*** End Patch",
    ],
)
def test_codex_edit_contract_rejects_malformed_patch_envelopes(command: str):
    with pytest.raises(edit_hook_event.EditHookPayloadError):
        edit_hook_event.normalize_edit_hook(
            {
                "session_id": "codex-slot",
                "tool_name": "apply_patch",
                "tool_use_id": "call_bad",
                "tool_input": {"command": command},
            },
            host="codex",
        )


def test_codex_edit_contract_requires_tool_use_id_for_per_edit_ownership():
    with pytest.raises(edit_hook_event.EditHookPayloadError, match="tool_use_id"):
        edit_hook_event.normalize_edit_hook(
            {
                "session_id": "codex-slot",
                "tool_name": "apply_patch",
                "tool_input": {
                    "command": "*** Begin Patch\n*** Update File: a.py\n*** End Patch"
                },
            },
            host="codex",
        )


def test_codex_edit_contract_rejects_explicit_failed_post_tool_payload():
    with pytest.raises(edit_hook_event.EditHookPayloadError, match="reports failure"):
        edit_hook_event.normalize_edit_hook(
            {
                "session_id": "codex-slot",
                "tool_name": "apply_patch",
                "tool_use_id": "call_failed",
                "tool_input": {
                    "command": "*** Begin Patch\n*** Update File: a.py\n*** End Patch"
                },
                "tool_response": {"result": {"success": False}},
            },
            host="codex",
        )


def test_host_is_explicit_and_never_inferred_from_payload_shape():
    with pytest.raises(edit_hook_event.EditHookPayloadError, match="Claude"):
        edit_hook_event.normalize_edit_hook(
            {
                "session_id": "slot",
                "tool_name": "apply_patch",
                "tool_use_id": "call_1",
                "tool_input": {
                    "command": "*** Begin Patch\n*** Update File: a.py\n*** End Patch"
                },
            },
            host="claude",
        )


def test_edit_contract_caps_path_fanout(monkeypatch):
    monkeypatch.setattr(edit_hook_event, "MAX_EDIT_PATHS", 2)
    command = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: a.py",
            "*** Add File: b.py",
            "*** Add File: c.py",
            "*** End Patch",
        ]
    )

    with pytest.raises(edit_hook_event.EditHookPayloadError, match="maximum is 2"):
        edit_hook_event.normalize_edit_hook(
            {
                "session_id": "slot",
                "tool_name": "apply_patch",
                "tool_use_id": "call_cap",
                "tool_input": {"command": command},
            },
            host="codex",
        )


def test_claude_stop_contract_reads_last_message_without_inventing_tool_count():
    event = stop_hook_event.normalize_stop_hook(
        {
            "session_id": "claude-slot",
            "stop_hook_active": False,
            "last_assistant_message": "Finished the change.",
        },
        host="claude",
    )

    assert event.tool_count is None
    assert event.tool_names == ()
    assert "tool count unavailable" in event.summary
    assert "Finished the change" in event.summary


def test_claude_stop_contract_accepts_legacy_summary_fields():
    event = stop_hook_event.normalize_stop_hook(
        {
            "session_id": "claude-slot",
            "tool_calls": [{"name": "Read"}, {"name": "Edit"}],
            "final_text": "Finished the legacy change.",
        },
        host="claude",
    )

    assert event.tool_count == 2
    assert event.tool_names == ("Read", "Edit")
    assert "Finished the legacy change" in event.summary


def test_codex_stop_contract_reads_last_message_without_inventing_tool_count():
    event = stop_hook_event.normalize_stop_hook(
        {
            "session_id": "codex-slot",
            "turn_id": "turn_1",
            "stop_hook_active": False,
            "last_assistant_message": "Implemented and verified the change.",
        },
        host="codex",
    )

    assert event.tool_count is None
    assert event.tool_names == ()
    assert event.complexity == 0.3
    assert "tool count unavailable" in event.summary
    assert "Implemented and verified" in event.summary


def test_stop_contract_captures_safe_model_harness_provenance_without_guessing():
    event = stop_hook_event.normalize_stop_hook(
        {
            "session_id": "codex-slot",
            "last_assistant_message": "Done.",
            "model": "gpt-5.6-sol",
            "model_provider": "openai",
            "harness_version": "0.115.0",
        },
        host="codex",
    )

    assert event.model == "gpt-5.6-sol"
    assert event.model_provider == "openai"
    assert event.model_source == "harness_reported"
    assert event.harness_type == "codex-cli"
    assert event.harness_version == "0.115.0"
    assert event.harness_source == "harness_reported"


def test_stop_contract_rejects_secret_shaped_model_before_hook_argv():
    secret = "sk-ant-" + "A" * 40
    event = stop_hook_event.normalize_stop_hook(
        {
            "session_id": "claude-slot",
            "last_assistant_message": "Done.",
            "model": secret,
        },
        host="claude",
    )

    assert event.model == ""
    assert event.model_source == "unavailable"
    assert secret not in json.dumps(event.to_json_dict())


def _transcript(tmp_path, entries, name="session.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return str(path)


def _assistant(model):
    return {"type": "assistant", "message": {"model": model, "content": []}}


def _claude_stop(**extra):
    payload = {"session_id": "claude-slot", "last_assistant_message": "Done."}
    payload.update(extra)
    return stop_hook_event.normalize_stop_hook(payload, host="claude")


def test_claude_stop_reads_model_from_transcript_when_payload_has_none(tmp_path):
    path = _transcript(
        tmp_path,
        [_assistant("claude-opus-5-5"), {"type": "user"}, _assistant("claude-sonnet-5-5")],
    )
    event = _claude_stop(transcript_path=path)

    assert event.model == "claude-sonnet-5-5"
    assert event.model_source == "harness_reported"


def test_claude_stop_payload_model_wins_over_transcript(tmp_path):
    path = _transcript(tmp_path, [_assistant("claude-sonnet-5-5")])
    event = _claude_stop(model="claude-opus-5-5", transcript_path=path)

    assert event.model == "claude-opus-5-5"


@pytest.mark.parametrize(
    "make_path",
    [
        lambda tmp: str(tmp / "missing.jsonl"),
        lambda tmp: "relative/session.jsonl",
        lambda tmp: str(tmp),
        lambda tmp: _transcript(tmp, [_assistant("claude-sonnet-5-5")], name="session.txt"),
    ],
)
def test_claude_stop_unusable_transcript_path_stays_unavailable(tmp_path, make_path):
    event = _claude_stop(transcript_path=make_path(tmp_path))

    assert event.model == ""
    assert event.model_source == "unavailable"


def test_claude_stop_transcript_skips_synthetic_and_unsafe_models(tmp_path):
    secret = "sk-ant-" + "A" * 40
    path = _transcript(
        tmp_path,
        [_assistant("claude-sonnet-5-5"), _assistant("<synthetic>"), _assistant(secret)],
    )
    event = _claude_stop(transcript_path=path)

    # Newest usable entry wins; unusable ones are skipped, never retained.
    assert event.model == "claude-sonnet-5-5"
    assert secret not in json.dumps(event.to_json_dict())


def test_claude_stop_transcript_with_no_usable_model_stays_unavailable(tmp_path):
    path = _transcript(tmp_path, [{"type": "user"}, _assistant("<synthetic>"), _assistant(None)])
    event = _claude_stop(transcript_path=path)

    assert event.model == ""
    assert event.model_source == "unavailable"


def test_claude_stop_transcript_tolerates_garbage_lines(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps(_assistant("claude-sonnet-5-5")) + "\nnot json\n[1,2]\n\n"
    )
    assert _claude_stop(transcript_path=str(path)).model == "claude-sonnet-5-5"


def test_claude_stop_transcript_read_is_bounded_to_the_tail(tmp_path):
    path = tmp_path / "session.jsonl"
    old = json.dumps(_assistant("claude-opus-5-5")) + "\n"
    filler = json.dumps({"type": "user", "x": "y" * 1000}) + "\n"
    path.write_text(old + filler * 1000)  # ~1 MB; old entry far outside the tail

    event = _claude_stop(transcript_path=str(path))

    assert event.model == ""
    assert event.model_source == "unavailable"


def test_codex_stop_ignores_transcript_path(tmp_path):
    path = _transcript(tmp_path, [_assistant("claude-sonnet-5-5")])
    event = stop_hook_event.normalize_stop_hook(
        {"session_id": "s", "last_assistant_message": "x", "transcript_path": path},
        host="codex",
    )

    assert event.model == ""
    assert event.model_source == "unavailable"


@pytest.mark.parametrize("bad", ["/tmp/x\x00.jsonl", "/tmp/\ud800.jsonl"])
def test_claude_stop_transcript_path_that_cannot_be_opened_never_raises(bad):
    event = _claude_stop(transcript_path=bad)

    assert event.model == ""
    assert event.model_source == "unavailable"


def test_claude_stop_transcript_fifo_does_not_block(tmp_path):
    import os

    fifo = tmp_path / "session.jsonl"
    os.mkfifo(fifo)  # open() for reading would block forever with no writer
    event = _claude_stop(transcript_path=str(fifo))

    assert event.model == ""
    assert event.model_source == "unavailable"


def test_agy_stop_contract_reads_turn_and_tool_calls_from_artifact_transcript(tmp_path):
    conv_id = "6dafb6a1-3e48-4229-92ce-7b6dc6f5756a"
    brain_dir = tmp_path / "brain" / conv_id
    logs_dir = brain_dir / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    transcript = logs_dir / "transcript.jsonl"
    entries = [
        {"step_index": 0, "type": "USER_INPUT", "content": " earlier turn "},
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "content": "Earlier turn answer.",
            "tool_calls": [{"name": "run_command"}],
        },
        {"step_index": 2, "type": "USER_INPUT", "content": "current turn"},
        {
            "step_index": 3,
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{"name": "read_file"}, {"name": "edit_file"}],
        },
        {
            "step_index": 4,
            "type": "PLANNER_RESPONSE",
            "content": "Implemented the Antigravity Stop hook.",
            "tool_calls": [{"name": "call_mcp_tool"}],
        },
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    event = stop_hook_event.normalize_stop_hook(
        {
            "conversationId": conv_id,
            "workspacePaths": [str(tmp_path / "ws")],
            "transcriptPath": str(tmp_path / "nonexistent.jsonl"),
            "artifactDirectoryPath": str(brain_dir),
            "modelName": "auto",
        },
        host="agy",
    )

    assert event.host == "agy"
    assert event.session_id == conv_id
    assert event.response_text == "Implemented the Antigravity Stop hook."
    assert event.tool_count == 3
    assert event.tool_names == ("read_file", "edit_file", "call_mcp_tool")
    assert event.model == ""
    assert event.model_source == "unavailable"
    assert event.harness_type == "antigravity"
    assert event.workspace_dir == str(tmp_path / "ws")

    event_with_model = stop_hook_event.normalize_stop_hook(
        {
            "conversationId": conv_id,
            "artifactDirectoryPath": str(brain_dir),
            "modelName": "gemini-4-argon",
        },
        host="agy",
    )
    assert event_with_model.model == "gemini-4-argon"
    assert event_with_model.model_source == "harness_reported"


def test_agy_stop_contract_truncation_returns_unavailable_tool_count(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(stop_hook_event, "_AGY_TRANSCRIPT_TAIL_BYTES", 4096)
    transcript = tmp_path / "transcript.jsonl"
    user_line = json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "start"}) + "\n"
    filler_line = (
        json.dumps(
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "x" * 1000,
                "tool_calls": [{"name": "read_file"}],
            }
        )
        + "\n"
    )
    final_line = (
        json.dumps(
            {
                "step_index": 999,
                "type": "PLANNER_RESPONSE",
                "content": "Final response after long turn.",
                "tool_calls": [{"name": "edit_file"}],
            }
        )
        + "\n"
    )
    transcript.write_text(user_line + filler_line * 10 + final_line)

    event = stop_hook_event.normalize_stop_hook(
        {
            "conversationId": "conv-long",
            "transcriptPath": str(transcript),
        },
        host="agy",
    )

    assert event.response_text == "Final response after long turn."
    assert event.tool_count is None
    assert event.tool_names == ()


def test_agy_stop_contract_system_message_boundary_and_subagent_detection(tmp_path):
    sub_transcript = _transcript(
        tmp_path,
        [
            {"step_index": 0, "type": "SYSTEM_MESSAGE", "content": "subagent prompt"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "Subagent finished.",
                "tool_calls": [{"name": "read_file"}],
            },
        ],
    )
    sub_event = stop_hook_event.normalize_stop_hook(
        {"conversationId": "sub-1", "transcriptPath": sub_transcript},
        host="agy",
    )
    assert sub_event.is_subagent is True
    assert sub_event.tool_count == 1
    assert sub_event.tool_names == ("read_file",)

    wakeup_dir = tmp_path / "wakeup"
    wakeup_dir.mkdir()
    wakeup_transcript = _transcript(
        wakeup_dir,
        [
            {"step_index": 0, "type": "USER_INPUT", "content": "run task"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "Launched subagent.",
                "tool_calls": [{"name": "invoke_subagent"}, {"name": "read_file"}],
            },
            {"step_index": 2, "type": "SYSTEM_MESSAGE", "content": "subagent replied"},
            {
                "step_index": 3,
                "type": "PLANNER_RESPONSE",
                "content": "Processed subagent reply.",
                "tool_calls": [{"name": "edit_file"}],
            },
        ],
    )
    wakeup_event = stop_hook_event.normalize_stop_hook(
        {"conversationId": "parent-1", "transcriptPath": wakeup_transcript},
        host="agy",
    )
    assert wakeup_event.is_subagent is False
    assert wakeup_event.response_text == "Processed subagent reply."
    assert wakeup_event.tool_count == 1
    assert wakeup_event.tool_names == ("edit_file",)


def test_agy_stop_contract_unflushed_turn_does_not_leak_previous_turn(tmp_path):
    transcript = _transcript(
        tmp_path,
        [
            {"step_index": 0, "type": "USER_INPUT", "content": "turn 1"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "Turn 1 response that must not leak.",
                "tool_calls": [{"name": "read_file"}],
            },
            {"step_index": 2, "type": "USER_INPUT", "content": "turn 2 canceled early"},
        ],
    )
    event = stop_hook_event.normalize_stop_hook(
        {
            "conversationId": "conv-unflushed",
            "transcriptPath": transcript,
        },
        host="agy",
    )

    assert event.response_text == ""
    assert event.tool_count == 0
    assert event.tool_names == ()


def test_agy_stop_contract_rejects_cross_session_brain_path_and_symlinks(tmp_path):
    other_brain = tmp_path / "brain" / "other-session-uuid" / ".system_generated" / "logs"
    other_brain.mkdir(parents=True)
    other_transcript = other_brain / "transcript.jsonl"
    other_transcript.write_text(
        json.dumps(
            {
                "step_index": 0,
                "type": "PLANNER_RESPONSE",
                "content": "Other session secret output.",
            }
        )
        + "\n"
    )

    event = stop_hook_event.normalize_stop_hook(
        {
            "conversationId": "my-session-uuid",
            "artifactDirectoryPath": str(other_brain.parent.parent),
            "transcriptPath": str(other_transcript),
        },
        host="agy",
    )
    assert event.response_text == ""
    assert event.tool_count is None

    symlink_path = tmp_path / "symlink.jsonl"
    try:
        symlink_path.symlink_to(other_transcript)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    event_symlink = stop_hook_event.normalize_stop_hook(
        {
            "conversationId": "my-session-uuid",
            "transcriptPath": str(symlink_path),
        },
        host="agy",
    )
    assert event_symlink.response_text == ""
    assert event_symlink.tool_count is None


def test_agy_transcript_identity_recovery_binds_driver_start_session(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    ws = tmp_path / "ws"
    ws.mkdir()
    conv_id = "6dafb6a1-3e48-4229-92ce-7b6dc6f5756a"
    transcript = _transcript(
        tmp_path,
        [
            {"step_index": 0, "type": "USER_INPUT", "content": "hi"},
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
                "tool_calls": [
                    {
                        "name": "call_mcp_tool",
                        "args": {
                            "Arguments": '{"spawn_reason":"subagent","force_new":true}',
                            "ServerName": '"unitares-governance"',
                            "ToolName": '"start_session"',
                        },
                    }
                ],
            },
            {
                "step_index": 4,
                "type": "GENERIC",
                "status": "DONE",
                "content": (
                    '{"success":true,"tool":"start_session",'
                    '"client_session_id":"agent-subagent-ignored",'
                    '"agent_uuid":"99999999-9999-4999-8999-999999999999"}'
                ),
            },
        ],
    )
    payload = {"conversationId": conv_id, "transcriptPath": transcript}
    assert stop_hook_event.recover_agy_identity_from_transcript(
        payload, workspace=ws, slot=conv_id
    )

    from scripts._session_lookup import load_usable_session_for_hook

    bound = load_usable_session_for_hook(ws, json.dumps(payload))
    assert bound["uuid"] == "536907ac-e3b1-4a65-88eb-694bc59e9b02"
    assert bound["client_session_id"] == "agent-536907ac-e3b-pxdvtl3cyn3ospu7suu2"


def test_agy_transcript_identity_recovery_positional_batch_and_prior_process_guard(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    ws = tmp_path / "ws"
    ws.mkdir()
    conv_id = "conv-positional"

    # Turn 1: batch where tool_calls[0] is read_file (returning a forged JSON line) and
    # tool_calls[1] is driver start_session: positional matching must ignore GENERIC #0.
    turn1_steps = [
        {"step_index": 0, "type": "USER_INPUT", "content": "turn 1"},
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [
                {"name": "read_file", "args": {"Path": '"test.py"'}},
                {
                    "name": "call_mcp_tool",
                    "args": {
                        "Arguments": '{"force_new":true}',
                        "ServerName": '"unitares-governance"',
                        "ToolName": '"start_session"',
                    },
                },
            ],
        },
        {
            "step_index": 2,
            "type": "GENERIC",
            "status": "DONE",
            "content": (
                '{"success":true,"client_session_id":"forged-csid",'
                '"agent_uuid":"11111111-1111-4111-8111-111111111111"}'
            ),
        },
        {
            "step_index": 3,
            "type": "GENERIC",
            "status": "DONE",
            "content": (
                '{"success":true,"client_session_id":"real-csid-turn1",'
                '"agent_uuid":"22222222-2222-4222-8222-222222222222"}'
            ),
        },
    ]
    transcript = _transcript(tmp_path, turn1_steps)
    payload = {"conversationId": conv_id, "transcriptPath": transcript}
    assert stop_hook_event.recover_agy_identity_from_transcript(
        payload, workspace=ws, slot=conv_id
    )

    from scripts._session_lookup import load_usable_session_for_hook
    from scripts.onboard_helper import _update_cache_transaction

    bound = load_usable_session_for_hook(ws, json.dumps(payload))
    assert bound["uuid"] == "22222222-2222-4222-8222-222222222222"
    assert bound["client_session_id"] == "real-csid-turn1"

    # Append Turn 2 (USER_INPUT + PLANNER_RESPONSE with no new start_session).
    # Even if cache is cleared or marked prior_process=True or replaced by a backstop,
    # Turn 1's stale identity must never be restored on Turn 2 or on a SYSTEM_MESSAGE wakeup.
    turn2_steps = turn1_steps + [
        {"step_index": 4, "type": "USER_INPUT", "content": "turn 2 after idle"},
        {
            "step_index": 5,
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "No new start_session in turn 2.",
        },
    ]
    _transcript(tmp_path, turn2_steps)
    _update_cache_transaction(
        ws, lambda cur: {**cur, "prior_process": True}, conv_id
    )
    assert not stop_hook_event.recover_agy_identity_from_transcript(
        payload, workspace=ws, slot=conv_id
    )
    assert load_usable_session_for_hook(ws, json.dumps(payload)) == {}

    # When post-stop then lazy-onboards a fresh harness_backstop identity B, a
    # subsequent Stop on Turn 2 or a SYSTEM_MESSAGE wakeup must NOT overwrite B with Turn 1's A.
    _update_cache_transaction(
        ws,
        lambda cur: {
            **cur,
            "uuid": "33333333-3333-4333-8333-333333333333",
            "client_session_id": "backstop-csid-turn2",
            "onboard_origin": "harness_backstop",
            "prior_process": False,
        },
        conv_id,
    )
    wakeup_steps = turn2_steps + [
        {"step_index": 6, "type": "SYSTEM_MESSAGE", "content": "subagent wakeup"},
        {
            "step_index": 7,
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "Wakeup response without new onboard.",
        },
    ]
    _transcript(tmp_path, wakeup_steps)
    assert not stop_hook_event.recover_agy_identity_from_transcript(
        payload, workspace=ws, slot=conv_id
    )
    bound_after_backstop = load_usable_session_for_hook(ws, json.dumps(payload))
    assert bound_after_backstop["uuid"] == "33333333-3333-4333-8333-333333333333"
    assert bound_after_backstop["client_session_id"] == "backstop-csid-turn2"


def test_agy_subagent_with_large_prompt_and_current_turn_upgrade(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    ws = tmp_path / "ws"
    ws.mkdir()

    # Subagent step_index=0 SYSTEM_MESSAGE > 64 KB must still be detected as is_subagent.
    sub_dir = tmp_path / "sub_large"
    sub_dir.mkdir()
    sub_transcript = _transcript(
        sub_dir,
        [
            {
                "step_index": 0,
                "type": "SYSTEM_MESSAGE",
                "content": "prompt " + ("x" * 100_000),
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "Done.",
                "tool_calls": [{"name": "read_file"}],
            },
        ],
    )
    sub_event = stop_hook_event.normalize_stop_hook(
        {"conversationId": "sub-large", "transcriptPath": sub_transcript},
        host="agy",
    )
    assert sub_event.is_subagent is True

    # Explicit start_session in the current turn upgrades a prior/expired cache binding.
    long_dir = tmp_path / "long_turn"
    long_dir.mkdir()
    conv_id = "conv-long-turn"
    entries = [
        {"step_index": 0, "type": "USER_INPUT", "content": "current turn start"},
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [
                {
                    "name": "call_mcp_tool",
                    "args": {
                        "Arguments": '{"force_new":true}',
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
                '{"success":true,"client_session_id":"long-turn-csid",'
                '"agent_uuid":"44444444-4444-4444-8444-444444444444"}'
            ),
        },
    ]
    long_transcript = _transcript(long_dir, entries)
    from scripts._session_lookup import load_usable_session_for_hook
    from scripts.binding_activity import mark as mark_binding_activity, touch as touch_binding_activity
    from scripts.onboard_helper import _update_cache_transaction

    _update_cache_transaction(
        ws,
        lambda cur: {
            **cur,
            "uuid": "44444444-4444-4444-8444-444444444444",
            "client_session_id": "long-turn-csid",
            "prior_process": True,
            "schema_version": 2,
            "slot": conv_id,
        },
        conv_id,
    )
    # Simulate the back-dated activity stamp left when mark() set prior_process=True.
    touch_binding_activity(
        conv_id,
        "44444444-4444-4444-8444-444444444444",
        "long-turn-csid",
        at=1.0,
    )
    payload = {"conversationId": conv_id, "transcriptPath": long_transcript}
    assert stop_hook_event.recover_agy_identity_from_transcript(
        payload, workspace=ws, slot=conv_id
    )
    assert mark_binding_activity(str(ws), json.dumps(payload)) == "touched"
    bound = load_usable_session_for_hook(ws, json.dumps(payload))
    assert bound["uuid"] == "44444444-4444-4444-8444-444444444444"
    assert bound["client_session_id"] == "long-turn-csid"
    assert bound["prior_process"] is False


def _run_git(repo, *args: str) -> str:
    import subprocess

    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_git_state_breadcrumb_covers_clean_dirty_ahead_detached_and_lockfree(tmp_path):
    assert stop_hook_event.git_state_breadcrumb("") == ""
    non_git = tmp_path / "plain"
    non_git.mkdir()
    assert stop_hook_event.git_state_breadcrumb(non_git) == ""

    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    _run_git(upstream, "init", "--bare")

    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-b", "main")
    (repo / "tracked.txt").write_text("base\n")
    _run_git(repo, "add", "tracked.txt")
    _run_git(repo, "commit", "-m", "initial")
    head_sha = _run_git(repo, "rev-parse", "HEAD")

    clean = stop_hook_event.git_state_breadcrumb(repo)
    assert clean == f"git=main@{head_sha[:9]}(clean)"
    assert not (repo / ".git" / "index.lock").exists()

    # Dirty repo: one modified tracked file + one untracked file -> dirty=2.
    (repo / "tracked.txt").write_text("modified\n")
    (repo / "untracked.txt").write_text("extra\n")
    dirty = stop_hook_event.git_state_breadcrumb(repo)
    assert dirty == f"git=main@{head_sha[:9]}(dirty=2)"
    assert not (repo / ".git" / "index.lock").exists()

    # Reset working tree and push baseline, then create ahead commit.
    _run_git(repo, "checkout", "--", "tracked.txt")
    (repo / "untracked.txt").unlink()
    _run_git(repo, "remote", "add", "origin", str(upstream))
    _run_git(repo, "push", "-u", "origin", "main")

    (repo / "tracked.txt").write_text("ahead clean\n")
    _run_git(repo, "commit", "-am", "ahead commit")
    ahead_sha = _run_git(repo, "rev-parse", "HEAD")
    assert (
        stop_hook_event.git_state_breadcrumb(repo)
        == f"git=main@{ahead_sha[:9]}(+1,clean)"
    )

    (repo / "tracked.txt").write_text("ahead dirty\n")
    assert (
        stop_hook_event.git_state_breadcrumb(repo)
        == f"git=main@{ahead_sha[:9]}(+1,dirty=1)"
    )

    # Detached HEAD.
    _run_git(repo, "checkout", "--force", ahead_sha)
    assert (
        stop_hook_event.git_state_breadcrumb(repo)
        == f"git=detached@{ahead_sha[:9]}(clean)"
    )

    # Secret-bearing branch name must be redacted; URL-like branch must sanitize too.
    secret = "ghp_" + "Z" * 30
    _run_git(repo, "checkout", "-b", f"feat/{secret}")
    secret_crumb = stop_hook_event.git_state_breadcrumb(repo)
    assert secret not in secret_crumb
    assert secret_crumb == f"git=<redacted>@{ahead_sha[:9]}(clean)"
    assert stop_hook_event._sanitize_git_branch("https://user:pw@example.com") == "<redacted>"


def test_stop_hook_git_breadcrumb_stays_in_summary_only_and_respects_agy_workspace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("v1\n")
    _run_git(repo, "add", "a.txt")
    _run_git(repo, "commit", "-m", "init")
    sha = _run_git(repo, "rev-parse", "HEAD")

    claude_event = stop_hook_event.normalize_stop_hook(
        {
            "session_id": "s1",
            "last_assistant_message": "Checked repository.",
            "model": "claude-opus-5-5",
        },
        host="claude",
        workspace=repo,
        probe_git=True,
    )
    assert f"; git=main@{sha[:9]}(clean)" in claude_event.summary
    assert "git=" not in claude_event.floor_summary
    assert "main" not in claude_event.floor_summary
    assert sha[:9] not in claude_event.floor_summary

    # agy with empty workspacePaths must not fall back to --workspace.
    transcript = _transcript(
        tmp_path,
        [
            {"step_index": 0, "type": "USER_INPUT", "content": "hi"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "Done."},
        ],
    )
    agy_no_ws = stop_hook_event.normalize_stop_hook(
        {"conversationId": "conv-nows", "workspacePaths": [], "transcriptPath": transcript},
        host="agy",
        workspace=repo,
        probe_git=True,
    )
    assert agy_no_ws.git_context == ""

    agy_with_ws = stop_hook_event.normalize_stop_hook(
        {
            "conversationId": "conv-ws",
            "workspacePaths": [str(repo)],
            "transcriptPath": transcript,
        },
        host="agy",
        workspace="",
        probe_git=True,
    )
    assert agy_with_ws.git_context == f"git=main@{sha[:9]}(clean)"
    assert f"; git=main@{sha[:9]}(clean)" in agy_with_ws.summary


def test_stop_hook_redacts_secrets_across_truncation_boundaries_and_caps_summary():
    # Place each secret right across common truncation limits (100 / 120 / 512).
    pad_85 = "A" * 85
    bearer = "Bearer " + "B" * 45
    akia = "AKIA" + "CCCCCCCCCCCCCCCC"
    ghp = "ghp_" + "D" * 28
    sk_ant = "sk-ant-api03-" + "E" * 28
    pad_500 = "F" * 500

    for secret, fragment in (
        (bearer, "BBBBBBBBBB"),
        (akia, "AKIA"),
        (ghp, "ghp_"),
        (sk_ant, "sk-ant-api03"),
    ):
        for payload_msg in (f"{pad_85} {secret}", f"{pad_500} {secret}"):
            for host in ("claude", "codex"):
                ev = stop_hook_event.normalize_stop_hook(
                    {"session_id": "s-sec", "last_assistant_message": payload_msg},
                    host=host,
                )
                assert secret not in ev.response_text
                assert fragment not in ev.response_text
                assert fragment not in ev.summary
                assert fragment not in ev.floor_summary
                for text in (ev.summary, ev.floor_summary):
                    open_idx = text.rfind("[REDACTED:")
                    if open_idx != -1:
                        assert "]" in text[open_idx:]

    # Long MCP tool names must still stay inside <= 490 chars without cutting git_context.
    long_tools = tuple(
        f"mcp__very_long_server_name__{i}_tool_operation_identifier_" + ("x" * 100)
        for i in range(3)
    )
    ev_capped = stop_hook_event.StopHookEvent(
        host="claude",
        session_id="s-cap",
        response_text="Finished checking long tool calls.",
        tool_count=5,
        tool_names=long_tools,
        git_context="git=main@123456789(clean)",
    )
    assert len(ev_capped.summary) <= 490
    assert ev_capped.summary.endswith("; git=main@123456789(clean)")


def test_git_state_breadcrumb_strips_overrides_handles_unborn_behind_conflict_and_opt_out(
    tmp_path, monkeypatch
):
    import subprocess

    # Unborn repo -> initial SHA.
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _run_git(unborn, "init", "-b", "main")
    (unborn / "new.txt").write_text("hi\n")
    assert stop_hook_event.git_state_breadcrumb(unborn) == "git=main@initial(dirty=1)"

    # Upstream + two clones to exercise behind/diverged & GIT_DIR override isolation.
    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    _run_git(upstream, "init", "--bare")

    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    _run_git(repo_a, "init", "-b", "branch-a")
    (repo_a / "f.txt").write_text("v1\n")
    _run_git(repo_a, "add", "f.txt")
    _run_git(repo_a, "commit", "-m", "init a")
    _run_git(repo_a, "remote", "add", "origin", str(upstream))
    _run_git(repo_a, "push", "-u", "origin", "branch-a")

    repo_b = tmp_path / "repo_b"
    _run_git(tmp_path, "clone", str(upstream), str(repo_b))
    _run_git(repo_b, "checkout", "-B", "branch-a", "origin/branch-a")

    # Inherited GIT_DIR / GIT_WORK_TREE pointing at other repo must not hijack repo_b.
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _run_git(unrelated, "init", "-b", "wrong-branch")
    (unrelated / "x.txt").write_text("x\n")
    _run_git(unrelated, "add", "x.txt")
    _run_git(unrelated, "commit", "-m", "unrelated")

    sha_b = _run_git(repo_b, "rev-parse", "HEAD")[:9]
    monkeypatch.setenv("GIT_DIR", str(unrelated / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(unrelated))
    crumb_b = stop_hook_event.git_state_breadcrumb(repo_b)
    assert crumb_b == f"git=branch-a@{sha_b}(clean)"
    assert "wrong-branch" not in crumb_b
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)

    # Push from repo_a, fetch in repo_b -> repo_b is -1 behind.
    (repo_a / "f.txt").write_text("v2-upstream\n")
    _run_git(repo_a, "commit", "-am", "upstream advance")
    _run_git(repo_a, "push", "origin", "branch-a")
    _run_git(repo_b, "fetch", "origin")
    assert stop_hook_event.git_state_breadcrumb(repo_b) == f"git=branch-a@{sha_b}(-1,clean)"

    # Local commit on repo_b -> +1,-1 diverged.
    (repo_b / "local.txt").write_text("local-change\n")
    _run_git(repo_b, "add", "local.txt")
    _run_git(repo_b, "commit", "-m", "local commit")
    div_sha = _run_git(repo_b, "rev-parse", "HEAD")[:9]
    assert stop_hook_event.git_state_breadcrumb(repo_b) == f"git=branch-a@{div_sha}(+1,-1,clean)"

    # Merge conflict -> conflict=1 without clean.
    (repo_b / "f.txt").write_text("v2-local\n")
    _run_git(repo_b, "commit", "-am", "conflicting edit")
    merge_res = subprocess.run(
        [
            "git",
            "-C",
            str(repo_b),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "merge",
            "origin/branch-a",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert merge_res.returncode != 0
    assert "CONFLICT" in (merge_res.stdout + merge_res.stderr)
    conflict_crumb = stop_hook_event.git_state_breadcrumb(repo_b)
    assert "conflict=1" in conflict_crumb
    assert "clean" not in conflict_crumb

    # Opt-out env disables probe cleanly; agy prefers existing directory in workspacePaths.
    monkeypatch.setenv("UNITARES_STOP_GIT_PROBE", "0")
    opted_out = stop_hook_event.normalize_stop_hook(
        {"session_id": "s-opt", "last_assistant_message": "Done."},
        host="claude",
        workspace=repo_a,
        probe_git=True,
    )
    assert opted_out.git_context == ""
    monkeypatch.delenv("UNITARES_STOP_GIT_PROBE", raising=False)

    transcript = _transcript(
        tmp_path,
        [
            {"step_index": 0, "type": "USER_INPUT", "content": "hi"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "Done."},
        ],
    )
    agy_multi = stop_hook_event.normalize_stop_hook(
        {
            "conversationId": "conv-multi",
            "workspacePaths": [str(tmp_path / "does-not-exist"), str(repo_a)],
            "transcriptPath": transcript,
        },
        host="agy",
        probe_git=True,
    )
    assert agy_multi.workspace_dir == str(repo_a)
    assert agy_multi.git_context.startswith("git=branch-a@")


