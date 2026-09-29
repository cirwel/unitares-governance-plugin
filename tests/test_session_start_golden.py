"""Golden snapshots of the RENDERED SessionStart preamble.

Source review keeps missing this surface. The hook is ~750 lines of bash
that assembles one of several agent-facing variants; what matters is the
text that actually ships, and which variant ships when. Two real misses
this file exists to prevent:

1. A judgment that a paragraph was redundant with the Fundamentals skill --
   when that skill contains ZERO identity terms, and the paragraph was the
   sole carrier of `parent_agent_id`, `lineage`, and `resume` in the
   rendered full variant. Deleting it would have dropped all three to zero
   with no test failing.
2. A plan to gate content by model capability -- when line 25 hard-gates
   the whole hook to claude|codex, so the weak-model consumer it was aimed
   at never sees this text at all.

Neither is visible while reading the source top-to-bottom. Both are
obvious in the rendered output.

Two layers here, deliberately:

- **Byte goldens** catch "the text changed" and put the diff in review.
- **Term assertions** catch "the CONCEPT left", which survives rewording.
  A golden alone would happily accept a rewrite that drops
  `parent_agent_id` entirely, as long as you regenerated it.

Regenerate after an intentional change:

    UPDATE_SESSION_START_GOLDENS=1 python3 -m pytest tests/test_session_start_golden.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

# Reuse the established harness rather than standing up a parallel one.
sys.path.insert(0, str(Path(__file__).parent))
from test_session_start_checkin import (  # noqa: E402
    PLUGIN_ROOT,
    RecordingHandler,
    _ReusableTCPServer,
)

GOLDEN_DIR = Path(__file__).parent / "golden" / "session_start"
UPDATE = os.getenv("UPDATE_SESSION_START_GOLDENS") == "1"

# A fixed UUID so the lineage-hint variant renders deterministically.
FAKE_PARENT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _render(tmp_path, *, host="claude", extra_env=None, session_id="golden-slot-0001",
            online=True, cwd=None):
    """Render one variant and return its additionalContext."""
    RecordingHandler.calls = []
    workdir = cwd if cwd is not None else tmp_path
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
        "PWD": str(workdir),
        "USER": "testuser",
        # Git-sourced sibling briefing depends on the checkout it runs in.
        # Deterministic goldens must not embed the developer's worktrees.
        "UNITARES_HOOK_SKIP_WORKSPACE_BRIEFING": "1",
        # The file-lease status line depends on whether a lease plane runs on
        # this machine; tests/test_file_lease_hook.py pins it separately.
        "UNITARES_FILE_LEASES_ENABLED": "0",
    }
    if extra_env:
        env.update(extra_env)

    srv = thread = None
    if online:
        srv = _ReusableTCPServer(("127.0.0.1", 0), RecordingHandler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        env["UNITARES_SERVER_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
    else:
        # Reserved-but-unbound port: the health probe fails fast.
        env["UNITARES_SERVER_URL"] = "http://127.0.0.1:1"

    try:
        result = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "session-start"), "--host", host],
            env=env, cwd=str(workdir),
            input=json.dumps({"session_id": session_id}),
            text=True, capture_output=True, timeout=30, check=False,
        )
    finally:
        if srv is not None:
            srv.shutdown()
            thread.join(timeout=2)

    payload = json.loads(result.stdout)
    return payload["hookSpecificOutput"]["additionalContext"]


def _normalize(text: str) -> str:
    """Strip content that legitimately varies run to run.

    The Fundamentals excerpt is the skill body, which has its own freshness
    gate and changes independently of this hook. What the golden needs to
    pin is WHETHER a host gets an excerpt or a pointer -- not the skill's
    current wording.
    """
    text = re.sub(
        r"(--- Governance Fundamentals \(excerpt\)[^\n]*---\n).*\Z",
        r"\1<EXCERPT BODY ELIDED>",
        text,
        flags=re.S,
    )
    return text.strip() + "\n"


def _assert_golden(name: str, rendered: str):
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = GOLDEN_DIR / f"{name}.txt"
    normalized = _normalize(rendered)
    if UPDATE or not path.exists():
        path.write_text(normalized)
        if not UPDATE:
            pytest.skip(f"created missing golden {path.name}; re-run to assert")
        return
    expected = path.read_text()
    assert normalized == expected, (
        f"Rendered SessionStart '{name}' drifted from its golden.\n"
        f"If intentional: UPDATE_SESSION_START_GOLDENS=1 pytest {Path(__file__).name}\n"
        f"and review the diff -- this is agent-facing text."
    )


# --------------------------------------------------------------------------
# Byte goldens, one per rendered variant
# --------------------------------------------------------------------------

def test_golden_full_claude(tmp_path):
    _assert_golden("full_claude", _render(tmp_path))


def test_golden_full_codex(tmp_path):
    """Codex loads plugin skills through its manifest, so it gets the same
    skill pointer Claude does. Its host-specific lines (recovery route,
    lazy-onboarding host name) still differ, so it keeps its own golden."""
    _assert_golden("full_codex", _render(tmp_path, host="codex"))


def test_golden_full_with_env_lineage(tmp_path):
    _assert_golden("full_env_lineage", _render(
        tmp_path,
        extra_env={"UNITARES_PARENT_AGENT_ID": FAKE_PARENT,
                   "UNITARES_SPAWN_REASON": "subagent"},
    ))


def test_golden_anchored(tmp_path):
    """Orchestrated conversations get the opposite instruction -- do NOT
    force_new. A change that unified the variants would break this."""
    _assert_golden("anchored", _render(
        tmp_path,
        extra_env={"UNITARES_ORCHESTRATED": "1",
                   "UNITARES_CLIENT_SESSION_ID": "agent-anchored-0001"},
    ))


def test_golden_offline(tmp_path):
    _assert_golden("offline", _render(tmp_path, online=False))


def test_golden_offline_codex(tmp_path):
    _assert_golden("offline_codex", _render(tmp_path, host="codex", online=False))


# --------------------------------------------------------------------------
# Term assertions -- what a golden cannot catch
# --------------------------------------------------------------------------

# Each term is the sole vocabulary for an affordance the agent cannot
# otherwise discover. Dropping one does not fail a regenerated golden.
REQUIRED_FULL_TERMS = [
    "force_new",        # the explicit opt-in invariant 2 requires
    "parent_agent_id",  # the lineage affordance; nothing else names it
    "lineage",
    "co-location",      # the negation that stops co-located false ancestry
    "sync_state",
]


@pytest.mark.parametrize("term", REQUIRED_FULL_TERMS)
def test_full_variant_still_carries_affordance(tmp_path, term):
    rendered = _render(tmp_path)
    assert term in rendered, (
        f"The rendered full variant no longer mentions {term!r}. "
        "An agent cannot use an affordance it is never told exists; this is "
        "how a dispatched agent lands in the no-lineage ghost population."
    )


SKILL_POINTER_NAMES = (
    "unitares-governance:governance-fundamentals",
    "unitares-governance:governance-lifecycle",
)


@pytest.mark.parametrize("online", [True, False], ids=["online", "offline"])
@pytest.mark.parametrize("host", ["claude", "codex"])
def test_both_hosts_get_the_skill_pointer_not_the_excerpt(tmp_path, host, online):
    """Both hosts load the bundled skills on demand, so neither gets the
    80-line excerpt; each gets the pointer naming both skills. The offline
    path carries the pointer too."""
    rendered = _render(tmp_path, host=host, online=online,
                       session_id=f"golden-pointer-{host}-{online}")
    assert "--- Governance Fundamentals (via skill) ---" in rendered
    for name in SKILL_POINTER_NAMES:
        assert name in rendered, f"{host} pointer no longer names {name}"
    assert "Governance Fundamentals (excerpt)" not in rendered
    assert "Governance Fundamentals (reference)" not in rendered


# Codex fresh SessionStart, rendered through this harness on master 0f9ec6c
# while it still inlined the Fundamentals excerpt: 6,151 bytes, 4,280 of them
# the excerpt block. The pointer is ~460 bytes.
CODEX_FRESH_BYTES_WITH_EXCERPT = 6151
MIN_POINTER_SAVING_BYTES = 3500


def test_codex_fresh_output_is_at_least_3500_bytes_smaller(tmp_path):
    rendered = _render(tmp_path, host="codex")
    size = len(rendered.encode("utf-8"))
    budget = CODEX_FRESH_BYTES_WITH_EXCERPT - MIN_POINTER_SAVING_BYTES
    assert size <= budget, (
        f"Codex fresh SessionStart is {size} B; the pointer swap is meant to "
        f"keep it at or under {budget} B ({MIN_POINTER_SAVING_BYTES} B below "
        f"the {CODEX_FRESH_BYTES_WITH_EXCERPT} B it was with the excerpt). "
        "Every byte here lands in each fresh Codex session's context."
    )


# The identity and lineage prose is a defence layer and must survive the
# pointer swap byte-for-byte. Pinned: the byte length and SHA-256 of
# everything the Codex host rendered ahead of the Fundamentals block on master
# 0f9ec6c (the fresh variant is the prefix of that commit's
# golden/session_start/full_codex.txt). Inspect the old text with
#   git show 0f9ec6c:tests/golden/session_start/full_codex.txt
CODEX_PROSE_BEFORE_POINTER = {
    "fresh": (
        {},
        1869,
        "e94b8b4d0c1899004e71359473bce6944abc91c53f98bf29073dc53c95fae8b8",
    ),
    "env_lineage": (
        {"UNITARES_PARENT_AGENT_ID": FAKE_PARENT,
         "UNITARES_SPAWN_REASON": "subagent"},
        2633,
        "8a0261c83b85afce7480605c42038218f29ff5e3dbdbdea64cc7c7d45d8677ab",
    ),
}


def _prose_before_fundamentals(rendered: str) -> str:
    head, sep, _ = rendered.partition("\n\n--- Governance Fundamentals")
    assert sep, "rendered SessionStart has no Fundamentals block"
    return head


@pytest.mark.parametrize("variant", sorted(CODEX_PROSE_BEFORE_POINTER))
def test_codex_lineage_prose_is_byte_identical_to_pre_pointer_golden(tmp_path, variant):
    extra_env, length, digest = CODEX_PROSE_BEFORE_POINTER[variant]
    prose = _prose_before_fundamentals(
        _render(tmp_path, host="codex", extra_env=extra_env)
    ).encode("utf-8")
    assert b"parent_agent_id" in prose and b"lineage" in prose.lower()
    assert (len(prose), hashlib.sha256(prose).hexdigest()) == (length, digest), (
        f"Codex {variant} identity/lineage prose changed ({len(prose)} B). "
        "It must stay byte-identical to the pre-pointer render; compare "
        "against git show 0f9ec6c:tests/golden/session_start/full_codex.txt."
    )


def test_codex_fresh_golden_keeps_the_pre_pointer_prose():
    """The committed golden is what reviewers read; pin that its prose
    prefix is the same pre-pointer text, not only the live render."""
    golden = (GOLDEN_DIR / "full_codex.txt").read_text()
    _, length, digest = CODEX_PROSE_BEFORE_POINTER["fresh"]
    prose = _prose_before_fundamentals(golden).encode("utf-8")
    assert (len(prose), hashlib.sha256(prose).hexdigest()) == (length, digest)


def test_identity_pointer_names_the_skill_that_has_the_ontology(tmp_path):
    """governance-fundamentals contains zero identity terms; the ontology
    lives in governance-lifecycle. Pointing identity questions at the
    former sends agents to EISV semantics."""
    rendered = _render(tmp_path)
    assert "governance-lifecycle" in rendered


def test_no_dangling_hint_reference_without_a_hint(tmp_path):
    """'see hint below' rendered in variants where no hint follows."""
    rendered = _render(tmp_path)
    assert "hint below" not in rendered, (
        "Dangling reference: the full variant mentions a hint that only "
        "renders when UNITARES_PARENT_AGENT_ID or a slot cache is present."
    )


def test_anchored_variant_does_not_tell_the_agent_to_force_new(tmp_path):
    rendered = _render(
        tmp_path,
        extra_env={"UNITARES_ORCHESTRATED": "1",
                   "UNITARES_CLIENT_SESSION_ID": "agent-anchored-0001"},
    )
    assert "Do NOT call" in rendered or "do NOT call" in rendered
