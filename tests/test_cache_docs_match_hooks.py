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
