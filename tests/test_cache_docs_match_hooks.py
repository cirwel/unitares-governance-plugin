"""Agent-facing cache docs must describe what the hooks and hosts actually do.

CODEX_START.md is read by Codex, which installs none of the Claude slash
commands in commands/. commands/checkin.md describes the post-checkin hook,
which credits a successful call that names no `client_session_id` to the
slot's binding (see tests/test_post_checkin_hook.py), so the doc must not
claim the call has to carry that binding.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_codex_start_points_at_no_claude_slash_command() -> None:
    text = (ROOT / "CODEX_START.md").read_text(encoding="utf-8")
    commands = sorted(p.stem for p in (ROOT / "commands").glob("*.md"))
    assert commands
    named = [c for c in commands if re.search(rf"`/{re.escape(c)}\b", text)]
    assert named == [], (
        f"CODEX_START.md tells Codex to run Claude-only commands: {named}"
    )


def test_checkin_doc_matches_argless_post_checkin_behaviour() -> None:
    text = " ".join(
        (ROOT / "commands" / "checkin.md").read_text(encoding="utf-8").split()
    )
    assert "only when the successful check-in carried" not in text
    assert "a call that named none is credited to the slot's binding" in text


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_codex_start_first_bind_keeps_the_conversation_anchor() -> None:
    # In an anchored conversation the pre-governance-call hook attaches the
    # anchor only to a bare start_session(); force_new=true skips it and mints
    # a second identity (tests/test_governance_call_inject.py). The no-binding
    # guidance must say so rather than send every unbound process to force_new.
    hook = _flat(ROOT / "hooks" / "session-start")
    assert "(anchored session)" in hook
    assert "call `start_session()` with no arguments" in hook.replace("\\`", "`")

    text = _flat(ROOT / "CODEX_START.md")
    assert "the session-start note says `anchored session`" in text
    assert (
        "bind as step 1 of the raw tool flow above says: `start_session()` with no "
        "arguments in an anchored conversation, otherwise `start_session(force_new=true)`"
    ) in text
    assert (
        "When this process has no known binding, call `start_session(force_new=true)`"
        not in text
    )


def test_checkin_doc_states_the_milestone_revision_check() -> None:
    # post-checkin resets through the pre-call milestone snapshot, so an edit
    # recorded while the call is in flight keeps the milestone pending
    # (tests/test_post_checkin_hook.py::test_hook_preserves_edit_made_during_manual_checkin).
    hook = (ROOT / "hooks" / "post-checkin").read_text(encoding="utf-8")
    assert "reset-milestone" in hook and '--snapshot-id "${TOOL_USE_ID}"' in hook

    text = _flat(ROOT / "commands" / "checkin.md")
    assert (
        "resets the slot's milestone only if the milestone's revision still equals "
        "the one in the snapshot"
    ) in text
    assert (
        "an edit that lands while the call is in flight keeps the milestone pending"
        in text
    )
    assert "the slot's cache was not written again while the call was in flight" in text
