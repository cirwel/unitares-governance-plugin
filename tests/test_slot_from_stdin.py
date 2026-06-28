"""Unit tests for scripts/_slot_from_stdin.py — shared slot-extraction
helper introduced in S20.1a (2026-04-26) to reduce drift between
hooks/post-checkin and hooks/post-edit, which previously inlined the
same Python heredoc."""

from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from _slot_from_stdin import slot_from_payload  # noqa: E402

SCRIPT = PLUGIN_ROOT / "scripts" / "_slot_from_stdin.py"


def test_extracts_session_id_unchanged_when_safe():
    assert slot_from_payload('{"session_id":"abc-123"}') == "abc-123"


def test_returns_empty_for_missing_session_id():
    assert slot_from_payload('{}') == ""
    assert slot_from_payload('{"other":"field"}') == ""


def test_returns_empty_for_empty_or_invalid_payload():
    assert slot_from_payload("") == ""
    assert slot_from_payload("not json") == ""
    assert slot_from_payload("[]") == ""  # not a dict
    assert slot_from_payload("null") == ""


def test_returns_empty_for_blank_session_id():
    assert slot_from_payload('{"session_id":""}') == ""
    assert slot_from_payload('{"session_id":"   "}') == ""
    assert slot_from_payload('{"session_id":null}') == ""


def test_sanitizes_unsafe_characters():
    # Anything outside [a-zA-Z0-9_-] becomes an underscore.
    assert slot_from_payload('{"session_id":"foo bar"}') == "foo_bar"
    assert slot_from_payload('{"session_id":"a/b/c"}') == "a_b_c"
    assert slot_from_payload('{"session_id":"x.y@z"}') == "x_y_z"


def test_truncates_to_64_chars():
    long_id = "a" * 200
    out = slot_from_payload(json.dumps({"session_id": long_id}))
    assert len(out) == 64
    assert out == "a" * 64


def test_codex_transcript_path_fallback_hashes_to_safe_slot():
    transcript = "/home/user/.codex/sessions/2026/06/18/rollout-long-common-prefix.jsonl"
    expected = "codex-transcript_path-" + hashlib.sha256(transcript.encode()).hexdigest()[:16]
    assert slot_from_payload(json.dumps({"transcript_path": transcript})) == expected


def test_codex_thread_id_fallback_wins_before_transcript_path():
    thread_id = "thread:abc/123"
    transcript = "/tmp/other.jsonl"
    expected = "codex-thread_id-" + hashlib.sha256(thread_id.encode()).hexdigest()[:16]
    assert slot_from_payload(json.dumps({
        "thread_id": thread_id,
        "transcript_path": transcript,
    })) == expected


def test_cli_round_trip_via_stdin():
    """The CLI is what hooks call via subprocess. Ensure it round-trips
    a typical Claude Code SessionStart payload."""
    payload = json.dumps({
        "session_id": "claude-12345-abc",
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
    })
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "claude-12345-abc"


def test_cli_emits_empty_on_no_session_id():
    """Hooks rely on empty stdout to mean "skip slot-scoped writes" —
    the CLI must not invent a default."""
    payload = json.dumps({"tool_name": "Edit"})
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""


def test_cli_matches_session_lookup_slot_filename():
    """The sanitization rule must stay byte-identical with
    _session_lookup._slot_filename so hooks that derive the slot via this
    helper end up reading/writing the same file as session-start and
    post-identity."""
    from _session_lookup import _slot_filename  # local import

    raw = "abc-123_xyz"
    derived = slot_from_payload(json.dumps({"session_id": raw}))
    expected_filename = _slot_filename(raw)
    assert expected_filename == f"session-{derived}.json"
