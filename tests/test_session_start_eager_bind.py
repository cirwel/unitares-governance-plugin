"""SessionStart binds the process at startup when harness onboarding is on.

Live 2026-10-14: the Stop hook minted an identity after the first turn without
the agent knowing, and the agent's later start_session(force_new=true) minted a
second one for the same process. Binding at startup and naming the identity in
the context closes that blind spot.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from _session_lookup import _slot_filename  # noqa: E402
from tests.test_post_stop_hook import LazyOnboardHandler  # noqa: E402
from tests.test_session_start_checkin import (  # noqa: E402
    _ReusableTCPServer,
    _context,
    _run_hook,
)

UUID = "11111111-2222-4333-8444-555555555555"
CSID = "agent-11111111-222"
SLOT = "eager-slot-1234"


def _serve(tmp_path, **kwargs):
    LazyOnboardHandler.calls = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), LazyOnboardHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        stdout, _ = _run_hook(tmp_path, f"http://127.0.0.1:{port}", **kwargs)
    finally:
        srv.shutdown()
        thread.join(timeout=2)
    onboards = [c for c in LazyOnboardHandler.calls if c.get("name") == "onboard"]
    return stdout, onboards


def test_startup_binds_and_names_the_identity(tmp_path):
    stdout, onboards = _serve(tmp_path, claude_session_id=SLOT)
    assert len(onboards) == 1
    args = onboards[0]["arguments"]
    assert args["onboard_origin"] == "harness_backstop"
    assert args["force_new"] is True
    assert "parent_agent_id" not in args

    cached = json.loads((tmp_path / ".unitares" / _slot_filename(SLOT)).read_text())
    assert cached["uuid"] == UUID

    context = _context(stdout)
    assert UUID in context and CSID in context
    assert "already bound" in context
    assert "Do NOT call\nstart_session(force_new=true)" in context
    assert "SessionStart has not created an identity yet" not in context


def test_bound_session_drops_the_onboard_fresh_lineage_hint(tmp_path):
    other = tmp_path / ".unitares" / "session-someone-else.json"
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps({
        "uuid": "99999999-8888-4777-8666-555555555555",
        "slot": "someone-else",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }))
    # Control: without the eager bind the hint names the other slot's uuid.
    unbound, _ = _serve(
        tmp_path, claude_session_id=SLOT, extra_env={"UNITARES_EAGER_BIND": "off"})
    assert "99999999-8888-4777-8666-555555555555" in _context(unbound)
    stdout, onboards = _serve(tmp_path, claude_session_id=SLOT)
    assert len(onboards) == 1
    context = _context(stdout)
    assert "99999999-8888-4777-8666-555555555555" not in context
    # The control run left a nudge marker; it must not hide the new binding.
    assert UUID in context and "already bound" in context


def test_auto_onboard_off_mints_nothing(tmp_path):
    stdout, onboards = _serve(
        tmp_path, claude_session_id=SLOT, extra_env={"UNITARES_AUTO_ONBOARD": "off"})
    assert onboards == []
    assert "SessionStart has not created an identity yet" in _context(stdout)


def test_eager_bind_off_keeps_the_lazy_path(tmp_path):
    stdout, onboards = _serve(
        tmp_path, claude_session_id=SLOT, extra_env={"UNITARES_EAGER_BIND": "off"})
    assert onboards == []
    assert "Stop hook will lazily create" in _context(stdout)


def test_spawned_child_with_parent_candidate_declares_its_own_lineage(tmp_path):
    _, onboards = _serve(
        tmp_path, claude_session_id=SLOT,
        extra_env={"UNITARES_PARENT_AGENT_ID": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"})
    assert onboards == []


def test_existing_slot_cache_is_not_reminted(tmp_path):
    cache = tmp_path / ".unitares" / _slot_filename(SLOT)
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"uuid": UUID, "client_session_id": CSID}))
    _, onboards = _serve(tmp_path, claude_session_id=SLOT)
    assert onboards == []


def test_orchestrated_anchor_is_left_to_resume(tmp_path):
    _, onboards = _serve(
        tmp_path, claude_session_id=SLOT,
        extra_env={"UNITARES_CLIENT_SESSION_ID": "agent:/thread-x",
                   "UNITARES_ORCHESTRATED": "1"})
    assert onboards == []


def test_home_mirror_counts_as_bound(tmp_path):
    home = tmp_path
    workspace = tmp_path / "elsewhere"
    workspace.mkdir()
    mirror = home / ".unitares" / _slot_filename(SLOT)
    mirror.parent.mkdir(parents=True)
    mirror.write_text(json.dumps({"uuid": UUID, "client_session_id": CSID, "slot": SLOT}))
    _, onboards = _serve(tmp_path, claude_session_id=SLOT, cwd=workspace)
    assert onboards == []


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "on", "yes"])
def test_every_disable_value_is_honoured(tmp_path, value):
    _, onboards = _serve(
        tmp_path, claude_session_id=SLOT,
        extra_env={"UNITARES_DISABLE_AUTO_ONBOARD": value})
    assert onboards == []


@pytest.mark.parametrize("source", ["resume", "compact"])
def test_resume_and_compact_leave_minting_to_the_stop_hook(tmp_path, source):
    _, onboards = _serve(
        tmp_path, hook_payload={"session_id": SLOT, "source": source})
    assert onboards == []


@pytest.mark.parametrize("source", ["startup", "clear"])
def test_new_host_sessions_bind(tmp_path, source):
    _, onboards = _serve(
        tmp_path, hook_payload={"session_id": SLOT, "source": source})
    assert len(onboards) == 1


def test_home_only_binding_is_shown_not_reminted(tmp_path):
    workspace = tmp_path / "elsewhere"
    workspace.mkdir()
    mirror = tmp_path / ".unitares" / _slot_filename(SLOT)
    mirror.parent.mkdir(parents=True)
    mirror.write_text(json.dumps({"uuid": UUID, "client_session_id": CSID, "slot": SLOT}))
    stdout, onboards = _serve(tmp_path, claude_session_id=SLOT, cwd=workspace)
    assert onboards == []
    context = _context(stdout)
    assert UUID in context and "already bound" in context
    assert "start_session(force_new=true)          #" not in context


def test_bind_overrides_compact_mode_for_an_unusable_recent_cache(tmp_path):
    cache = tmp_path / ".unitares" / _slot_filename(SLOT)
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"uuid": UUID, "slot": SLOT}))  # no client_session_id
    stdout, onboards = _serve(tmp_path, claude_session_id=SLOT)
    assert len(onboards) == 1
    context = _context(stdout)
    assert UUID in context and CSID in context and "already bound" in context


def test_resume_marks_the_inherited_cache(tmp_path):
    cache = tmp_path / ".unitares" / _slot_filename(SLOT)
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"uuid": UUID, "client_session_id": CSID, "slot": SLOT}))
    _, onboards = _serve(tmp_path, hook_payload={"session_id": SLOT, "source": "resume"})
    assert onboards == []
    data = json.loads(cache.read_text())
    assert data["prior_process"] is True
    assert data["uuid"] == UUID and data["client_session_id"] == CSID


def test_resume_with_home_only_binding_writes_a_complete_marked_cache(tmp_path):
    workspace = tmp_path / "elsewhere"
    workspace.mkdir()
    mirror = tmp_path / ".unitares" / _slot_filename(SLOT)
    mirror.parent.mkdir(parents=True)
    mirror.write_text(json.dumps({"uuid": UUID, "client_session_id": CSID, "slot": SLOT}))
    _, onboards = _serve(tmp_path, cwd=workspace,
                         hook_payload={"session_id": SLOT, "source": "resume"})
    assert onboards == []
    data = json.loads((workspace / ".unitares" / _slot_filename(SLOT)).read_text())
    assert data["prior_process"] is True
    assert data["uuid"] == UUID and data["client_session_id"] == CSID


def test_bound_uuid_is_exported_for_later_commands(tmp_path):
    env_file = tmp_path / "claude-env.sh"
    env_file.write_text("")
    _, onboards = _serve(tmp_path, claude_session_id=SLOT,
                         extra_env={"CLAUDE_ENV_FILE": str(env_file)})
    assert len(onboards) == 1
    assert env_file.read_text() == f"export UNITARES_AGENT_UUID={UUID}\n"


def test_unbound_session_exports_nothing(tmp_path):
    env_file = tmp_path / "claude-env.sh"
    env_file.write_text("")
    _serve(tmp_path, claude_session_id=SLOT,
           extra_env={"CLAUDE_ENV_FILE": str(env_file), "UNITARES_AUTO_ONBOARD": "off"})
    assert env_file.read_text() == ""


def test_codex_host_does_not_write_a_claude_env_file(tmp_path):
    env_file = tmp_path / "claude-env.sh"
    env_file.write_text("")
    _serve(tmp_path, claude_session_id=SLOT, host="codex",
           extra_env={"CLAUDE_ENV_FILE": str(env_file)})
    assert env_file.read_text() == ""
