"""Races in the workspace ``.unitares/.gitignore`` helper (issue #460).

Each test runs a concurrent writer at one named step of
``ensure_cache_dir_ignored`` through the module's ``_race_hook`` seam, or
kills the helper mid-write in a child process.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _io():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("_session_cache_io")


def _at(io, monkeypatch, step: str, action) -> None:
    def hook(name: str) -> None:
        if name == step:
            action()

    monkeypatch.setattr(io, "_race_hook", hook)


def _scratch(cache_dir: Path) -> list[str]:
    return sorted(n for n in os.listdir(cache_dir) if n != ".gitignore")


_KILL_MID_WRITE = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import _session_cache_io as io
if sys.argv[3] == "no-dir-fd":  # the Windows shape: no dir_fd, no O_NOFOLLOW
    os.supports_dir_fd = set()
    del os.O_NOFOLLOW
real_write = os.write
def dying(fd, data):
    real_write(fd, bytes(data[:8]))
    os._exit(9)
os.write = dying
io.ensure_cache_dir_ignored(Path(sys.argv[2]))
"""


@pytest.mark.parametrize("platform", ["dir-fd", "no-dir-fd"])
def test_a_run_killed_mid_write_leaves_no_partial_ignore(tmp_path: Path, platform: str) -> None:
    # Before #460 a kill here left a truncated .gitignore that only a later
    # repair (and never on a platform without O_NOFOLLOW) could complete.
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", _KILL_MID_WRITE, str(SCRIPTS), str(cache_dir), platform],
        env={**os.environ, "HOME": str(tmp_path / "home")},
    )
    assert result.returncode == 9
    assert not (cache_dir / ".gitignore").exists()
    io.ensure_cache_dir_ignored(cache_dir)
    assert (cache_dir / ".gitignore").read_bytes() == io._CACHE_IGNORE


def test_a_file_created_while_staging_wins(tmp_path: Path, monkeypatch) -> None:
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    _at(io, monkeypatch, "staged", lambda: (cache_dir / ".gitignore").write_bytes(b"mine\n"))
    io.ensure_cache_dir_ignored(cache_dir)
    assert (cache_dir / ".gitignore").read_bytes() == b"mine\n"
    assert _scratch(cache_dir) == []


def test_an_in_place_edit_during_repair_is_never_interleaved(tmp_path: Path, monkeypatch) -> None:
    # The #460 race: the partial is checked, then an editor rewrites the file
    # in place. The repair must notice and keep the editor's bytes intact
    # rather than append the rest of its own content after them.
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    ignore = cache_dir / ".gitignore"
    ignore.write_bytes(io._CACHE_IGNORE[:8])

    def edit_in_place() -> None:
        with open(ignore, "r+b") as f:
            f.truncate(0)
            f.write(b"mine\n")

    _at(io, monkeypatch, "repair-checked", edit_in_place)
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == b"mine\n"
    assert _scratch(cache_dir) == []


@pytest.mark.parametrize("edit", ["longer-prefix", "shorter-prefix", "truncated"])
def test_an_edit_that_leaves_a_prefix_is_still_not_overwritten(tmp_path: Path, monkeypatch, edit: str) -> None:
    # Growing, shrinking or truncating the partial (an editor's O_TRUNC before
    # its write) is still a change by someone else after the check, though
    # the result is again a prefix: the swap must back off, not replace it.
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    ignore = cache_dir / ".gitignore"
    ignore.write_bytes(io._CACHE_IGNORE[:8])
    edited = {"longer-prefix": io._CACHE_IGNORE[:16], "shorter-prefix": io._CACHE_IGNORE[:4],
              "truncated": b""}[edit]

    def edit_in_place() -> None:
        with open(ignore, "r+b") as f:
            f.truncate(0)
            f.write(edited)

    _at(io, monkeypatch, "repair-checked", edit_in_place)
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == edited
    assert _scratch(cache_dir) == []


def test_a_directory_swapped_in_before_the_swap_is_put_back(tmp_path: Path, monkeypatch) -> None:
    # A directory cannot be hard-linked back; it is renamed back instead.
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    ignore = cache_dir / ".gitignore"
    ignore.write_bytes(io._CACHE_IGNORE[:8])

    def to_directory() -> None:
        ignore.unlink()
        ignore.mkdir()
        (ignore / "keep").write_bytes(b"x")

    _at(io, monkeypatch, "repair-checked", to_directory)
    io.ensure_cache_dir_ignored(cache_dir)
    assert (ignore / "keep").read_bytes() == b"x"
    assert _scratch(cache_dir) == []


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_a_read_only_partial_is_left_as_it_is(tmp_path: Path) -> None:
    # An empty read-only .gitignore is how a user would opt out; the repair
    # needs write permission on the file, as it did before #460.
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    ignore = cache_dir / ".gitignore"
    ignore.write_bytes(b"")
    ignore.chmod(0o444)
    inode = os.stat(ignore).st_ino
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == b""
    assert os.stat(ignore).st_ino == inode
    assert _scratch(cache_dir) == []


def test_a_partial_that_cannot_be_linked_is_left_in_place(tmp_path: Path, monkeypatch) -> None:
    # Linux fs.protected_hardlinks refuses links to another user's file. The
    # probe links the entry itself, so such a file is never moved aside.
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    ignore = cache_dir / ".gitignore"
    ignore.write_bytes(io._CACHE_IGNORE[:8])
    real_link = os.link

    def protected(src, dst, **kwargs):
        if os.fspath(src).endswith(".gitignore"):
            raise PermissionError(1, "Operation not permitted")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(io.os, "link", protected)
    inode = os.stat(ignore).st_ino
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == io._CACHE_IGNORE[:8]
    assert os.stat(ignore).st_ino == inode
    assert _scratch(cache_dir) == []


