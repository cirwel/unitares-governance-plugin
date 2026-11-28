"""Races in the workspace ``.unitares/.gitignore`` helper (issue #460).

Each test runs a concurrent writer at one named step of
``ensure_cache_dir_ignored`` through the module's ``_race_hook`` seam, or
kills the helper mid-write in a child process.
"""

from __future__ import annotations

import errno
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


def _record(io, cache_dir: Path) -> tuple[Path, Path]:
    """The probe and capture paths a repair's transaction record names."""
    names = io._JOURNAL_RE.fullmatch((cache_dir / io._JOURNAL_NAME).read_bytes())
    assert names is not None
    return cache_dir / names.group(1).decode(), cache_dir / names.group(2).decode()


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


def test_a_directory_swapped_in_before_the_swap_is_kept_at_its_capture(tmp_path: Path, monkeypatch) -> None:
    # A directory cannot be hard-linked back, and renaming it back could
    # replace an empty directory created at the name meanwhile. It stays at
    # the capture the record names, and nothing is published over it.
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
    for _ in range(2):
        io.ensure_cache_dir_ignored(cache_dir)
        _, capture = _record(io, cache_dir)
        assert (capture / "keep").read_bytes() == b"x"
        assert not ignore.exists()
        assert _scratch(cache_dir) == sorted([io._JOURNAL_NAME, capture.name])


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
    # rename: neither may be lost. The moved file stays at the capture the
    # record names, and later runs keep both.
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
    _, capture = _record(io, cache_dir)
    assert capture.read_bytes() == b"edited\n"
    assert _scratch(cache_dir) == sorted([io._JOURNAL_NAME, capture.name])
    monkeypatch.setattr(io, "_race_hook", None)
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == b"saved\n"
    assert capture.read_bytes() == b"edited\n"


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
    assert seen == ["dir-opened", "staged", "repair-probed", "repair-checked",
                    "repair-captured", "repair-published"]


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
        raise OSError(errno.ENOTSUP, "Operation not supported")

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


def _held(cache_dir: Path) -> list[str]:
    # A killed run also leaves its staged copy (plugin content only, covered
    # by the published `*`); these tests are about what a repair displaced.
    return [n for n in _scratch(cache_dir) if not n.endswith(".tmp")]


# --- Interrupted repairs and their transaction record (PR #467 review) ------

_KILL_AT_STEP = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import _session_cache_io as io
cache_dir, step, save = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
def hook(name):
    if name == "repair-checked" and save:
        # An editor saving atomically between the check and the capture.
        tmp = cache_dir.parent / "save.tmp"
        tmp.write_bytes(save.encode())
        os.replace(tmp, cache_dir / ".gitignore")
    if name == step:
        os._exit(9)
