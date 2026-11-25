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


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_codex_start_no_binding_guidance_never_mints_over_an_injected_binding() -> None:
    # The pre-governance-call hook attaches a conversation anchor or the slot's
    # binding only to a start_session() without force_new
    # (tests/test_governance_call_inject.py), and u-run / the sidecar bind
    # before the agent runs. Unbound guidance must therefore send the agent to
    # a bare start_session(), not key on whether a startup note was seen, and
    # keep force_new=true for a deliberate fresh identity.
    text = _flat(ROOT / "CODEX_START.md")
    assert (
        "make the first governance call a bare `start_session()` with no "
        "`force_new`, and let the hook or sidecar supply any anchor or existing "
        "binding"
    ) in text
    assert (
        "`force_new=true` is only for a deliberate, operator-intended fresh identity"
    ) in text
    assert (
        "Sessions launched through `u-run` or the sidecar already have a binding "
        "the agent does not see and must not replace"
    ) in text
    assert "When this process has no known binding, bind as step 1" in text
    assert "session-start note says" not in text
    assert "otherwise `start_session(force_new=true)`" not in text
    assert "call `start_session(force_new=true)` once per fresh process" not in text
    assert "First run of a fresh process: `start_session(force_new=true)`" not in text
    assert "skill and call `start_session(force_new=true)`" not in text


def test_checkin_doc_states_only_what_post_checkin_keeps_exact() -> None:
    # post-checkin stamps and resets through the pre-call snapshot
    # (tests/test_post_checkin_hook.py). The doc states that rule and its
    # consequence without enumerating conditions it cannot keep exact.
    hook = (ROOT / "hooks" / "post-checkin").read_text(encoding="utf-8")
    assert "reset-milestone" in hook and '--snapshot-id "${TOOL_USE_ID}"' in hook

    text = _flat(ROOT / "commands" / "checkin.md")
    assert (
        "stamps the slot and resets its milestone only when its own pre-call "
        "snapshot is still current"
    ) in text
    assert "an overlapping check-in or edit can leave the milestone unreset" in text
    assert "only when all of these hold" not in text
    assert (
        "the slot's cache was not written again while the call was in flight"
        not in text
    )
    assert "the milestone's revision still equals the one in the snapshot" not in text


def test_no_command_tells_the_agent_to_write_the_session_cache() -> None:
    # hooks/post-identity keys the session slot by the host session id and
    # stores client_session_id inside the record (tests/test_post_identity_hook.py).
    # An agent knows only its client_session_id, so a hand `set session` lands
    # under a second key and leaves the slot the hooks read unchanged.
    offenders = [
        path.name
        for path in sorted((ROOT / "commands").glob("*.md"))
        if re.search(r"session_cache\.py\s+set\s+session", _flat(path))
    ]
    assert offenders == [], (
        f"commands telling the agent to write the session cache: {offenders}"
    )


def test_governance_start_leaves_the_slot_write_to_the_hook() -> None:
    text = _flat(ROOT / "commands" / "governance-start.md")
    assert "do not write the session cache yourself" in text
    assert "--slot=<client_session_id>" not in text
