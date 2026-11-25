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
        f"PLUGIN_ROOT='{SCRIPT.parents[1]}'\n"
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
        f"PLUGIN_ROOT='{SCRIPT.parents[1]}'\n"
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
        f"PLUGIN_ROOT='{SCRIPT.parents[1]}'\n"
        f"eval \"$(sed -n '/^ensure_workspace_cache_ignored()/,/^}}/p' '{hook}')\"\n"
        f"ensure_workspace_cache_ignored '{real_home}/.unitares'\n"
    )
    subprocess.run(["bash", "-c", script], check=True, env={**os.environ, "HOME": str(link_home)})
    assert not (real_home / ".unitares" / ".gitignore").exists()


def _cli_module():
    import importlib
    scripts = str(SCRIPT.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("session_cache")


def test_shared_atomic_write_ignores_its_directory(tmp_path: Path, monkeypatch) -> None:
    # The shared writer itself, not just the helper: removing its call fails this.
    io = _io_module()
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    cache = tmp_path / "proj" / ".unitares" / "session-s1.json"
    io.write_json_dict_unlocked(cache, {"uuid": "x"})
    assert (cache.parent / ".gitignore").read_text().splitlines()[-1] == "*"


def test_shared_lock_alone_ignores_its_directory(tmp_path: Path, monkeypatch) -> None:
    io = _io_module()
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    cache = tmp_path / "proj" / ".unitares" / "session-s1.json"
    with io.session_cache_lock(cache):
        pass
    assert (cache.parent / ".gitignore").exists()
    assert not cache.exists()


def test_cli_lock_alone_ignores_its_directory(tmp_path: Path, monkeypatch) -> None:
    sc = _cli_module()
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    cache = tmp_path / "proj" / ".unitares" / "last-milestone.json"
    with sc._cache_lock(cache, timeout_s=1.0, label="test"):
        pass
    assert (cache.parent / ".gitignore").exists()
    assert not cache.exists()


def test_symlinked_ignore_is_never_written_through(tmp_path: Path) -> None:
    # A symlink already holds the name: its target's content and mode stay
    # exactly as they were, and the symlink is not replaced.
    io = _io_module()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    target = tmp_path / "private.txt"
    target.write_text("secret\n")
    target.chmod(0o600)
    (cache_dir / ".gitignore").symlink_to(target)
    io.ensure_cache_dir_ignored(cache_dir)
    assert target.read_text() == "secret\n"
    assert _stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert os.readlink(cache_dir / ".gitignore") == str(target)


def test_short_write_is_completed_by_the_next_call(tmp_path: Path, monkeypatch) -> None:
    # Nothing is ever deleted (another writer may own the name by then); the
    # partial prefix this content left is completed on the next call.
    io = _io_module()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    real_write = os.write
    calls = []

    def short(fd, data):
        calls.append(len(data))
        return real_write(fd, bytes(data[:8])) if len(calls) == 1 else 0

    monkeypatch.setattr(io.os, "write", short)
    io.ensure_cache_dir_ignored(cache_dir)  # must not raise
    assert (cache_dir / ".gitignore").read_bytes() == io._CACHE_IGNORE[:8]
    monkeypatch.setattr(io.os, "write", real_write)
    io.ensure_cache_dir_ignored(cache_dir)
    assert (cache_dir / ".gitignore").read_bytes() == io._CACHE_IGNORE


def test_interrupted_write_left_by_a_killed_process_is_repaired(tmp_path: Path) -> None:
    io = _io_module()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    for partial in (b"", io._CACHE_IGNORE[:8], io._CACHE_IGNORE[:-1]):
        (cache_dir / ".gitignore").write_bytes(partial)
        io.ensure_cache_dir_ignored(cache_dir)
        assert (cache_dir / ".gitignore").read_bytes() == io._CACHE_IGNORE


def test_a_foreign_or_longer_ignore_is_never_touched(tmp_path: Path) -> None:
    io = _io_module()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    for content in (b"# Written by hand\n", b"node_modules/\n", io._CACHE_IGNORE + b"extra\n"):
        (cache_dir / ".gitignore").write_bytes(content)
        io.ensure_cache_dir_ignored(cache_dir)
        assert (cache_dir / ".gitignore").read_bytes() == content


def test_cli_atomic_writer_alone_ignores_its_directory(tmp_path: Path, monkeypatch) -> None:
    # _write_json without the lock, so only its own call can create the file.
    sc = _cli_module()
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    cache = tmp_path / "proj" / ".unitares" / "last-milestone.json"
    sc._write_json(cache, {"edits": 1})
    assert (cache.parent / ".gitignore").read_bytes() == sc._CACHE_IGNORE


def test_symlinked_home_mirror_directory_is_skipped(tmp_path: Path) -> None:
    # ~/.unitares itself a symlink elsewhere: the hook (through the shared
    # helper) compares the resolved mirror, so the target gets no .gitignore.
    hook = SCRIPT.parents[1] / "hooks" / "session-start"
    home = tmp_path / "home"
    home.mkdir()
    # Named .unitares so only the resolved-path comparison can skip it.
    storage = tmp_path / "storage" / ".unitares"
    storage.mkdir(parents=True)
    (home / ".unitares").symlink_to(storage)
    script = (
        f"PLUGIN_ROOT='{SCRIPT.parents[1]}'\n"
        f"eval \"$(sed -n '/^ensure_workspace_cache_ignored()/,/^}}/p' '{hook}')\"\n"
        f"ensure_workspace_cache_ignored '{storage}'\n"
        f"ensure_workspace_cache_ignored '{home}/.unitares'\n"
    )
    subprocess.run(["bash", "-c", script], check=True, env={**os.environ, "HOME": str(home)})
    assert not (storage / ".gitignore").exists()


def test_symlinked_cache_directory_gets_no_ignore(tmp_path: Path, monkeypatch) -> None:
    # `.unitares -> src`: the rule must not land in src/, which would ignore
    # every new file there. Covers the CLI writer too, which shares the helper.
    io = _io_module()
    sc = _cli_module()
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    project = tmp_path / "proj"
    src = project / "src"
    src.mkdir(parents=True)
    (project / ".unitares").symlink_to(src)
    io.ensure_cache_dir_ignored(project / ".unitares")
    sc._write_json(project / ".unitares" / "last-milestone.json", {"edits": 1})
    assert not (src / ".gitignore").exists()


def test_ensure_ignored_cli_rejects_misuse(tmp_path: Path) -> None:
    script = SCRIPT.parent / "_session_cache_io.py"
    for argv in ([], ["ensure-ignored"], ["ensure-ignore", str(tmp_path)],
                 ["ensure-ignored", str(tmp_path), "extra"]):
        result = subprocess.run([sys.executable, str(script), *argv], capture_output=True, text=True)
        assert result.returncode == 2
        assert "usage:" in result.stderr
