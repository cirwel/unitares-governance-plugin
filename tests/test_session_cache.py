"""Tests for session_cache.py milestone accumulator.

Covers the behavior the post-edit hook depends on:
  * bump-edit increments the counter and dedupes files_touched
  * first_edit_ts is stamped on first bump, not overwritten after
  * reset-milestone zeros the accumulator but leaves legacy keys alone
  * the files_touched cap is enforced (no unbounded growth)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "session_cache.py"


def _run(args: list[str], workspace: Path) -> str:
    cmd = [sys.executable, str(SCRIPT), *args, "--workspace", str(workspace)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _read_milestone(workspace: Path) -> dict:
    raw = _run(["get", "milestone"], workspace)
    return json.loads(raw) if raw else {}


def test_bump_edit_increments_counter(tmp_path: Path) -> None:
    _run(["bump-edit", "--file-path", "/w/a.py"], tmp_path)
    _run(["bump-edit", "--file-path", "/w/b.py"], tmp_path)
    _run(["bump-edit", "--file-path", "/w/c.py"], tmp_path)

    state = _read_milestone(tmp_path)
    assert state["edit_count"] == 3
    assert state["files_touched"] == ["/w/a.py", "/w/b.py", "/w/c.py"]


def test_bump_edit_dedupes_files(tmp_path: Path) -> None:
    for _ in range(4):
        _run(["bump-edit", "--file-path", "/w/a.py"], tmp_path)
    _run(["bump-edit", "--file-path", "/w/b.py"], tmp_path)

    state = _read_milestone(tmp_path)
    assert state["edit_count"] == 5
    assert state["files_touched"] == ["/w/a.py", "/w/b.py"]


def test_first_edit_ts_only_stamped_once(tmp_path: Path) -> None:
    _run(["bump-edit", "--file-path", "/w/a.py"], tmp_path)
    first = _read_milestone(tmp_path)["first_edit_ts"]

    _run(["bump-edit", "--file-path", "/w/b.py"], tmp_path)
    _run(["bump-edit", "--file-path", "/w/c.py"], tmp_path)
    final = _read_milestone(tmp_path)

    assert final["first_edit_ts"] == first
    # last_edit_ts always updates; first_edit_ts never moves after bump 1.
    assert final["last_edit_ts"] >= first


def test_files_touched_is_capped(tmp_path: Path) -> None:
    # 30 distinct files — cap is 20, should keep only the most recent 20.
    for i in range(30):
        _run(["bump-edit", "--file-path", f"/w/f{i:02d}.py"], tmp_path)

    state = _read_milestone(tmp_path)
    assert state["edit_count"] == 30
    assert len(state["files_touched"]) == 20
    assert state["files_touched"][0] == "/w/f10.py"
    assert state["files_touched"][-1] == "/w/f29.py"


def test_reset_milestone_zeros_accumulator(tmp_path: Path) -> None:
    _run(["bump-edit", "--file-path", "/w/a.py"], tmp_path)
    _run(["bump-edit", "--file-path", "/w/b.py"], tmp_path)
    _run(["reset-milestone"], tmp_path)

    state = _read_milestone(tmp_path)
    assert state["edit_count"] == 0
    assert state["files_touched"] == []
    assert state["first_edit_ts"] is None
    assert state["last_edit_ts"] is None


def test_bump_after_reset_restamps_first_edit(tmp_path: Path) -> None:
    _run(["bump-edit", "--file-path", "/w/a.py"], tmp_path)
    _run(["reset-milestone"], tmp_path)
    _run(["bump-edit", "--file-path", "/w/b.py"], tmp_path)

    state = _read_milestone(tmp_path)
    assert state["edit_count"] == 1
    assert state["files_touched"] == ["/w/b.py"]
    assert state["first_edit_ts"] is not None


def _run_raw(args: list[str], workspace: Path) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPT), *args, "--workspace", str(workspace)]
    return subprocess.run(cmd, capture_output=True, text=True)


def _run_with_home(
    args: list[str], workspace: Path, fake_home: Path
) -> subprocess.CompletedProcess:
    """Run session_cache.py with HOME redirected to a sandbox so the slotted-
    HOME mirror write goes into the test tmp dir rather than the real ~/.unitares/."""
    cmd = [sys.executable, str(SCRIPT), *args, "--workspace", str(workspace)]
    env = {"HOME": str(fake_home), "PATH": "/usr/bin:/bin"}
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def test_set_session_mirrors_to_home(tmp_path: Path) -> None:
    """Session-kind writes mirror to $HOME/.unitares/session-<slot>.json so
    the slotted-HOME read fallback in _session_lookup.resolve_session_file
    actually has a file to find when PWD changes between post-identity and
    later hooks (the PWD-mismatch failure mode).

    Milestone-kind writes are NOT mirrored — they stay workspace-scoped per
    the auto-checkin design.
    """
    workspace = tmp_path / "ws"
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True)

    result = _run_with_home(
        [
            "set",
            "session",
            "--slot",
            "test-mirror-slot-9999",
            "--json",
            '{"uuid": "00000000-0000-0000-0000-000000000099"}',
        ],
        workspace,
        fake_home,
    )
    assert result.returncode == 0, result.stderr

    # Primary workspace write (unchanged behavior)
    ws_path = workspace / ".unitares" / "session-test-mirror-slot-9999.json"
    assert ws_path.exists(), f"workspace cache not written: {ws_path}"
    ws_data = json.loads(ws_path.read_text())
    assert ws_data["uuid"] == "00000000-0000-0000-0000-000000000099"

    # HOME mirror (new behavior — the fix)
    home_path = fake_home / ".unitares" / "session-test-mirror-slot-9999.json"
    assert home_path.exists(), f"home mirror not written: {home_path}"
    home_data = json.loads(home_path.read_text())
    assert home_data == ws_data, "home mirror payload should match workspace"


def test_set_milestone_does_not_mirror_to_home(tmp_path: Path) -> None:
    """Milestone accumulator stays workspace-scoped — only session caches
    mirror to HOME (per the design comment in session_cache.py:cmd_set)."""
    workspace = tmp_path / "ws"
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True)

    result = _run_with_home(
        ["bump-edit", "--file-path", "/w/a.py"],
        workspace,
        fake_home,
    )
    assert result.returncode == 0, result.stderr

    # Milestone in workspace
    assert (workspace / ".unitares" / "last-milestone.json").exists()
    # NOT mirrored to home
    assert not (fake_home / ".unitares" / "last-milestone.json").exists()


def test_set_session_home_mirror_skipped_when_workspace_is_home(tmp_path: Path) -> None:
    """If workspace IS $HOME, the home-mirror is a no-op (paths are equal) —
    one write, not two."""
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True)

    result = _run_with_home(
        [
            "set",
            "session",
            "--slot",
            "test-noop-slot-8888",
            "--json",
            '{"uuid": "00000000-0000-0000-0000-000000000088"}',
        ],
        fake_home,  # workspace == home
        fake_home,
    )
    assert result.returncode == 0, result.stderr
    # Exactly one file at home/.unitares/, no separate workspace path
    cache_files = list((fake_home / ".unitares").glob("session-*.json"))
    assert len(cache_files) == 1
    assert cache_files[0].name == "session-test-noop-slot-8888.json"


def test_set_session_refuses_stub_without_identity(tmp_path: Path) -> None:
    """Stamp-only writes into a missing/identityless session cache must fail loudly,
    not silently produce 88-byte stubs that brick the next hook's identity lookup.
    Reproduces the failure path observed in production where post-edit's trailing
    `--merge --stamp last_checkin_ts` write created caches with no UUID/token,
    causing subsequent hooks to no-op."""
    result = _run_raw(
        [
            "set", "session",
            "--slot", "test-slot",
            "--merge", "--stamp",
            "--json", '{"last_checkin_ts": 1777281496}',
        ],
        tmp_path,
    )
    assert result.returncode == 1
    assert "refusing to write session cache without any identity field" in result.stderr
    assert not (tmp_path / ".unitares" / "session-test-slot.json").exists()


def test_set_session_allows_partial_identity(tmp_path: Path) -> None:
    """Partial-identity writes via uuid-only or client_session_id-only seed
    are still allowed — readers only need any one identity hook to resolve.
    Note: the legacy `continuity_token`-only partial seed is no longer valid
    under S20.1b — see test_set_session_rejects_continuity_token_payload."""
    uuid_only = _run_raw(
        ["set", "session", "--slot", "test-slot",
         "--json", '{"uuid": "00000000-0000-0000-0000-000000000001"}'],
        tmp_path,
    )
    assert uuid_only.returncode == 0

    sid_only = _run_raw(
        ["set", "session", "--slot", "test-slot-2",
         "--json", '{"client_session_id": "agent-test"}'],
        tmp_path,
    )
    assert sid_only.returncode == 0


# ---------------------------------------------------------------------------
# S20.1b — helper-side rejection of slotless writes + non-empty token payloads
# ---------------------------------------------------------------------------


def test_set_session_rejects_slotless_write(tmp_path: Path) -> None:
    """Slotless session writes produce flat session.json — the workspace-shared
    'current owner' file the hook layer (PR #19) refuses to read. The helper
    now refuses to write it by default."""
    result = _run_raw(
        ["set", "session",
         "--json", '{"uuid": "00000000-0000-0000-0000-000000000001"}'],
        tmp_path,
    )
    assert result.returncode == 2
    assert "refusing slotless session write" in result.stderr
    assert not (tmp_path / ".unitares" / "session.json").exists()


def test_set_session_allows_shared_with_opt_in(tmp_path: Path) -> None:
    """`--allow-shared` permits the slotless write for substrate-earned
    single-tenant deployments (Lumen on dedicated Pi)."""
    result = _run_raw(
        ["set", "session", "--allow-shared",
         "--json", '{"uuid": "00000000-0000-0000-0000-000000000001"}'],
        tmp_path,
    )
    assert result.returncode == 0
    assert (tmp_path / ".unitares" / "session.json").exists()


def test_set_milestone_unaffected_by_slotless_rule(tmp_path: Path) -> None:
    """The slotless-rejection rule applies to kind=session only. The milestone
    accumulator is workspace-level by design (per _cache_path); slotless
    writes there must keep working so the post-edit bump-edit path is
    unaffected."""
    result = _run_raw(
        ["set", "milestone",
         "--json", '{"edit_count": 1}'],
        tmp_path,
    )
    assert result.returncode == 0
    assert (tmp_path / ".unitares" / "last-milestone.json").exists()


def test_set_session_rejects_continuity_token_payload(tmp_path: Path) -> None:
    """Non-empty continuity_token in a session payload is the v1 legacy
    pattern. Under v2 ontology the cache holds lineage hints, not resume
    credentials — out-of-tree callers (e.g., onboard_helper.py) cannot
    bypass the post-identity hook's empty-token contract through this helper."""
    result = _run_raw(
        ["set", "session", "--slot", "test-slot",
         "--json", '{"uuid": "00000000-0000-0000-0000-000000000001", '
                   '"continuity_token": "v1.real-token"}'],
        tmp_path,
    )
    assert result.returncode == 2
    assert "non-empty continuity_token" in result.stderr
    assert not (tmp_path / ".unitares" / "session-test-slot.json").exists()


def test_set_session_allows_empty_token_erasure(tmp_path: Path) -> None:
    """Empty-string continuity_token is the v2 hook erasure path
    (post-identity writes schema_version: 2 with empty token to overwrite
    any prior value). Must continue to pass."""
    result = _run_raw(
        ["set", "session", "--slot", "test-slot",
         "--json", '{"uuid": "00000000-0000-0000-0000-000000000001", '
                   '"continuity_token": "", "schema_version": 2}'],
        tmp_path,
    )
    assert result.returncode == 0
    cached = json.loads(
        (tmp_path / ".unitares" / "session-test-slot.json").read_text()
    )
    assert cached["continuity_token"] == ""
    assert cached["schema_version"] == 2


def test_set_session_token_check_runs_after_merge(tmp_path: Path) -> None:
    """The token rejection must apply to the *merged* payload, not just the
    incoming JSON. Otherwise a caller could seed an empty token and merge
    a real one on top to bypass the gate."""
    seed = _run_raw(
        ["set", "session", "--slot", "test-slot",
         "--json", '{"uuid": "00000000-0000-0000-0000-000000000001", '
                   '"continuity_token": ""}'],
        tmp_path,
    )
    assert seed.returncode == 0

    sneak = _run_raw(
        ["set", "session", "--slot", "test-slot", "--merge",
         "--json", '{"continuity_token": "v1.real-token"}'],
        tmp_path,
    )
    assert sneak.returncode == 2
    cached = json.loads(
        (tmp_path / ".unitares" / "session-test-slot.json").read_text()
    )
    # Pre-existing seed retained; merge was rejected before the write.
    assert cached["continuity_token"] == ""


def test_cmd_list_returns_slot_inventory_newest_first(tmp_path: Path) -> None:
    """`list` returns one entry per session-*.json, sorted by updated_at
    descending. Callers use this for the scan-newest lineage fallback —
    field names are the v2 declared-lineage parameters of `onboard()` so
    consumers naturally flow into `onboard(force_new=true,
    parent_agent_id=entry["parent_agent_id"])`."""
    older = _run_raw(
        ["set", "session", "--slot", "older", "--stamp",
         "--json", '{"uuid": "00000000-0000-0000-0000-000000000001"}'],
        tmp_path,
    )
    assert older.returncode == 0
    # ISO-8601 strings sort lexically; force a real gap on the timestamp.
    older_path = tmp_path / ".unitares" / "session-older.json"
    older_data = json.loads(older_path.read_text())
    older_data["updated_at"] = "2026-04-20T00:00:00+00:00"
    older_path.write_text(json.dumps(older_data))

    newer = _run_raw(
        ["set", "session", "--slot", "newer", "--stamp",
         "--json", '{"uuid": "00000000-0000-0000-0000-000000000002"}'],
        tmp_path,
    )
    assert newer.returncode == 0
    newer_path = tmp_path / ".unitares" / "session-newer.json"
    newer_data = json.loads(newer_path.read_text())
    newer_data["updated_at"] = "2026-04-26T00:00:00+00:00"
    newer_path.write_text(json.dumps(newer_data))

    listed = _run_raw(["list"], tmp_path)
    assert listed.returncode == 0
    entries = json.loads(listed.stdout)
    assert [e["slot"] for e in entries] == ["newer", "older"]
    assert entries[0]["parent_agent_id"] == "00000000-0000-0000-0000-000000000002"
    assert entries[1]["parent_agent_id"] == "00000000-0000-0000-0000-000000000001"
    # Lineage-explicit field naming: a `uuid` key would invite resume-
    # pattern misuse; the surface explicitly steers toward declared lineage.
    assert "uuid" not in entries[0]
    assert "client_session_id" not in entries[0]
    assert "prior_client_session_id" in entries[0]


def test_cmd_list_filters_null_identity_entries(tmp_path: Path) -> None:
    """An on-disk session file with neither uuid nor client_session_id has
    no actionable lineage hint; emitting it would silently mis-rank the
    scan-newest pick if it sorted to the top by updated_at. Skip it."""
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    # Well-formed JSON, no identity fields, freshest updated_at — would
    # win the sort if not filtered.
    (cache_dir / "session-orphan.json").write_text(json.dumps({
        "last_checkin_ts": 1777300000,
        "updated_at": "2026-04-26T23:59:59+00:00",
    }))
    good = _run_raw(
        ["set", "session", "--slot", "good", "--stamp",
         "--json", '{"uuid": "00000000-0000-0000-0000-000000000001"}'],
        tmp_path,
    )
    assert good.returncode == 0

    listed = _run_raw(["list"], tmp_path)
    assert listed.returncode == 0
    entries = json.loads(listed.stdout)
    slots = [e["slot"] for e in entries]
    assert slots == ["good"]


def test_cmd_list_handles_malformed_files(tmp_path: Path) -> None:
    """Malformed JSON in the cache directory must not crash list — it's a
    discovery surface, not a validator. Skip silently."""
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    (cache_dir / "session-broken.json").write_text("not-json{")
    (cache_dir / "session-empty.json").write_text("")

    good = _run_raw(
        ["set", "session", "--slot", "good",
         "--json", '{"uuid": "00000000-0000-0000-0000-000000000001"}'],
        tmp_path,
    )
    assert good.returncode == 0

    listed = _run_raw(["list"], tmp_path)
    assert listed.returncode == 0
    entries = json.loads(listed.stdout)
    slots = [e["slot"] for e in entries]
    assert slots == ["good"]


def test_cmd_list_empty_workspace(tmp_path: Path) -> None:
    """No `.unitares/` directory yet → empty array, not a crash."""
    listed = _run_raw(["list"], tmp_path)
    assert listed.returncode == 0
    assert json.loads(listed.stdout) == []


def test_cmd_list_surfaces_flat_session_json(tmp_path: Path) -> None:
    """Pre-PR-19 flat session.json files still on disk should show up in
    list with slot=None — operators need them visible to migrate. Future
    writes are blocked by the slotless-rejection rule; reads stay open."""
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    (cache_dir / "session.json").write_text(json.dumps({
        "uuid": "00000000-0000-0000-0000-00000000beef",
        "updated_at": "2026-04-15T00:00:00+00:00",
    }))

    listed = _run_raw(["list"], tmp_path)
    assert listed.returncode == 0
    entries = json.loads(listed.stdout)
    assert len(entries) == 1
    assert entries[0]["slot"] is None
    assert entries[0]["parent_agent_id"] == "00000000-0000-0000-0000-00000000beef"


def test_set_session_rejects_whitespace_continuity_token(tmp_path: Path) -> None:
    """A one-byte ' ' or '\\n' continuity_token is truthy under bare-`token`
    truthiness but downstream readers that test `if continuity_token:` will
    treat it as a resume credential. Rejection uses `.strip()` so whitespace
    cannot slip past the v2 gate."""
    for sneaky in (" ", "\t", "\n", "  \n  "):
        result = _run_raw(
            ["set", "session", "--slot", "ws-test",
             "--json", json.dumps({
                 "uuid": "00000000-0000-0000-0000-000000000001",
                 "continuity_token": sneaky,
             })],
            tmp_path,
        )
        assert result.returncode == 2, f"expected rejection for {sneaky!r}"
        assert "non-empty continuity_token" in result.stderr


def test_set_session_merge_strips_legacy_v1_token(tmp_path: Path) -> None:
    """A pre-existing slot file from before S11/S20 may carry a real
    `continuity_token` at rest. The post-edit auto-checkin hook calls
    `set session --slot X --merge --stamp --json {"last_checkin_ts": N}`
    against this file. Without the migration strip, the merge would carry
    the legacy token forward, the rejection would fire, and the stamp
    would be silently dropped (errors swallowed via `|| true`).

    Post-fix: the helper auto-strips the pre-existing token during merge,
    emits a [V1_LEGACY_STRIP] breadcrumb, and lets the clean stamp succeed."""
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    legacy_path = cache_dir / "session-legacy-slot.json"
    legacy_path.write_text(json.dumps({
        "uuid": "00000000-0000-0000-0000-000000000001",
        "client_session_id": "agent-legacy",
        "continuity_token": "v1.real-token-from-disk",
        "last_checkin_ts": 1_700_000_000,
    }))

    result = _run_raw(
        ["set", "session", "--slot", "legacy-slot", "--merge", "--stamp",
         "--json", '{"last_checkin_ts": 1777285000}'],
        tmp_path,
    )
    assert result.returncode == 0
    assert "[V1_LEGACY_STRIP]" in result.stderr

    cached = json.loads(legacy_path.read_text())
    assert cached["uuid"] == "00000000-0000-0000-0000-000000000001"
    assert cached["last_checkin_ts"] == 1777285000
    assert cached.get("continuity_token") in (None, "")  # stripped


def test_set_session_merge_strip_does_not_bypass_rejection(tmp_path: Path) -> None:
    """The migration strip is one-way: a legacy token gets dropped from
    the existing payload, but if the *new* incoming JSON carries a non-
    empty token, the rejection still fires after the merge."""
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    legacy_path = cache_dir / "session-attack-slot.json"
    legacy_path.write_text(json.dumps({
        "uuid": "00000000-0000-0000-0000-000000000001",
        "continuity_token": "v1.legacy-token",
    }))
    legacy_before = legacy_path.read_text()

    result = _run_raw(
        ["set", "session", "--slot", "attack-slot", "--merge",
         "--json", '{"continuity_token": "v1.attacker-supplied"}'],
        tmp_path,
    )
    assert result.returncode == 2
    assert "non-empty continuity_token" in result.stderr
    # Legacy file untouched — rejection short-circuited the write.
    assert legacy_path.read_text() == legacy_before


def test_set_session_allows_stamp_when_identity_already_cached(tmp_path: Path) -> None:
    """The stamp-only path must still work for the success case: cache has
    identity from a prior onboard, post-edit merges last_checkin_ts on top."""
    full = {
        "uuid": "00000000-0000-0000-0000-000000000001",
        "client_session_id": "agent-test",
        "continuity_token": "",
        "schema_version": 2,
    }
    seed = _run_raw(
        ["set", "session", "--slot", "test-slot", "--json", json.dumps(full)],
        tmp_path,
    )
    assert seed.returncode == 0

    stamp = _run_raw(
        [
            "set", "session",
            "--slot", "test-slot",
            "--merge", "--stamp",
            "--json", '{"last_checkin_ts": 1777281496}',
        ],
        tmp_path,
    )
    assert stamp.returncode == 0
    cached = json.loads((tmp_path / ".unitares" / "session-test-slot.json").read_text())
    assert cached["uuid"] == full["uuid"]
    assert cached["client_session_id"] == full["client_session_id"]
    assert cached["last_checkin_ts"] == 1777281496
    assert "updated_at" in cached


def test_write_json_failure_does_not_leave_tmp_file(tmp_path: Path, monkeypatch) -> None:
    """S20.3: a failed atomic write unlinks the temp file rather than
    leaving a .tmp turd in the cache directory.

    Imports session_cache.py in-process (rather than via subprocess) so we
    can monkeypatch os.replace to simulate the failure path.
    """
    import importlib.util
    import os

    spec = importlib.util.spec_from_file_location("session_cache_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    target = cache_dir / "session.json"

    def boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(module.os, "replace", boom)
    import pytest as _pytest

    with _pytest.raises(OSError):
        module._write_json(target, {"uuid": "x"})

    stragglers = [p for p in cache_dir.iterdir() if p.suffix == ".tmp"]
    assert stragglers == [], f"temp file leaked: {stragglers}"