io._race_hook = hook
io.ensure_cache_dir_ignored(cache_dir)
"""


def _kill_at(tmp_path: Path, cache_dir: Path, step: str, save: str = "") -> None:
    result = subprocess.run(
        [sys.executable, "-c", _KILL_AT_STEP, str(SCRIPTS), str(cache_dir), step, save],
        env={**os.environ, "HOME": str(tmp_path / "home")},
    )
    assert result.returncode == 9


def _partial(io, tmp_path: Path) -> tuple[Path, Path]:
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    ignore = cache_dir / ".gitignore"
    ignore.write_bytes(io._CACHE_IGNORE[:8])
    return cache_dir, ignore


def _pending(io, cache_dir: Path, content: bytes) -> Path:
    """A record and capture as a repair killed right after its rename leaves."""
    probe, capture = ".gitignore.0123456789abcdef.probe", ".gitignore.0123456789abcdef.capture"
    (cache_dir / io._JOURNAL_NAME).write_bytes(io._journal_bytes(probe, capture))
    (cache_dir / capture).write_bytes(content)
    return cache_dir / capture


def test_a_repair_killed_after_the_capture_is_restored_then_finished(tmp_path: Path) -> None:
    # The live name is empty while the partial sits at the recorded capture.
    # The next run puts it back and ends; the run after completes it.
    io = _io()
    cache_dir, ignore = _partial(io, tmp_path)
    _kill_at(tmp_path, cache_dir, "repair-captured")
    assert not ignore.exists()
    _, capture = _record(io, cache_dir)
    assert _held(cache_dir) == sorted([io._JOURNAL_NAME, capture.name])
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == io._CACHE_IGNORE[:8]
    assert _held(cache_dir) == []
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == io._CACHE_IGNORE
    assert os.stat(ignore).st_nlink == 1
    assert _held(cache_dir) == []


@pytest.mark.parametrize("save", ["mine\n", "# Wr"])
def test_an_editor_save_captured_by_a_killed_repair_is_restored(tmp_path: Path, save: str) -> None:
    # The capture took the editor's save, not the partial; it goes back
    # unchanged and the defaults are never published over it. A save that is
    # itself a strict prefix is not completed in the run that restores it.
    io = _io()
    cache_dir, ignore = _partial(io, tmp_path)
    _kill_at(tmp_path, cache_dir, "repair-captured", save=save)
    _, capture = _record(io, cache_dir)
    assert capture.read_bytes() == save.encode()
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == save.encode()
    assert os.stat(ignore).st_nlink == 1
    assert _held(cache_dir) == []


def test_recovery_never_overwrites_a_newer_save(tmp_path: Path) -> None:
    # Killed after capturing an editor's save; a newer save then lands at the
    # name. Both are kept, the capture where its record says.
    io = _io()
    cache_dir, ignore = _partial(io, tmp_path)
    _kill_at(tmp_path, cache_dir, "repair-captured", save="mine\n")
    ignore.write_bytes(b"newer\n")
    _, capture = _record(io, cache_dir)
    for _ in range(2):
        io.ensure_cache_dir_ignored(cache_dir)
        assert ignore.read_bytes() == b"newer\n"
        assert capture.read_bytes() == b"mine\n"
        assert _record(io, cache_dir)[1] == capture


def test_a_kill_after_linking_the_capture_back_is_resolved(tmp_path: Path) -> None:
    # Died between restoring the capture by link and dropping its name: the
    # live file and the capture are one inode. The next run drops the extra
    # name instead of treating the capture as stuck behind a newer save.
    io = _io()
    cache_dir, ignore = _partial(io, tmp_path)
    _kill_at(tmp_path, cache_dir, "restore-linked", save="mine\n")
    _, capture = _record(io, cache_dir)
    assert os.stat(ignore).st_nlink == 2
    assert os.path.samefile(ignore, capture)
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == b"mine\n"
    assert os.stat(ignore).st_nlink == 1
    assert _held(cache_dir) == []


def test_a_kill_after_the_probe_link_is_resolved_and_repaired(tmp_path: Path) -> None:
    # The probe's second link makes the live partial nlink 2; the record is
    # checked before the fast path, so the probe is dropped and the repair
    # completes in the next run.
    io = _io()
    cache_dir, ignore = _partial(io, tmp_path)
    _kill_at(tmp_path, cache_dir, "repair-probed")
    assert os.stat(ignore).st_nlink == 2
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == io._CACHE_IGNORE
    assert os.stat(ignore).st_nlink == 1
    assert _held(cache_dir) == []


def test_a_kill_after_publishing_keeps_the_capture(tmp_path: Path) -> None:
    # The full content is live; the capture (the verified partial) is kept
    # rather than judged, and later runs write nothing.
    io = _io()
    cache_dir, ignore = _partial(io, tmp_path)
    _kill_at(tmp_path, cache_dir, "repair-published")
    assert ignore.read_bytes() == io._CACHE_IGNORE
    _, capture = _record(io, cache_dir)
    assert capture.read_bytes() == io._CACHE_IGNORE[:8]
    before = (sorted(os.listdir(cache_dir)), os.stat(cache_dir).st_mtime_ns)
    io.ensure_cache_dir_ignored(cache_dir)
    assert (sorted(os.listdir(cache_dir)), os.stat(cache_dir).st_mtime_ns) == before
    assert ignore.read_bytes() == io._CACHE_IGNORE
    assert capture.read_bytes() == io._CACHE_IGNORE[:8]


@pytest.mark.parametrize("fault", [RuntimeError, KeyboardInterrupt])
def test_an_exception_after_the_capture_puts_the_entry_back(tmp_path: Path, monkeypatch, fault) -> None:
    io = _io()
    cache_dir, ignore = _partial(io, tmp_path)

    def hook(step: str) -> None:
        if step == "repair-checked":
            saved = tmp_path / "save.tmp"
            saved.write_bytes(b"mine\n")
            os.replace(saved, ignore)
        elif step == "repair-captured":
            raise fault("interrupted")

    monkeypatch.setattr(io, "_race_hook", hook)
    if fault is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            io.ensure_cache_dir_ignored(cache_dir)
    else:
        io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == b"mine\n"
    assert _scratch(cache_dir) == []


@pytest.mark.parametrize("newer_save", [False, True])
def test_a_read_failure_after_the_capture_preserves_every_file(tmp_path: Path, monkeypatch, newer_save: bool) -> None:
    io = _io()
    cache_dir, ignore = _partial(io, tmp_path)
    real_pread = os.pread
    armed = []

    def failing_pread(fd, n, offset):
        if armed:
            raise OSError(errno.EIO, "Input/output error")
        return real_pread(fd, n, offset)

    def hook(step: str) -> None:
        if step == "repair-captured":
            if newer_save:
                ignore.write_bytes(b"newer\n")
            armed.append(True)

    monkeypatch.setattr(io.os, "pread", failing_pread)
    monkeypatch.setattr(io, "_race_hook", hook)
    io.ensure_cache_dir_ignored(cache_dir)
    if newer_save:
        assert ignore.read_bytes() == b"newer\n"
        assert _record(io, cache_dir)[1].read_bytes() == io._CACHE_IGNORE[:8]
    else:
        assert ignore.read_bytes() == io._CACHE_IGNORE[:8]
        assert _scratch(cache_dir) == []


def test_a_concurrent_run_never_publishes_over_a_pending_capture(tmp_path: Path, monkeypatch) -> None:
    # A second run arriving while the live name is empty mid-repair waits on
    # the directory lock and backs off; it does not publish the defaults.
    io = _io()
    cache_dir, ignore = _partial(io, tmp_path)
    monkeypatch.setattr(io, "_REPAIR_LOCK_TIMEOUT_S", 0.05)
    observed = []

    def hook(step: str) -> None:
        if step == "repair-captured" and not observed:
            observed.append("inner")
            io.ensure_cache_dir_ignored(cache_dir)
            observed.append(ignore.exists())

    monkeypatch.setattr(io, "_race_hook", hook)
    io.ensure_cache_dir_ignored(cache_dir)
    assert observed == ["inner", False]
    assert ignore.read_bytes() == io._CACHE_IGNORE
    assert _scratch(cache_dir) == []


def test_without_flock_support_a_pending_record_blocks_creation(tmp_path: Path, monkeypatch) -> None:
    # Where flock is reported unsupported there is no safe recovery, so
    # nothing is published over a pending capture; once the lock works the
    # capture goes back.
    io = _io()
    fcntl = pytest.importorskip("fcntl")
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    capture = _pending(io, cache_dir, b"mine\n")

    def unsupported(fd, op):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(fcntl, "flock", unsupported)
    io.ensure_cache_dir_ignored(cache_dir)
    assert _scratch(cache_dir) == sorted([io._JOURNAL_NAME, capture.name])
    assert not (cache_dir / ".gitignore").exists()
    monkeypatch.undo()
    io.ensure_cache_dir_ignored(cache_dir)
    assert (cache_dir / ".gitignore").read_bytes() == b"mine\n"
    assert _scratch(cache_dir) == []


def test_without_flock_support_creation_still_works(tmp_path: Path, monkeypatch) -> None:
    io = _io()
    fcntl = pytest.importorskip("fcntl")
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()

    def unsupported(fd, op):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(fcntl, "flock", unsupported)
    io.ensure_cache_dir_ignored(cache_dir)
    assert (cache_dir / ".gitignore").read_bytes() == io._CACHE_IGNORE
    assert _scratch(cache_dir) == []


def test_a_lock_failure_such_as_enolck_fails_closed(tmp_path: Path, monkeypatch) -> None:
    # ENOLCK is a transient shortage of lock records, not missing support:
    # the run writes nothing, and a later run creates the file.
    io = _io()
    fcntl = pytest.importorskip("fcntl")
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()

    def no_records(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", no_records)
    io.ensure_cache_dir_ignored(cache_dir)
    assert os.listdir(cache_dir) == []
    monkeypatch.undo()
    io.ensure_cache_dir_ignored(cache_dir)
    assert (cache_dir / ".gitignore").read_bytes() == io._CACHE_IGNORE


@pytest.mark.parametrize("ignore_present", [False, True])
def test_an_unrecognised_file_at_the_record_name_is_left_alone(tmp_path: Path, ignore_present: bool) -> None:
    # A user's own file under the record's name is not a record: it is never
    # parsed as one, moved or deleted. Repairs stop; creation still works.
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    foreign = cache_dir / io._JOURNAL_NAME
    foreign.write_bytes(b"my notes\n")
    ignore = cache_dir / ".gitignore"
    if ignore_present:
        ignore.write_bytes(io._CACHE_IGNORE[:8])
        inode = os.stat(ignore).st_ino
    io.ensure_cache_dir_ignored(cache_dir)
    assert foreign.read_bytes() == b"my notes\n"
    assert _scratch(cache_dir) == [io._JOURNAL_NAME]
    if ignore_present:
        assert ignore.read_bytes() == io._CACHE_IGNORE[:8]
        assert os.stat(ignore).st_ino == inode
    else:
        assert ignore.read_bytes() == io._CACHE_IGNORE


@pytest.mark.parametrize("code", [errno.EIO, errno.ENOSPC, errno.EMLINK])
def test_a_transient_or_ambiguous_link_failure_never_writes_at_the_live_name(
        tmp_path: Path, monkeypatch, code: int) -> None:
    # Only a positively unsupported link falls back to an in-place create.
    # Any such failure leaves nothing, and a later run publishes by link.
    # EPERM, which on Linux routes to the no-replace rename, has its own
    # tests below, both where that rename publishes and where it must not.
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()

    def failing(*args, **kwargs):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(io.os, "link", failing)
    monkeypatch.setattr(io, "_create_in_place", lambda *a: pytest.fail("wrote in place"))
    io.ensure_cache_dir_ignored(cache_dir)
    assert os.listdir(cache_dir) == []
    monkeypatch.undo()
    io.ensure_cache_dir_ignored(cache_dir)
    assert (cache_dir / ".gitignore").read_bytes() == io._CACHE_IGNORE


@pytest.mark.parametrize("code", ["ENOTSUP", "EOPNOTSUPP"])
def test_an_unsupported_link_falls_back_to_an_exclusive_create(tmp_path: Path, monkeypatch, code: str) -> None:
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    number = getattr(errno, code)

    def unsupported(*args, **kwargs):
        raise OSError(number, os.strerror(number))

    monkeypatch.setattr(io.os, "link", unsupported)
    io.ensure_cache_dir_ignored(cache_dir)
    assert (cache_dir / ".gitignore").read_bytes() == io._CACHE_IGNORE
    assert _scratch(cache_dir) == []


# --- Linux vfat: EPERM link, no-replace rename publish ----------------------

def _eperm_links(io, monkeypatch) -> None:
    def refused(*args, **kwargs):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(io.os, "link", refused)
    monkeypatch.setattr(io, "_create_in_place", lambda *a: pytest.fail("wrote in place"))


def _fake_renameat2(calls: list, before=None, result=None):
    """Stands in for the libc call only: no-replace rename relative to dir_fd."""
    def fake(dir_fd: int, src: str, dst: str) -> int:
        calls.append((src, dst))
        if before is not None:
            before()
        if result is not None:
            return result
        try:
            os.stat(dst, dir_fd=dir_fd, follow_symlinks=False)
            return errno.EEXIST
        except FileNotFoundError:
            os.rename(src, dst, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            return 0
    return fake


def test_on_linux_an_eperm_link_publishes_by_noreplace_rename(tmp_path: Path, monkeypatch) -> None:
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    _eperm_links(io, monkeypatch)
    calls: list = []
    monkeypatch.setattr(io, "_RENAMEAT2_PLATFORM", True)
    monkeypatch.setattr(io, "_renameat2_noreplace", _fake_renameat2(calls))
    io.ensure_cache_dir_ignored(cache_dir)
    assert (cache_dir / ".gitignore").read_bytes() == io._CACHE_IGNORE
    assert len(calls) == 1 and calls[0][1] == ".gitignore" and calls[0][0].endswith(".tmp")
    assert _scratch(cache_dir) == []


def test_the_noreplace_rename_never_overwrites_a_concurrent_save(tmp_path: Path, monkeypatch) -> None:
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    ignore = cache_dir / ".gitignore"
    _eperm_links(io, monkeypatch)
    calls: list = []
    monkeypatch.setattr(io, "_RENAMEAT2_PLATFORM", True)
    monkeypatch.setattr(io, "_renameat2_noreplace",
                        _fake_renameat2(calls, before=lambda: ignore.write_bytes(b"mine\n")))
    io.ensure_cache_dir_ignored(cache_dir)
    assert ignore.read_bytes() == b"mine\n"
    assert len(calls) == 1
    assert _scratch(cache_dir) == []


@pytest.mark.parametrize("code", [errno.EINVAL, errno.ENOSYS, errno.EPERM, errno.EACCES])
def test_a_refused_noreplace_rename_writes_nothing(tmp_path: Path, monkeypatch, code: int) -> None:
    # No flag support, no libc symbol, or a policy refusal: fail closed, and
    # never fall back to an ordinary (overwriting) rename.
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    _eperm_links(io, monkeypatch)
    calls: list = []
    monkeypatch.setattr(io, "_RENAMEAT2_PLATFORM", True)
    monkeypatch.setattr(io, "_renameat2_noreplace", _fake_renameat2(calls, result=code))
    monkeypatch.setattr(io.os, "rename", lambda *a, **k: pytest.fail("ordinary rename"))
    monkeypatch.setattr(io.os, "replace", lambda *a, **k: pytest.fail("ordinary replace"))
    io.ensure_cache_dir_ignored(cache_dir)
    assert len(calls) == 1
    assert os.listdir(cache_dir) == []


@pytest.mark.parametrize("case", ["not-linux", "no-dir-fd", "other-errno"])
def test_the_noreplace_rename_is_only_routed_for_linux_eperm_with_dir_fd(
        tmp_path: Path, monkeypatch, case: str) -> None:
    io = _io()
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    code = errno.EIO if case == "other-errno" else errno.EPERM

    def refused(*args, **kwargs):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(io.os, "link", refused)
    monkeypatch.setattr(io, "_create_in_place", lambda *a: pytest.fail("wrote in place"))
    monkeypatch.setattr(io, "_RENAMEAT2_PLATFORM", case != "not-linux")
    monkeypatch.setattr(io, "_renameat2_noreplace", lambda *a: pytest.fail("renameat2 routed"))
    if case == "no-dir-fd":
        monkeypatch.setattr(io.os, "supports_dir_fd", set())
    io.ensure_cache_dir_ignored(cache_dir)
    assert os.listdir(cache_dir) == []


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="checks the missing-symbol path")
def test_without_a_libc_renameat2_the_helper_fails_closed(tmp_path: Path) -> None:
    # The real helper, unmocked: off Linux libc has no renameat2 symbol.
    io = _io()
    (tmp_path / "src").write_bytes(b"a")
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert io._renameat2_noreplace(dir_fd, "src", "dst") != 0
    finally:
        os.close(dir_fd)
    assert sorted(os.listdir(tmp_path)) == ["src"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="real renameat2 needs Linux")
def test_real_renameat2_moves_when_free_and_refuses_when_taken(tmp_path: Path) -> None:
    io = _io()
    (tmp_path / "src").write_bytes(b"new")
    (tmp_path / "taken").write_bytes(b"old")
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        result = io._renameat2_noreplace(dir_fd, "src", "taken")
        if result in (errno.ENOSYS, errno.EINVAL):
            pytest.skip("no renameat2 or RENAME_NOREPLACE here")
        assert result == errno.EEXIST
        assert (tmp_path / "taken").read_bytes() == b"old"
        assert io._renameat2_noreplace(dir_fd, "src", "free") == 0
    finally:
        os.close(dir_fd)
    assert (tmp_path / "free").read_bytes() == b"new"
    assert not (tmp_path / "src").exists()
