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


def _mint(workspace: Path, env: dict[str, str], host: str):
    hook_input = {
        "session_id": SLOT,
        "tool_name": "mcp__unitares-governance__start_session",
        "tool_input": {"force_new": True},
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
    path = _cache(tmp_path, age_s=2 * DAY)
    binding_activity.touch(path)
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
    assert binding_activity.activity_path(path).exists()
    assert json.loads(path.read_text()).get("prior_process") is not True


def test_mark_records_the_unreported_resume_without_losing_the_binding(tmp_path, monkeypatch):
    path = _cache(tmp_path, age_s=2 * DAY)
    seen_argv: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(args, *a, **kw):
        seen_argv.append([str(arg) for arg in args])
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", recording_run)
    assert binding_activity.mark(str(tmp_path), _payload()) == "marked_prior_process"
    data = json.loads(path.read_text())
    assert data["prior_process"] is True
    # The earlier identity stays recorded as the lineage candidate.
    assert data["uuid"] == UUID and data["client_session_id"] == CSID
    # The proof value went on stdin, never into a process argument list.
    assert seen_argv and all(CSID not in " ".join(argv) for argv in seen_argv)
    # An inherited cache is left alone afterwards.
    assert binding_activity.mark(str(tmp_path), _payload()) == "none"


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
    assert binding_activity.activity_path(path).exists()
    assert _denied(_mint(tmp_path, env, host))
