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


def _io_module():
    import importlib
    scripts = str(SCRIPT.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("_session_cache_io")


def test_shared_writer_ignores_workspace_cache_but_not_others(tmp_path: Path, monkeypatch) -> None:
    io = _io_module()
    cache_dir = tmp_path / "proj" / ".unitares"
    cache_dir.mkdir(parents=True)
    io.ensure_cache_dir_ignored(cache_dir)
    assert (cache_dir / ".gitignore").read_text().splitlines()[-1] == "*"
    # A directory with another name is not touched.
    other = tmp_path / "proj" / "cache"
    other.mkdir()
    io.ensure_cache_dir_ignored(other)
    assert not (other / ".gitignore").exists()
    # The home mirror sits outside any project and is left alone.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    home_cache = tmp_path / "home" / ".unitares"
    home_cache.mkdir(parents=True)
    io.ensure_cache_dir_ignored(home_cache)
    assert not (home_cache / ".gitignore").exists()


def test_shared_writer_does_not_follow_a_symlinked_ignore(tmp_path: Path) -> None:
    io = _io_module()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    target = tmp_path / "elsewhere"
    (cache_dir / ".gitignore").symlink_to(target)
    io.ensure_cache_dir_ignored(cache_dir)
    assert not target.exists()


def test_session_start_helper_writes_and_respects_existing(tmp_path: Path) -> None:
    hook = SCRIPT.parents[1] / "hooks" / "session-start"
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    script = (
        f"eval \"$(sed -n '/^ensure_workspace_cache_ignored()/,/^}}/p' '{hook}')\"\n"
        f"ensure_workspace_cache_ignored '{cache_dir}'\n"
    )
    subprocess.run(["bash", "-c", script], check=True, env={**os.environ, "HOME": str(tmp_path / "h")})
    assert (cache_dir / ".gitignore").read_text().splitlines()[-1] == "*"
    (cache_dir / ".gitignore").write_text("mine\n")
    subprocess.run(["bash", "-c", script], check=True, env={**os.environ, "HOME": str(tmp_path / "h")})
    assert (cache_dir / ".gitignore").read_text() == "mine\n"


def test_home_mirror_is_skipped_through_a_symlinked_home(tmp_path: Path, monkeypatch) -> None:
    io = _io_module()
    real_home = tmp_path / "real-home"
    (real_home / ".unitares").mkdir(parents=True)
    link_home = tmp_path / "link-home"
    link_home.symlink_to(real_home)
    monkeypatch.setenv("HOME", str(link_home))
    io.ensure_cache_dir_ignored(real_home / ".unitares")
    assert not (real_home / ".unitares" / ".gitignore").exists()
    # The CLI writer's twin skips the home mirror too.
    env = {**os.environ, "HOME": str(link_home)}
    subprocess.run(
        [sys.executable, str(SCRIPT), "bump-edit", "--file-path", "/w/a.py", "--workspace", str(link_home)],
        check=True, capture_output=True, env=env,
    )
    assert not (real_home / ".unitares" / ".gitignore").exists()


def test_session_start_helper_skips_the_home_mirror(tmp_path: Path) -> None:
    hook = SCRIPT.parents[1] / "hooks" / "session-start"
    home = tmp_path / "home"
    (home / ".unitares").mkdir(parents=True)
    script = (
        f"eval \"$(sed -n '/^ensure_workspace_cache_ignored()/,/^}}/p' '{hook}')\"\n"
        f"ensure_workspace_cache_ignored '{home}/.unitares'\n"
    )
    subprocess.run(["bash", "-c", script], check=True, env={**os.environ, "HOME": str(home)})
    assert not (home / ".unitares" / ".gitignore").exists()


def test_prompt_recall_marker_dir_ignores_itself(tmp_path: Path, monkeypatch) -> None:
    import importlib
    scripts = str(SCRIPT.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    prompt_recall = importlib.import_module("prompt_recall")
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    marker = tmp_path / "proj" / ".unitares" / "prompt-kg-recall-s1.json"
    prompt_recall.write_marker(marker, "flaky upload retry")
    assert marker.exists()
    assert (marker.parent / ".gitignore").read_text().splitlines()[-1] == "*"


def test_session_start_helper_skips_the_home_mirror_through_a_symlink(tmp_path: Path) -> None:
    # The hook compares physical paths, so a symlinked HOME is still the home.
    hook = SCRIPT.parents[1] / "hooks" / "session-start"
    real_home = tmp_path / "real-home"
    (real_home / ".unitares").mkdir(parents=True)
    link_home = tmp_path / "link-home"
    link_home.symlink_to(real_home)
    script = (
        f"eval \"$(sed -n '/^ensure_workspace_cache_ignored()/,/^}}/p' '{hook}')\"\n"
        f"ensure_workspace_cache_ignored '{real_home}/.unitares'\n"
    )
    subprocess.run(["bash", "-c", script], check=True, env={**os.environ, "HOME": str(link_home)})
    assert not (real_home / ".unitares" / ".gitignore").exists()
