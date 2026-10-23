"""An idle slot binding is an earlier process's: the resume the host did not report."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import binding_activity  # noqa: E402

SLOT = "01a15b57-51ce-7a91-b5bc-f713a89ad9c7"
UUID = "727c452e-522c-43d7-b861-47dd77b5f145"
CSID = "agent-727c452e-522-idle-binding-test"
DAY = 24 * 3600


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    # The stamp lives under HOME; the hooks run with HOME=tmp_path as well.
    monkeypatch.setenv("HOME", str(tmp_path))


def _cache(workspace: Path, *, age_s: float = 0.0, **overrides) -> Path:
    path = workspace / ".unitares" / f"session-{SLOT}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "uuid": UUID, "client_session_id": CSID, "schema_version": 2,
        "slot": SLOT, "onboard_origin": "agent", **overrides,
    }))
    if age_s:
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
    return path


def _payload() -> str:
    return json.dumps({"session_id": SLOT, "last_assistant_message": "Done."})


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
        "UNITARES_SERVER_URL": "http://127.0.0.1:9",
        "UNITARES_CHECKIN_LOG": str(tmp_path / "checkins.log"),
        "UNITARES_AUTO_ONBOARD": "off",
        **extra,
    }


def _mint(workspace: Path, env: dict[str, str], host: str, *,
          tool: str = "start_session", tool_input: dict | None = None):
    hook_input = {
        "session_id": SLOT,
        "tool_name": f"mcp__unitares-governance__{tool}",
        "tool_input": {"force_new": True} if tool_input is None else tool_input,
    }
    return subprocess.run(
        [str(PLUGIN_ROOT / "hooks" / "pre-governance-call"), "--host", host],
        input=json.dumps(hook_input), text=True, capture_output=True,
        timeout=10, cwd=str(workspace), env=env,
    )


def _denied(result) -> bool:
    if not result.stdout.strip():
        return False
    out = json.loads(result.stdout)["hookSpecificOutput"]
    return out.get("permissionDecision") == "deny"


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_an_idle_binding_does_not_block_the_new_processs_mint(tmp_path, host):
    _cache(tmp_path, age_s=2 * DAY)
    assert not _denied(_mint(tmp_path, _env(tmp_path), host))


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_a_binding_with_a_recent_turn_still_blocks_a_second_mint(tmp_path, host):
    _cache(tmp_path, age_s=2 * DAY)
    binding_activity.touch(SLOT, UUID, CSID)
    assert _denied(_mint(tmp_path, _env(tmp_path), host))


def test_a_new_binding_blocks_a_second_mint(tmp_path):
    _cache(tmp_path)
    assert _denied(_mint(tmp_path, _env(tmp_path), "codex"))


def test_the_idle_threshold_follows_the_configured_session_lifetime(tmp_path):
    _cache(tmp_path, age_s=2 * DAY)
    env = _env(tmp_path, UNITARES_BINDING_IDLE_HOURS="72")
    assert _denied(_mint(tmp_path, env, "codex"))


@pytest.mark.parametrize("value", ["", "0", "-3", "soon"])
def test_a_bad_threshold_falls_back_to_the_default(monkeypatch, value):
    monkeypatch.setenv("UNITARES_BINDING_IDLE_HOURS", value)
    assert binding_activity.idle_hours() == binding_activity.DEFAULT_IDLE_HOURS


def test_mark_refreshes_a_fresh_binding(tmp_path):
    path = _cache(tmp_path)
    assert binding_activity.mark(str(tmp_path), _payload()) == "touched"
    stamp = binding_activity.activity_path(SLOT)
    assert stamp.parent == tmp_path / ".unitares"
    assert stamp.read_text().startswith(UUID + " ")
    assert CSID not in stamp.read_text()
    assert json.loads(path.read_text()).get("prior_process") is not True


def test_mark_records_the_unreported_resume_without_losing_the_binding(tmp_path):
    path = _cache(tmp_path, age_s=2 * DAY)
    assert binding_activity.mark(str(tmp_path), _payload()) == "marked_prior_process"
    data = json.loads(path.read_text())
    assert data["prior_process"] is True
    # The earlier identity stays recorded as the lineage candidate.
    assert data["uuid"] == UUID and data["client_session_id"] == CSID
    # An inherited cache is left alone afterwards.
    assert binding_activity.mark(str(tmp_path), _payload()) == "none"


def test_mark_never_overwrites_a_mint_that_landed_after_its_read(tmp_path, monkeypatch):
    # Claude's Stop is asynchronous: the next turn can mint between mark's
    # read and its write. The locked write sees the new binding and stands down.
    path = _cache(tmp_path, age_s=2 * DAY)
    real_is_idle = binding_activity.is_idle

    def idle_then_mint(*args, **kwargs):
        result = real_is_idle(*args, **kwargs)
        path.write_text(json.dumps({
            "uuid": "11111111-2222-4333-8444-555555555555",
            "client_session_id": "agent-new-mint", "schema_version": 2, "slot": SLOT,
        }))
        return result

    monkeypatch.setattr(binding_activity, "is_idle", idle_then_mint)
    assert binding_activity.mark(str(tmp_path), _payload()) == "none"
    data = json.loads(path.read_text())
    assert data["uuid"] == "11111111-2222-4333-8444-555555555555"
    assert data.get("prior_process") is not True


def test_mark_marks_a_binding_that_exists_only_in_the_home_mirror(tmp_path):
    # Bound from another directory: this workspace has no cache, the HOME
    # mirror is two days old. The marking must land on the HOME copy.
    home_copy = _cache(tmp_path, age_s=2 * DAY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert binding_activity.mark(str(workspace), _payload()) == "marked_prior_process"
    data = json.loads(home_copy.read_text())
    assert data["prior_process"] is True and data["uuid"] == UUID
    assert not (workspace / ".unitares" / f"session-{SLOT}.json").exists()


def test_a_rebind_under_a_new_proof_is_not_judged_by_the_old_stamp(tmp_path):
    binding_activity.touch(SLOT, UUID, CSID)
    stale = time.time() - 2 * DAY
    os.utime(binding_activity.activity_path(SLOT), (stale, stale))
    path = _cache(tmp_path, client_session_id="agent-727c452e-522-rebound")
    assert not binding_activity.is_idle(path, SLOT, UUID, "agent-727c452e-522-rebound")
    assert binding_activity.mark(str(tmp_path), _payload()) == "touched"


def test_mark_never_overwrites_a_mint_made_from_another_workspace(tmp_path):
    # Workspace X still holds A; a mint of B from workspace Y reached only the
    # HOME mirror. X's delayed Stop must not write A back over HOME's B.
    workspace = tmp_path / "x"
    workspace.mkdir()
    _cache(workspace, age_s=2 * DAY)
    home_copy = _cache(tmp_path, uuid="11111111-2222-4333-8444-555555555555",
                       client_session_id="agent-minted-in-y")
    assert binding_activity.mark(str(workspace), _payload()) == "none"
    assert json.loads(home_copy.read_text())["uuid"] == "11111111-2222-4333-8444-555555555555"


def test_mark_clears_a_legacy_token_rather_than_failing_every_stop(tmp_path):
    path = _cache(tmp_path, age_s=2 * DAY, continuity_token="v1.legacy-at-rest")
    assert binding_activity.mark(str(tmp_path), _payload()) == "marked_prior_process"
    data = json.loads(path.read_text())
    assert data["prior_process"] is True and data["continuity_token"] == ""


def test_another_identitys_stamp_does_not_count(tmp_path):
    path = _cache(tmp_path, age_s=2 * DAY)
    binding_activity.touch(SLOT, "11111111-2222-4333-8444-555555555555", CSID)
    assert binding_activity.is_idle(path, SLOT, UUID, CSID)


def test_other_hooks_cache_writes_are_not_activity(tmp_path):
    # post-edit and post-checkin rewrite the cache; only a bound turn end
    # (the stamp, once this identity has one) counts.
    path = _cache(tmp_path)
    binding_activity.touch(SLOT, UUID, CSID)
    stale = time.time() - 2 * DAY
    os.utime(binding_activity.activity_path(SLOT), (stale, stale))
    assert binding_activity.is_idle(path, SLOT, UUID, CSID)


def test_activity_follows_the_binding_across_workspace_and_home(tmp_path):
    # A live process's Stops ran in its workspace; a later hook runs from
    # another directory and resolves the HOME mirror, written two days ago.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _cache(workspace)
    _cache(tmp_path, age_s=2 * DAY)
    binding_activity.touch(SLOT, UUID, CSID)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert _denied(_mint(elsewhere, _env(tmp_path), "codex"))


def test_bare_identity_stays_refused_over_an_idle_binding(tmp_path):
    # post-identity discards an identity() result over an unmarked cache, so
    # letting it through would mint an identity nothing records.
    _cache(tmp_path, age_s=2 * DAY)
    result = _mint(tmp_path, _env(tmp_path), "codex", tool="identity", tool_input={})
    assert _denied(result)


@pytest.mark.parametrize("overrides", [{"client_session_id": ""}, {"uuid": ""}])
def test_mark_ignores_an_unbound_cache(tmp_path, overrides):
    _cache(tmp_path, age_s=2 * DAY, **overrides)
    assert binding_activity.mark(str(tmp_path), _payload()) == "none"


def test_mark_without_a_cache_does_nothing(tmp_path):
    assert binding_activity.mark(str(tmp_path), _payload()) == "none"


def _post_stop(workspace: Path, env: dict[str, str], host: str) -> None:
    subprocess.run(
        [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", host],
        input=_payload(), text=True, capture_output=True, timeout=60,
        cwd=str(workspace), env={**env, "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
                                 "PWD": str(workspace)},
        check=False,
    )


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_a_resumed_threads_first_turn_end_keeps_its_mint_open(tmp_path, host):
    # The 2026-10-22 Codex case: a thread bound two days earlier is reopened
    # with no SessionStart. Its first turn ends; the next turn tries to mint.
    path = _cache(tmp_path, age_s=2 * DAY)
    env = _env(tmp_path)
    _post_stop(tmp_path, env, host)
    assert json.loads(path.read_text())["prior_process"] is True
    assert not _denied(_mint(tmp_path, env, host))


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_a_live_threads_turn_end_keeps_the_guard(tmp_path, host):
    path = _cache(tmp_path, age_s=3600)
    env = _env(tmp_path)
    _post_stop(tmp_path, env, host)
    assert json.loads(path.read_text()).get("prior_process") is not True
    assert binding_activity.activity_path(SLOT).read_text().startswith(UUID + " ")
    assert _denied(_mint(tmp_path, env, host))
