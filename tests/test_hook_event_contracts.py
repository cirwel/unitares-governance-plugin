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
