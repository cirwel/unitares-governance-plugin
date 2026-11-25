#!/usr/bin/env python3
"""Shared transactions for UNITARES session-cache writers."""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

DEFAULT_SESSION_CACHE_LOCK_TIMEOUT_S = 2.0


def cache_generation_path(path: Path) -> Path:
    """Return the persistent generation/tombstone path for one cache slot."""
    return path.with_suffix(".generation")


def read_cache_generation_unlocked(path: Path) -> int:
    """Read a generation while the caller holds ``session_cache_lock``."""
    generation_path = cache_generation_path(path)
    if generation_path.is_symlink():
        return 0
    try:
        generation = int(generation_path.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return 0
    return max(0, generation)


_CACHE_DIR_NAME = ".unitares"
_CACHE_IGNORE = b"# Written by the UNITARES plugin: these session caches stay out of git.\n*\n"
_IGNORE_NAME = ".gitignore"

# Test seam: called with a step name between the helper's filesystem steps, so
# a test can run a concurrent writer at exactly that point. None in production.
_race_hook: Callable[[str], None] | None = None


def _race_point(step: str) -> None:
    if _race_hook is not None:
        _race_hook(step)


class _CacheDir:
    """Filesystem calls relative to one opened cache directory.

    With a directory handle every call resolves names against the inode that
    was opened and checked, never against the path again, so renaming the
    directory or an ancestor afterwards cannot redirect a later step. Without
    one (Windows) the calls fall back to joined paths.
    """

    def __init__(self, path: Path, fd: int | None) -> None:
        self.path = path
        self.fd = fd

    def _name(self, name: str) -> str:
        return name if self.fd is not None else os.path.join(self.path, name)

    def open(self, name: str, flags: int) -> int:
        if self.fd is not None:
            return os.open(name, flags, 0o644, dir_fd=self.fd)
        return os.open(self._name(name), flags, 0o644)

    def link(self, src: str, dst: str) -> None:
        """Hard-link ``src`` as ``dst``; never replaces an existing ``dst``."""
        kwargs: dict[str, Any] = {}
        if self.fd is not None:
            kwargs.update(src_dir_fd=self.fd, dst_dir_fd=self.fd)
        if os.link in os.supports_follow_symlinks:
            kwargs["follow_symlinks"] = False
        os.link(self._name(src), self._name(dst), **kwargs)

    def rename(self, src: str, dst: str) -> None:
        if self.fd is not None:
            os.rename(src, dst, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            os.rename(self._name(src), self._name(dst))

    def lstat(self, name: str) -> os.stat_result:
        if self.fd is not None:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        return os.stat(self._name(name), follow_symlinks=False)

    def unlink_quietly(self, name: str) -> None:
        try:
            if self.fd is not None:
                os.unlink(name, dir_fd=self.fd)
            else:
                os.unlink(self._name(name))
        except OSError:
            pass


def _write_all(fd: int, data: bytes) -> bool:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            return False
        remaining = remaining[written:]
    return True


def _scratch_name(kind: str) -> str:
    # Inside .unitares, so the published `*` covers any a killed run leaves.
    return f"{_IGNORE_NAME}.{secrets.token_hex(8)}.{kind}"


def _is_repair_candidate(info: os.stat_result) -> bool:
    # A hard-linked file is shared with a path outside this directory, so it
    # is not ours to complete.
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and info.st_size < len(_CACHE_IGNORE))


def _read_partial_ignore(fd: int, info: os.stat_result) -> bytes | None:
    """The file's bytes when ``fd`` is a single-link regular file holding a
    strict prefix of the plugin's content, which is what an interrupted write
    leaves; otherwise None."""
    if not _is_repair_candidate(info):
        return None
    existing = os.pread(fd, len(_CACHE_IGNORE), 0)
    if len(existing) != info.st_size or not _CACHE_IGNORE.startswith(existing):
        return None
    return existing


def _create_in_place(cache_dir: _CacheDir, flags: int) -> None:
    """Fallback where hard links are unavailable: exclusive create, then write.

    A run killed mid-write can leave a partial file here. Repair needs hard
    links too, so on such a filesystem that partial stays as it is.
    """
    fd = cache_dir.open(_IGNORE_NAME, os.O_CREAT | os.O_EXCL | os.O_WRONLY | flags)
    try:
        _write_all(fd, _CACHE_IGNORE)
    finally:
        os.close(fd)


def _repair_partial_ignore(cache_dir: _CacheDir, staged: str, flags: int) -> None:
    """Replace an interrupted plugin write with the full content.

    Nothing is written into the existing file. It is moved aside by one
    rename, which takes exactly whatever holds the name at that instant, and
    is then checked again through the handle opened before: same inode, same
    size and modification time, same bytes. Only then is the staged full copy
    linked into place. Otherwise the moved entry is put back. A link never
    replaces an entry, so a file someone else saved in the meantime wins.
    """
    if cache_dir.fd is None or not hasattr(os, "O_NOFOLLOW"):
        return
    try:
        # O_RDWR although nothing is written: a file the user cannot write,
        # such as a read-only opt-out, is left as it is.
        fd = cache_dir.open(_IGNORE_NAME, os.O_RDWR | getattr(os, "O_NONBLOCK", 0) | flags)
    except OSError:
        return
    try:
        checked = os.fstat(fd)
        content = _read_partial_ignore(fd, checked)
        if content is None:
            return
        # Move the entry only once it is known it can be linked back. Probing
        # the entry itself also covers filesystems without hard links and
        # Linux's fs.protected_hardlinks.
        probe = _scratch_name("probe")
        try:
            cache_dir.link(_IGNORE_NAME, probe)
        except OSError:
            return
        try:
            probed = cache_dir.lstat(probe)
        finally:
            cache_dir.unlink_quietly(probe)
        if (probed.st_dev, probed.st_ino) != (checked.st_dev, checked.st_ino):
            return
        _race_point("repair-checked")
        aside = _scratch_name("aside")
        cache_dir.rename(_IGNORE_NAME, aside)
        _race_point("repair-captured")
        moved = cache_dir.lstat(aside)
        now = os.fstat(fd)
        # ctime is not compared: the rename itself changes it.
        still_ours = ((moved.st_dev, moved.st_ino) == (checked.st_dev, checked.st_ino)
                      and (now.st_size, now.st_mtime_ns) == (checked.st_size, checked.st_mtime_ns)
                      and _read_partial_ignore(fd, now) == content)
        if still_ours:
            try:
                cache_dir.link(staged, _IGNORE_NAME)
            except FileExistsError:
                pass  # a file saved at the name meanwhile wins
            except OSError:
                still_ours = False
        if not still_ours:
            # Put back exactly what the rename took. If the name was taken
            # again meanwhile, the moved entry stays under its scratch name
            # rather than being deleted.
            try:
                cache_dir.link(aside, _IGNORE_NAME)
            except FileExistsError:
                return
            except OSError:
                # A directory cannot be hard-linked. Renaming one back never
                # replaces a file or a non-empty directory.
                if stat.S_ISDIR(moved.st_mode):
                    try:
                        cache_dir.rename(aside, _IGNORE_NAME)
                    except OSError:
                        pass
                return
        cache_dir.unlink_quietly(aside)
    finally:
        os.close(fd)


def ensure_cache_dir_ignored(directory: Path) -> None:
    """Give a workspace ``.unitares/`` a self-ignoring ``.gitignore``.

    Only a real directory named ``.unitares`` (not a symlink) that is not
    ``~/.unitares`` (the home mirror, outside any project) gets one. The caches
    here carry the session's ``client_session_id``; without this a routine
    ``git add -A`` in the user's project commits them.

    The directory is opened without following a symlink and every later step
    is relative to that handle, so a directory or ancestor swapped in after the
    open is not written into. The full content is written to a scratch file
    first and published by a hard link, which never replaces an entry, so no
    reader ever sees a partial ``.gitignore`` and a killed run leaves none.
    Where hard links are unavailable it falls back to an exclusive create.

    An existing ``.gitignore`` is never written into. It is replaced only when
    it is a single-link regular file holding a strict prefix of this content,
    which is what an interrupted write by an earlier version leaves, and only
    by the compare-and-swap in ``_repair_partial_ignore``.

    Deliberately not handled (issue #460):

    - An ancestor swapped *before* the open. A symlinked ancestor is ordinary
      (``/tmp`` and ``/var`` on macOS, projects on another volume), so walking
      the path with ``O_NOFOLLOW`` would refuse real setups, and resolving it
      first is itself the step such a swap races. The cache writers resolve
      the same path, so the file lands beside the caches it covers.
    - A writer that opened the partial file before the repair and writes
      through that handle after the repair's final check. Its bytes go to the
      moved-aside copy. Closing that window needs a lock every editor honours.
    - An entry that cannot be hard-linked (another user's file under
      ``fs.protected_hardlinks``) swapped in between the repair's probe and
      its rename. It is kept under a ``.gitignore.*.aside`` name, not deleted.

    Best effort: any failure leaves the cache write itself unaffected.
    """
    try:
        if directory.name != _CACHE_DIR_NAME or os.path.islink(directory):
            return
        try:
            # Same file, not same spelling: case-insensitive volumes and
            # symlinked homes spell one directory several ways.
            if os.path.samefile(directory, Path.home() / _CACHE_DIR_NAME):
                return
        except OSError:
            pass  # no home mirror (or no directory): nothing to compare
        # O_BINARY: on Windows a plain os.open is text mode and would write CRLF.
        flags = (getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_BINARY", 0))
        dir_fd = None
        if os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY"):
            try:
                dir_fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY | flags)
            except OSError:
                return
            # Re-check the home mirror against the directory actually opened,
            # so a swap after the samefile() check above cannot slip past it.
            try:
                opened = os.fstat(dir_fd)
                mirror = os.stat(Path.home() / _CACHE_DIR_NAME)
                if (opened.st_dev, opened.st_ino) == (mirror.st_dev, mirror.st_ino):
                    os.close(dir_fd)
                    return
            except OSError:
                pass
        try:
            _race_point("dir-opened")
            cache_dir = _CacheDir(directory, dir_fd)
            try:
                present = cache_dir.lstat(_IGNORE_NAME)
            except FileNotFoundError:
                present = None
            if present is not None and not _is_repair_candidate(present):
                return  # the usual case, a complete file: nothing is written
            staged = _scratch_name("tmp")
            fd = cache_dir.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY | flags)
            try:
                try:
                    complete = _write_all(fd, _CACHE_IGNORE)
                finally:
                    os.close(fd)
                if not complete:
                    return
                _race_point("staged")
                try:
                    cache_dir.link(staged, _IGNORE_NAME)
                except FileExistsError:
                    _repair_partial_ignore(cache_dir, staged, flags)
                except OSError:
                    _create_in_place(cache_dir, flags)
            finally:
                cache_dir.unlink_quietly(staged)
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
    except Exception:
        return


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_cache_dir_ignored(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        try:
            os.write(fd, data)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def advance_cache_generation_unlocked(path: Path) -> int:
    """Advance one generation before mutating its JSON cache."""
    generation = read_cache_generation_unlocked(path) + 1
    _atomic_write_bytes(
        cache_generation_path(path),
        f"{generation}\n".encode("ascii"),
    )
    return generation


def read_json_dict_unlocked(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_json_dict_unlocked(path: Path, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write_bytes(path, data)


def _lock_timeout_s() -> float:
    raw = os.environ.get(
        "UNITARES_SESSION_CACHE_LOCK_TIMEOUT_S",
        str(DEFAULT_SESSION_CACHE_LOCK_TIMEOUT_S),
    )
    try:
        return min(4.0, max(0.05, float(raw)))
    except ValueError:
        return DEFAULT_SESSION_CACHE_LOCK_TIMEOUT_S


@contextmanager
def session_cache_lock(
    path: Path,
    *,
    timeout_s: float | None = None,
) -> Iterator[None]:
    """Lock one cache slot through a stable sidecar across atomic replaces.

    Cache JSON files are replaced atomically, so locking the JSON inode would
    not serialize a later writer that opens the replacement. Every writer uses
    ``session-<slot>.lock`` instead and leaves that sidecar in place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_cache_dir_ignored(path.parent)
    lock_path = path.with_suffix(".lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    acquired = False
    lock_timeout = _lock_timeout_s() if timeout_s is None else max(0.0, timeout_s)
    deadline = time.monotonic() + lock_timeout
    try:
        if os.name == "nt":
            import msvcrt

            # msvcrt locks a byte range from the current offset. Keep one byte
            # in the sidecar so every process locks the same region.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while not acquired:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"session cache lock timed out: {lock_path}"
                        )
                    time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
        else:
            import fcntl

            while not acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"session cache lock timed out: {lock_path}"
                        )
                    time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def session_cache_snapshot(path: Path) -> tuple[dict[str, Any], int]:
    """Read one cache and generation atomically."""
    path, _ = _normalized_cache_paths(path, None)
    with session_cache_lock(path):
        return read_json_dict_unlocked(path), read_cache_generation_unlocked(path)


def _normalized_cache_paths(
    path: Path,
    home_path: Path | None,
) -> tuple[Path, Path | None]:
    """Resolve directory aliases without following the cache file itself."""
    path = _normalized_final_path(path)
    if home_path is None:
        return path, None
    home_path = _normalized_final_path(home_path)
    return path, None if home_path == path else home_path


def _normalized_final_path(path: Path) -> Path:
    """Resolve directory aliases while preserving final-file symlink handling."""
    path = path.expanduser()
    return path.parent.resolve() / path.name


def cache_authority_path(home_path: Path) -> Path:
    """Return the persistent slot authority outside the optional mirror dir."""
    raw = home_path.expanduser()
    home_root = raw.parent.parent.resolve()
    return home_root / ".unitares-cache-authority" / raw.name


def _transaction_paths(
    path: Path,
    home_path: Path | None,
) -> tuple[Path, Path | None, Path | None, bool]:
    """Return authoritative primary, optional workspace mirror, and authority.

    A slotted session's HOME cache is the only proof-bearing copy protected
    from a workspace writer.  Older revisions treated the workspace path as
    primary and copied it into HOME on every merge, which let a modified
    workspace cache promote attacker-controlled proof into the protected
    mirror.  When a HOME path is supplied, make it the transaction primary
    and treat the workspace path as a diagnostic/compatibility mirror.
    """
    raw_home = home_path
    workspace_path, normalized_home = _normalized_cache_paths(path, home_path)
    if raw_home is None:
        return workspace_path, None, None, False

    home_is_primary = True
    if normalized_home is None:
        path = workspace_path
        mirror_path = None
    else:
        path = normalized_home
        mirror_path = workspace_path
    authority_path = (
        _normalized_final_path(cache_authority_path(raw_home))
        if raw_home is not None else None
    )
    if authority_path is not None and authority_path in (path, mirror_path):
        raise ValueError(
            "session cache path aliases its authority record; "
            "use distinct .unitares and .unitares-cache-authority directories"
        )
    return path, mirror_path, authority_path, home_is_primary


def _read_authority_unlocked(
    authority_path: Path,
    home_cache_path: Path,
) -> dict[str, Any]:
    state = read_json_dict_unlocked(authority_path)
    if state.get("schema_version") == 1 and isinstance(state.get("generation"), int):
        return state
    return {
        "schema_version": 1,
        "generation": 0,
        "mirror_valid": home_cache_path.is_file() and not home_cache_path.is_symlink(),
    }


def _write_authority_unlocked(
    authority_path: Path,
    *,
    generation: int,
    mirror_valid: bool,
) -> None:
    write_json_dict_unlocked(
        authority_path,
        {
            "schema_version": 1,
            "generation": generation,
            "mirror_valid": mirror_valid,
        },
    )


def _advance_authority_unlocked(
    authority_path: Path,
    home_cache_path: Path,
    *,
    mirror_valid: bool | None = None,
) -> int:
    state = _read_authority_unlocked(authority_path, home_cache_path)
    generation = max(0, int(state.get("generation") or 0)) + 1
    _write_authority_unlocked(
        authority_path,
        generation=generation,
        mirror_valid=(
            bool(state.get("mirror_valid")) if mirror_valid is None else mirror_valid
        ),
    )
    return generation


def home_session_mirror_is_valid(home_path: Path) -> bool:
    """Reject stale mirrors invalidated by a failed write or explicit clear."""
    home_path = home_path.expanduser()
    authority_path = _normalized_final_path(cache_authority_path(home_path))
    home_path = _normalized_final_path(home_path)
    if home_path.is_symlink():
        return False
    if authority_path == home_path:
        return False
    if not authority_path.exists():
        return True  # Legacy mirror written before the authority contract.
    state = read_json_dict_unlocked(authority_path)
    if state.get("schema_version") == 1 and state.get("mirror_valid") is True:
        return True
    # _replace_locked transiently writes mirror_valid=False while holding the
    # authority lock before replacing the cache and restoring mirror_valid=True.
    # Re-read under the authority lock before treating False as durable so a
    # concurrent reader never mistakes an in-flight write for an invalid mirror.
    try:
        with session_cache_lock(authority_path):
            state = read_json_dict_unlocked(authority_path)
    except (OSError, TimeoutError):
        return False
    return state.get("schema_version") == 1 and state.get("mirror_valid") is True


def _read_transaction_current_unlocked(
    path: Path,
    *,
    authority_path: Path | None,
    home_is_primary: bool,
) -> dict[str, Any]:
    if home_is_primary and authority_path is not None:
        authority = _read_authority_unlocked(authority_path, path)
        if authority.get("mirror_valid") is not True:
            return {}
    return read_json_dict_unlocked(path)


@contextmanager
def _session_cache_locks(
    path: Path,
    home_path: Path | None,
    *,
    authority_path: Path | None,
    lock_mirror: bool = True,
) -> Iterator[tuple[Path | None, Exception | None]]:
    """Acquire authority, primary, then optional mirror in fixed order."""
    authority_lock = session_cache_lock(authority_path) if authority_path else None
    if authority_lock is not None:
        authority_lock.__enter__()
    try:
        primary_lock = session_cache_lock(path)
        primary_lock.__enter__()
        try:
            if home_path is None or not lock_mirror:
                yield None, None
                return
            home_lock = session_cache_lock(home_path)
            try:
                home_lock.__enter__()
            except TimeoutError:
                raise
            except OSError as exc:
                yield None, exc
                return
            try:
                yield home_path, None
            finally:
                home_lock.__exit__(None, None, None)
        finally:
            primary_lock.__exit__(None, None, None)
    finally:
        if authority_lock is not None:
            authority_lock.__exit__(None, None, None)


def reserve_session_cache_snapshot(
    path: Path,
    *,
    home_path: Path | None = None,
) -> tuple[dict[str, Any], int, int | None]:
    """Reserve invocation order and return the cache plus generation token."""
    path, home_path, authority_path, home_is_primary = _transaction_paths(
        path,
        home_path,
    )
    home_cache_path = path if home_is_primary else home_path
    with _session_cache_locks(
        path,
        home_path,
        authority_path=authority_path,
        lock_mirror=False,
    ):
        current = _read_transaction_current_unlocked(
            path,
            authority_path=authority_path,
            home_is_primary=home_is_primary,
        )
        authority_generation = None
        if authority_path is not None and home_cache_path is not None:
            authority_generation = _advance_authority_unlocked(
                authority_path,
                home_cache_path,
            )
        generation = advance_cache_generation_unlocked(path)
        return current, generation, authority_generation


def _replace_locked(
    path: Path,
    payload: dict[str, Any],
    *,
    home_path: Path | None,
    home_cache_path: Path | None,
    home_is_primary: bool,
    authority_path: Path | None,
    on_mirror_error: Callable[[Path, Exception], None] | None,
) -> None:
    """Replace primary and mirror while all available transaction locks are held."""
    authority_generation = None
    if authority_path is not None and home_cache_path is not None:
        authority_generation = _advance_authority_unlocked(
            authority_path,
            home_cache_path,
            mirror_valid=False,
        )

    mirror_ready = home_path is not None
    if mirror_ready:
        # A missing fallback is safer than a stale identity. Invalidate the
        # mirror before the authoritative primary changes.
        try:
            advance_cache_generation_unlocked(home_path)
            home_path.unlink(missing_ok=True)
        except Exception as exc:
            mirror_ready = False
            try:
                home_path.unlink(missing_ok=True)
            except OSError:
                pass
            if on_mirror_error is not None:
                on_mirror_error(home_path, exc)

    advance_cache_generation_unlocked(path)
    write_json_dict_unlocked(path, payload)

    mirror_written = home_is_primary
    if mirror_ready:
        try:
            write_json_dict_unlocked(home_path, payload)
            mirror_written = True
        except Exception as exc:
            home_path.unlink(missing_ok=True)
            if on_mirror_error is not None:
                on_mirror_error(home_path, exc)

    if authority_path is not None and authority_generation is not None and mirror_written:
        try:
            _write_authority_unlocked(
                authority_path,
                generation=authority_generation,
                mirror_valid=True,
            )
        except OSError as exc:
            if on_mirror_error is not None and home_cache_path is not None:
                on_mirror_error(home_cache_path, exc)


def replace_session_cache(
    path: Path,
    payload: dict[str, Any],
    *,
    home_path: Path | None = None,
    on_mirror_error: Callable[[Path, Exception], None] | None = None,
) -> None:
    """Replace primary and optional HOME mirror in one ordered transaction."""
    path, home_path, authority_path, home_is_primary = _transaction_paths(
        path,
        home_path,
    )
    home_cache_path = path if home_is_primary else home_path
    with _session_cache_locks(
        path,
        home_path,
        authority_path=authority_path,
    ) as (locked_home, home_error):
        if home_error is not None and on_mirror_error is not None and home_path is not None:
            on_mirror_error(home_path, home_error)
        _replace_locked(
            path,
            payload,
            home_path=locked_home,
            home_cache_path=home_cache_path,
            home_is_primary=home_is_primary,
            authority_path=authority_path,
            on_mirror_error=on_mirror_error,
        )


def update_session_cache(
    path: Path,
    mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
    *,
    home_path: Path | None = None,
    expected_generation: int | None = None,
    expected_authority_generation: int | None = None,
    on_mirror_error: Callable[[Path, Exception], None] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Run one generation-aware read-modify-write transaction.

    Returns ``(authoritative_payload, committed)``. A generation mismatch or a
    mutator returning ``None`` preserves current state and reports ``False``.
    """
    path, home_path, authority_path, home_is_primary = _transaction_paths(
        path,
        home_path,
    )
    home_cache_path = path if home_is_primary else home_path
    with _session_cache_locks(
        path,
        home_path,
        authority_path=authority_path,
    ) as (locked_home, home_error):
        if home_error is not None and on_mirror_error is not None and home_path is not None:
            on_mirror_error(home_path, home_error)
        current = _read_transaction_current_unlocked(
            path,
            authority_path=authority_path,
            home_is_primary=home_is_primary,
        )
        if (
            expected_generation is not None
            and read_cache_generation_unlocked(path) != expected_generation
        ):
            return current, False
        if expected_authority_generation is not None:
            if authority_path is None or home_cache_path is None:
                return current, False
            authority_state = _read_authority_unlocked(authority_path, home_cache_path)
            if authority_state.get("generation") != expected_authority_generation:
                return current, False
        replacement = mutator(dict(current))
        if replacement is None:
            return current, False
        _replace_locked(
            path,
            replacement,
            home_path=locked_home,
            home_cache_path=home_cache_path,
            home_is_primary=home_is_primary,
            authority_path=authority_path,
            on_mirror_error=on_mirror_error,
        )
        return replacement, True


def clear_session_cache(path: Path, *, home_path: Path | None = None) -> None:
    """Clear a cache while retaining a generation tombstone."""
    path, home_path, authority_path, home_is_primary = _transaction_paths(
        path,
        home_path,
    )
    home_cache_path = path if home_is_primary else home_path
    with _session_cache_locks(
        path,
        home_path,
        authority_path=authority_path,
    ) as (locked_home, _home_error):
        if authority_path is not None and home_cache_path is not None:
            _advance_authority_unlocked(
                authority_path,
                home_cache_path,
                mirror_valid=False,
            )
        if locked_home is not None:
            try:
                advance_cache_generation_unlocked(locked_home)
            except OSError:
                pass
            try:
                locked_home.unlink(missing_ok=True)
            except OSError:
                pass
        advance_cache_generation_unlocked(path)
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    # `python3 _session_cache_io.py ensure-ignored <dir>`: the SessionStart
    # hook's entry to ensure_cache_dir_ignored, so bash shares one
    # implementation instead of redirecting into the path itself.
    import sys

    if len(sys.argv) != 3 or sys.argv[1] != "ensure-ignored":
        print("usage: _session_cache_io.py ensure-ignored <directory>", file=sys.stderr)
        sys.exit(2)
    ensure_cache_dir_ignored(Path(sys.argv[2]))
