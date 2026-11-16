"""Native Codex completed-item shapes, including code-mode nested tools."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from codex_tool_outcomes import collect_window
from tool_outcomes import run

SLOT = "session-under-test"


def item(call_id, *, kind="CommandExecution", status="completed", code=0, **extra):
    return {"type": "event_msg", "payload": {
        "type": "item_completed", "thread_id": SLOT,
        "item": {"id": call_id, "type": kind, "status": status,
                 "command": "private command", "cwd": "/private-workspace",
                 "exit_code": code, **extra}}}


def start():
    return {"type": "event_msg", "payload": {"type": "task_started"}}


def append(path, *rows):
    with path.open("a") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / "rollout.jsonl"
    append(path, {"type": "session_meta", "payload": {"id": SLOT}}, start())
    return path


def test_code_mode_counts_native_results_once_not_wrapper_output(transcript):
    append(transcript, item("a", status="failed", code=2), item("b", status="failed", code=2),
           item("m", kind="McpToolCall", result={"isError": True}),
           item("ok"), item("ok"),
           {"type": "response_item", "payload": {"type": "custom_tool_call_output",
            "output": "arbitrary tool text: exit_code: 9, isError: true"}})
    counts, state = collect_window(str(transcript), None, slot=SLOT)
    assert counts["tool_delta"] == 4
    assert counts["tool_error_delta"] == 3
    assert counts["repeat_failure_delta"] == 1
    assert counts["tool_unknown_status_delta"] == 0
    assert counts["window_truncated"] is False
    counts, _ = collect_window(str(transcript), state, slot=SLOT)
    assert counts["tool_delta"] == 0
    assert "private" not in json.dumps(state)


def test_first_window_does_not_count_prior_turn(transcript):
    append(transcript, item("prior"), start(), item("now"))
    counts, state = collect_window(str(transcript), None, slot=SLOT)
    assert counts["tool_delta"] == 1
    append(transcript, item("interrupted", status="failed", code=2), start(), item("next"))
    counts, _ = collect_window(str(transcript), state, slot=SLOT)
    assert counts["tool_delta"] == 2


@pytest.mark.parametrize("status,code,error,never,unknown", [
    ("completed", 0, 0, 0, 0), ("failed", 2, 1, 0, 0),
    ("declined", None, 1, 1, 0), ("cancelled", None, 0, 0, 1),
    ("failed", None, 0, 0, 1), ("completed", True, 0, 0, 1),
    ({"malformed": True}, None, 0, 0, 1),
    ("cancelled", 0, 0, 0, 1), ("failed", 0, 0, 0, 1),
    ("bogus", 0, 0, 0, 1), ("inProgress", 1, 0, 0, 1),
])
def test_missing_exit_status_is_not_success(transcript, status, code, error, never, unknown):
    append(transcript, item("x", status=status, code=code))
    counts, _ = collect_window(str(transcript), None, slot=SLOT)
    assert counts["tool_error_delta"] == error
    assert counts["tool_not_executed_delta"] == never
    assert counts["tool_unknown_status_delta"] == unknown
    assert counts["window_truncated"] is bool(unknown)


def test_mcp_application_text_is_not_transport_error(transcript):
    append(transcript, item("x", kind="McpToolCall", result={"isError": False,
           "content": [{"text": "error: exit code 2"}]}))
    counts, _ = collect_window(str(transcript), None, slot=SLOT)
    assert counts["tool_error_delta"] == 0


def test_foreign_rollout_and_child_items_are_not_attributed(transcript):
    append(transcript, item("own"))
    foreign = item("child")
    foreign["payload"]["thread_id"] = "other-session"
    append(transcript, foreign)
    counts, _ = collect_window(str(transcript), None, slot=SLOT)
    assert counts["tool_delta"] == 1
    assert collect_window(str(transcript), None, slot="other-session") is None


def test_legacy_rollout_is_unsupported_not_zero(transcript):
    append(transcript, {"type": "response_item", "payload": {
        "type": "function_call_output", "output": "Exit code: 0"}})
    assert collect_window(str(transcript), None, slot=SLOT) is None


def test_partial_line_waits_for_next_window(transcript):
    append(transcript, item("first"))
    encoded = json.dumps(item("second"))
    with transcript.open("a") as f:
        f.write(encoded[:30])
    counts, state = collect_window(str(transcript), None, slot=SLOT)
    assert counts["tool_delta"] == 1
    with transcript.open("a") as f:
        f.write(encoded[30:] + "\n")
    counts, _ = collect_window(str(transcript), state, slot=SLOT)
    assert counts["tool_delta"] == 1


def test_unknown_native_activity_marks_incomplete_coverage(transcript):
    append(transcript, item("known"), item("x", kind="FutureTool"))
    counts, _ = collect_window(str(transcript), None, slot=SLOT)
    assert counts["tool_delta"] == 1
    assert counts["window_truncated"] is True


def test_delivery_counts_only_is_bound_and_at_most_once(transcript, tmp_path):
    append(transcript, item("x", status="failed", code=2))
    sent = []
    def sender(url, body):
        sent.append(body)
        return False
    payload = json.dumps({"session_id": SLOT, "transcript_path": str(transcript)})
    args = dict(agent_uuid="agent-id", client_session_id="binding", slot=SLOT,
                server_url="http://localhost", home=tmp_path, host="codex", sender=sender)
    assert run(payload, **args) == "post_failed"
    assert run(payload, **args) == "skip_nothing_new"
    assert len(sent) == 1
    assert sent[0]["host_family"] == "codex"
    assert sent[0]["tool_error_delta"] == 1
    wire = json.dumps(sent)
    for private in ("private", SLOT, str(transcript)):
        assert private not in wire


def test_kill_switch_does_not_read_or_advance(transcript, tmp_path, monkeypatch):
    monkeypatch.setenv("UNITARES_TOOL_OUTCOMES", "off")
    assert run("{}", agent_uuid="a", client_session_id="c", slot=SLOT,
               server_url="x", home=tmp_path, host="codex") == "skip_disabled"
    assert not (tmp_path / ".unitares").exists()


def test_rotated_rollout_rechecks_identity_and_resets_cursor(transcript):
    append(transcript, item("first"))
    _, state = collect_window(str(transcript), None, slot=SLOT)
    transcript.rename(transcript.with_suffix(".old"))
    append(transcript, {"type": "session_meta", "payload": {"id": "foreign"}},
           start(), item("second"))
    assert collect_window(str(transcript), state, slot=SLOT) is None
    transcript.write_text("")
    append(transcript, {"type": "session_meta", "payload": {"id": SLOT}},
           start(), item("second"))
    counts, _ = collect_window(str(transcript), state, slot=SLOT)
    assert counts["tool_delta"] == 1
    assert counts["window_truncated"] is True


def test_bounded_first_read_without_turn_boundary_is_unavailable(transcript):
    append(transcript, item("x", aggregated_output="x" * 3000), item("y"))
    assert collect_window(str(transcript), None, slot=SLOT, read_bound=1000) is None


def test_symlink_transcript_is_not_followed(transcript, tmp_path):
    link = tmp_path / "link.jsonl"
    link.symlink_to(transcript)
    assert collect_window(str(link), None, slot=SLOT) is None


def test_messages_do_not_establish_tool_coverage(transcript):
    append(transcript, item("message", kind="AgentMessage"),
           {"type": "response_item", "payload": {
               "type": "function_call_output", "call_id": "legacy", "output": "done"}})
    assert collect_window(str(transcript), None, slot=SLOT) is None


def test_later_unmatched_tool_result_marks_incomplete_coverage(transcript):
    append(transcript, item("known"))
    _, state = collect_window(str(transcript), None, slot=SLOT)
    append(transcript, {"type": "response_item", "payload": {
        "type": "function_call_output", "call_id": "not-itemized", "output": "done"}})
    counts, _ = collect_window(str(transcript), state, slot=SLOT)
    assert counts["tool_delta"] == 0
    assert counts["window_truncated"] is True


def test_code_mode_wrapper_across_windows_does_not_claim_missing_nested_result(transcript):
    append(transcript, item("nested"), {"type": "response_item", "payload": {
        "type": "custom_tool_call", "name": "functions.exec", "call_id": "wrapper"}})
    _, state = collect_window(str(transcript), None, slot=SLOT)
    append(transcript, {"type": "response_item", "payload": {
        "type": "custom_tool_call_output", "call_id": "wrapper", "output": "done"}})
    counts, _ = collect_window(str(transcript), state, slot=SLOT)
    assert counts["tool_delta"] == 0
    assert counts["window_truncated"] is False


def test_codex_delivery_has_bounded_http_timeout(transcript, tmp_path, monkeypatch):
    import runtime_observer
    observed = []
    def post(url, body, *, timeout):
        observed.append(timeout)
        return True
    monkeypatch.setattr(runtime_observer, "_post_runtime", post)
    append(transcript, item("x"))
    payload = json.dumps({"session_id": SLOT, "transcript_path": str(transcript)})
    assert run(payload, agent_uuid="a", client_session_id="c", slot=SLOT,
               server_url="x", home=tmp_path, host="codex") == "sent"
    assert observed == [2.0]
