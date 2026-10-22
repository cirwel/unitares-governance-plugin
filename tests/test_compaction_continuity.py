"""Exercise rendered compaction guidance and the next governance call together, per host."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_session_start_checkin import PLUGIN_ROOT, _context, _run_hook, _serve_and_run


UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
BINDING = "agent-compaction-test-binding"
SLOT = "codex-compaction-slot"


def _cache(root, slot=SLOT, **overrides):
    path = root / ".unitares" / f"session-{slot}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "uuid": UUID, "client_session_id": BINDING,
        "continuity_token": "must-never-appear-in-context",
        "slot": slot, "schema_version": 2, **overrides,
    }))
    return path


def _event(**overrides):
    return {"session_id": SLOT, "source": "compact", **overrides}


HOSTS = {"codex": "Codex", "claude": "Claude Code"}


def _assert_continuation(ctx, host="codex"):
    assert f"{HOSTS[host]} reported context compaction" in ctx
    assert f"existing governance identity `{UUID}`" in ctx
    assert "Continue using its client_session_id" in ctx
    assert "start_session(force_new=true)" not in ctx
    assert "prior process-instance" not in ctx
    assert "parent_agent_id=" not in ctx
    assert "Scan-newest" not in ctx
    assert BINDING not in ctx
    assert "must-never-appear-in-context" not in ctx


@pytest.mark.parametrize("host", sorted(HOSTS))
@pytest.mark.parametrize("location", ["workspace", "home"])
def test_repeated_compaction_preserves_binding_even_after_cache_prose_ttl(tmp_path, location, host):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = _cache(workspace if location == "workspace" else tmp_path)
    os.utime(path, (1, 1))  # A prose TTL must not decide identity continuity.
    original = path.read_bytes()
    for _ in range(2):
        stdout, calls = _serve_and_run(
            tmp_path, cwd=workspace, host=host, hook_payload=_event(),
            extra_env={"UNITARES_HOOK_COMPACT_TTL": "0"},
        )
        _assert_continuation(_context(stdout), host)
        assert str(path) in _context(stdout)
        assert calls == []  # No mint, rebind, check-in, or lineage write.
        assert path.read_bytes() == original

    # The follow-on tool uses the exact binding described by SessionStart.
    result = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts/governance_call_inject.py"),
         "--host", host],
        cwd=workspace, env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
        input=json.dumps({"session_id": SLOT,
                          "tool_name": "mcp__unitares-governance__check_working_state",
                          "tool_input": {}}),
        text=True, capture_output=True, check=True,
    )
    updated = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]
    assert updated["client_session_id"] == BINDING


@pytest.mark.parametrize("field", ["thread_id", "conversation_id", "transcript_path"])
def test_compaction_uses_the_same_codex_fallback_slot_as_tool_hooks(tmp_path, field):
    value = "stable-host-conversation"
    digest = hashlib.sha256(value.encode()).hexdigest()[:16]
    _cache(tmp_path, f"codex-{field}-{digest}")
    stdout, _ = _serve_and_run(
        tmp_path, host="codex", hook_payload={field: value, "source": "compact"},
    )
    _assert_continuation(_context(stdout))


@pytest.mark.parametrize("host", sorted(HOSTS))
@pytest.mark.parametrize("case", ["missing", "uuid_only", "malformed", "invalid_mirror", "slotless"])
def test_compaction_never_substitutes_another_slot_for_missing_proof(tmp_path, case, host):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = _cache(workspace, "other-session")
    event = _event()
    if case == "uuid_only":
        _cache(workspace, client_session_id="")
    elif case == "malformed":
        _cache(workspace).write_text("{bad json")
    elif case == "invalid_mirror":
        _cache(tmp_path)
        authority = tmp_path / ".unitares-cache-authority" / f"session-{SLOT}.json"
        authority.parent.mkdir()
        authority.write_text(json.dumps({"mirror_valid": False, "generation": 7}))
    elif case == "slotless":
        event = {"source": "compact"}
    stdout, calls = _serve_and_run(
        tmp_path, cwd=workspace, host=host, hook_payload=event,
        extra_env={"UNITARES_PARENT_AGENT_ID": UUID, "UNITARES_SPAWN_REASON": "subagent"},
    )
    ctx = _context(stdout)
    assert "No usable binding was found for this exact session" in ctx
    assert "retained in your context" in ctx
    assert UUID not in ctx
    assert str(other) not in ctx
    assert "Scan-newest" not in ctx
    assert "parent_agent_id=" not in ctx
    assert calls == []


def test_offline_compaction_keeps_known_binding_guidance(tmp_path):
    _cache(tmp_path)
    stdout, calls = _run_hook(
        tmp_path, "http://127.0.0.1:1", host="codex", hook_payload=_event(),
    )
    ctx = _context(stdout)
    assert "OFFLINE" in ctx
    assert "No governance identity is currently bound" not in ctx
    _assert_continuation(ctx)
    assert calls == []


@pytest.mark.parametrize("host", sorted(HOSTS))
@pytest.mark.parametrize("source", ["startup", "resume", "clear", "unknown"])
def test_other_start_events_do_not_assert_same_process_continuity(tmp_path, source, host):
    _cache(tmp_path)
    stdout, _ = _serve_and_run(
        tmp_path, host=host, hook_payload=_event(source=source),
    )
    ctx = _context(stdout)
    assert "reported context compaction" not in ctx
    assert "existing governance identity" not in ctx
    assert "spawn_reason=\"explicit\"" in ctx


def test_codex_compaction_keeps_orchestrated_anchor_precedence(tmp_path):
    _cache(tmp_path)
    stdout, _ = _serve_and_run(
        tmp_path, host="codex", hook_payload=_event(),
        extra_env={"UNITARES_CLIENT_SESSION_ID": "agent-orchestrated",
                   "UNITARES_ORCHESTRATED": "1"},
    )
    ctx = _context(stdout)
    assert "ONLINE (anchored session)" in ctx
    assert "ONE identity per conversation" in ctx
    assert UUID not in ctx
