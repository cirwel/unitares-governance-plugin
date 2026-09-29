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
            online=True, cwd=None, plugin_root=None):
    """Render one variant and return its additionalContext."""
    root = plugin_root if plugin_root is not None else PLUGIN_ROOT
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
            [str(root / "hooks" / "session-start"), "--host", host],
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
    # The Codex pointer names SKILL.md paths under this checkout.
    text = text.replace(str(PLUGIN_ROOT), "<PLUGIN_ROOT>")
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


def test_claude_pointer_invokes_the_skill_and_codex_pointer_gives_the_path(tmp_path):
    """Claude reads a skill through its Skill tool; Codex opens SKILL.md. A
    Codex skill id resolves to the plugin version the process started with,
    which a marketplace refresh can remove, so its pointer names the file
    under the hook's own PLUGIN_ROOT."""
    claude = _render(tmp_path, session_id="golden-pointer-wording-claude")
    codex = _render(tmp_path, host="codex", session_id="golden-pointer-wording-codex")
    assert "invoke `unitares-governance:governance-fundamentals`" in claude
    assert "SKILL.md" not in claude
    for skill in ("governance-fundamentals", "governance-lifecycle"):
        path = PLUGIN_ROOT / "skills" / skill / "SKILL.md"
        assert path.is_file()
        assert f"read {path}" in codex or f"or {path}" in codex, skill
    assert "invoke `" not in codex


def test_a_plugin_without_the_skill_file_is_not_pointed_at_it(tmp_path):
    """The pointer is only as good as the file it names. A plugin tree
    without skills/governance-fundamentals takes the excerpt path instead
    (server fetch, then the bundled mirror; here neither has the skill, so no
    Fundamentals block at all), and never names a SKILL.md that is not
    there. The server-fetched excerpt itself is covered by the opt-in tests
    in test_session_start_checkin.py."""
    import shutil

    root = tmp_path / "plugin"
    for part in ("hooks", "scripts", "config"):
        if (PLUGIN_ROOT / part).exists():
            shutil.copytree(PLUGIN_ROOT / part, root / part)
    (root / "skills").mkdir()
    for host in ("claude", "codex"):
        rendered = _render(tmp_path, host=host, plugin_root=root,
                           session_id=f"golden-no-skill-{host}")
        assert "Governance Fundamentals (via skill)" not in rendered, host
        assert "SKILL.md" not in rendered, host


# Every byte of a fresh Codex SessionStart lands in that session's context.
# With the excerpt inlined it was 6,151 B through this harness; the pointer
# brings it to about 2.8 KB. The budget leaves room for ordinary prose edits
# and fails if an excerpt-sized block comes back.
CODEX_FRESH_SESSION_START_BUDGET = 4000


def test_codex_fresh_session_start_stays_within_budget(tmp_path):
    size = len(_render(tmp_path, host="codex").encode("utf-8"))
    assert size <= CODEX_FRESH_SESSION_START_BUDGET, (
        f"Codex fresh SessionStart is {size} B, over the "
        f"{CODEX_FRESH_SESSION_START_BUDGET} B budget; it lands in every fresh "
        "Codex session's context."
    )


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