def test_a_complete_ignore_costs_no_write(tmp_path: Path, monkeypatch) -> None:
    # The steady state: no scratch file is created, the directory is not
    # modified, so file watchers see nothing and a kill leaves nothing.
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    io.ensure_cache_dir_ignored(cache_dir)
    before = os.stat(cache_dir).st_mtime_ns
    seen = []
    monkeypatch.setattr(io, "_race_hook", seen.append)
    for _ in range(3):
        io.ensure_cache_dir_ignored(cache_dir)
    assert seen == ["dir-opened"] * 3
    assert os.stat(cache_dir).st_mtime_ns == before
    assert os.listdir(cache_dir) == [".gitignore"]


def test_a_file_saved_by_rename_before_the_swap_is_restored(tmp_path: Path, monkeypatch) -> None:
    # Editors that save atomically replace the inode. The rename-aside takes
    # their new file; it is not the inode that was checked, so it goes back.
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    ignore = cache_dir / ".gitignore"
    ignore.write_bytes(io._CACHE_IGNORE[:8])

    def atomic_save() -> None:
        saved = tmp_path / "save.tmp"
        saved.write_bytes(io._CACHE_IGNORE[:8])  # even identical bytes
        os.replace(saved, ignore)

    _at(io, monkeypatch, "repair-checked", atomic_save)
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == io._CACHE_IGNORE[:8]
    assert _scratch(cache_dir) == []


def test_a_file_saved_after_the_swap_wins(tmp_path: Path, monkeypatch) -> None:
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    ignore = cache_dir / ".gitignore"
    ignore.write_bytes(io._CACHE_IGNORE[:8])
    _at(io, monkeypatch, "repair-captured", lambda: ignore.write_bytes(b"mine\n"))
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == b"mine\n"
    assert _scratch(cache_dir) == []


def test_a_changed_file_is_never_deleted_when_its_name_is_retaken(tmp_path: Path, monkeypatch) -> None:
    # Edited after the check, then a new file saved at the name after the
    # rename: neither may be lost. The moved file stays under a scratch name.
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    ignore = cache_dir / ".gitignore"
    ignore.write_bytes(io._CACHE_IGNORE[:8])

    def hook(step: str) -> None:
        if step == "repair-checked":
            with open(ignore, "r+b") as f:
                f.truncate(0)
                f.write(b"edited\n")
        elif step == "repair-captured":
            ignore.write_bytes(b"saved\n")

    monkeypatch.setattr(io, "_race_hook", hook)
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == b"saved\n"
    kept = _scratch(cache_dir)
    assert len(kept) == 1 and kept[0].endswith(".aside")
    assert (cache_dir / kept[0]).read_bytes() == b"edited\n"


def test_an_untouched_partial_is_still_repaired(tmp_path: Path, monkeypatch) -> None:
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    ignore = cache_dir / ".gitignore"
    ignore.write_bytes(io._CACHE_IGNORE[:8])
    seen = []
    monkeypatch.setattr(io, "_race_hook", seen.append)
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == io._CACHE_IGNORE
    assert os.stat(ignore).st_nlink == 1
    assert _scratch(cache_dir) == []
    assert seen == ["dir-opened", "staged", "repair-checked", "repair-captured"]


@pytest.mark.parametrize("swapped", ["ancestor", "cache-dir"])
def test_a_swap_after_the_open_cannot_redirect_the_write(tmp_path: Path, monkeypatch, swapped: str) -> None:
    # Ancestor substitution after the directory is opened: every step is
    # relative to the opened handle, so the file lands in the directory that
    # was checked, and nothing reaches the decoy the path now points to.
    io = _io()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "proj"
    cache_dir = project / ".unitares"
    cache_dir.mkdir(parents=True)
    decoy = tmp_path / "decoy" / ".unitares"
    decoy.mkdir(parents=True)

    def swap() -> None:
        if swapped == "ancestor":
            project.rename(tmp_path / "moved")
            project.symlink_to(decoy.parent)
        else:
            cache_dir.rename(project / "moved")
            cache_dir.symlink_to(decoy)

    _at(io, monkeypatch, "dir-opened", swap)
    io.ensure_cache_dir_ignored(cache_dir)
    moved = tmp_path / "moved" / ".unitares" if swapped == "ancestor" else project / "moved"
    assert (moved / ".gitignore").read_bytes() == io._CACHE_IGNORE
    assert os.listdir(decoy) == []


def test_without_hard_links_creation_falls_back_to_an_exclusive_create(tmp_path: Path, monkeypatch) -> None:
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()

    def no_links(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(io.os, "link", no_links)
    io.ensure_cache_dir_ignored(cache_dir)
    assert (cache_dir / ".gitignore").read_bytes() == io._CACHE_IGNORE
    assert _scratch(cache_dir) == []


def test_without_hard_links_a_partial_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    # Filesystems such as FAT report EEXIST before they refuse a link. The
    # repair proves a link works before it moves anything, since putting the
    # file back depends on one.
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    ignore = cache_dir / ".gitignore"
    ignore.write_bytes(io._CACHE_IGNORE[:8])

    def fat_link(src, dst, **kwargs):
        if os.fspath(dst).endswith(".gitignore"):
            raise FileExistsError(17, "File exists")
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(io.os, "link", fat_link)
    inode = os.stat(ignore).st_ino
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == io._CACHE_IGNORE[:8]
    assert os.stat(ignore).st_ino == inode
    assert _scratch(cache_dir) == []
