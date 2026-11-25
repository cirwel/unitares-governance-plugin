"""session_cache.py writes session and milestone files mode 0600.

The session cache historically carried continuity tokens. Even after
S20.1b narrows v2 caches to lineage hints (no resume credential), a
world-readable cache still leaks identity hints, slot strings, and
last_checkin_ts metadata to any same-UID process on the host. Guards
against regressing _write_json back to Path.write_text (which inherits
umask 022 → 0644).
"""

from __future__ import annotations

import json
import os
import stat as _stat
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "session_cache.py"


def _run(args: list[str], workspace: Path, stdin: str | None = None) -> str:
    cmd = [sys.executable, str(SCRIPT), *args, "--workspace", str(workspace)]
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=True, input=stdin
    )
    return result.stdout.strip()


def test_set_session_writes_mode_0600(tmp_path: Path) -> None:
    # S20.1b: slotted v2 write is the supported path; --allow-shared kept
    # available for substrate-earned single-tenant deployments. Either
    # writer must emit 0600.
    _run(
        ["set", "session", "--slot", "perm-test",
         "--json", '{"uuid": "00000000-0000-0000-0000-00000000abcd"}'],
        tmp_path,
    )
    cache_file = tmp_path / ".unitares" / "session-perm-test.json"
    assert cache_file.exists()
    assert _stat.S_IMODE(os.stat(cache_file).st_mode) == 0o600


def test_bump_edit_milestone_writes_mode_0600(tmp_path: Path) -> None:
    """Milestone doesn't carry tokens, but perm consistency matters —
    all writers in this helper should emit 0600 files."""
    _run(["bump-edit", "--file-path", "/w/a.py"], tmp_path)
    milestone = tmp_path / ".unitares" / "last-milestone.json"
    assert milestone.exists()
    assert _stat.S_IMODE(os.stat(milestone).st_mode) == 0o600


def test_set_session_overwrite_tightens_loose_mode(tmp_path: Path) -> None:
    """If a pre-existing cache was written 0644 by an older version,
    the next write must tighten it to 0600 — old loose permissions
    must not leak through. (S20.1b: tested on the slotted path; the
    flat-file writer is rejected by default.)"""
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    legacy = cache_dir / "session-perm-test.json"
    legacy.write_text(json.dumps({"uuid": "00000000-0000-0000-0000-00000000beef"}))
    os.chmod(legacy, 0o644)
    assert _stat.S_IMODE(os.stat(legacy).st_mode) == 0o644

    _run(
        ["set", "session", "--slot", "perm-test",
         "--json", '{"uuid": "00000000-0000-0000-0000-00000000abcd"}'],
        tmp_path,
    )
    assert _stat.S_IMODE(os.stat(legacy).st_mode) == 0o600


def test_workspace_cache_dir_ignores_itself(tmp_path: Path) -> None:
    # The caches carry client_session_id; a stranger's `git add -A` must not
    # commit them, whatever their own ignore files say.
    _run(["bump-edit", "--file-path", "/w/a.py"], tmp_path)
    ignore = tmp_path / ".unitares" / ".gitignore"
    assert ignore.read_text().splitlines()[-1] == "*"
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull  # no operator-wide excludes
    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, check=True, env=env,
    ).stdout
    assert ".unitares" not in status


def test_existing_cache_ignore_file_is_left_alone(tmp_path: Path) -> None:
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    (cache_dir / ".gitignore").write_text("mine\n")
    _run(["bump-edit", "--file-path", "/w/a.py"], tmp_path)
    assert (cache_dir / ".gitignore").read_text() == "mine\n"
