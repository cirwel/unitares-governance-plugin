#!/usr/bin/env python3
"""When did this slot's governance binding last see a turn end?

A host is supposed to signal a resumed session with SessionStart
``source=resume``, which marks the inherited cache ``prior_process``. Codex
often does not: a thread reopened days later, even on another model, keeps
running with no SessionStart (6 of 33 resumes after gaps over three hours
fired one, 2026-10-13 to 10-22). The slot cache then still looks like this
process's own binding, and the pre-governance-call guard refused the new
process's ``force_new`` mint and offered the old identity instead.

A binding with no turn end for longer than the server's session lifetime is
treated as inherited instead. The server lets an idle session expire after
``SESSION_TTL_HOURS`` (24 by default), so such a binding no longer works
anyway. ``UNITARES_BINDING_IDLE_HOURS`` sets the threshold to match a server
configured otherwise.

``hooks/post-stop`` calls ``mark`` at the end of every bound turn: a fresh
binding gets its activity stamp refreshed, an idle one is marked
``prior_process`` (the resume the host did not report), so the next turn does
not mistake it for fresh again. Nothing here mints, rebinds or writes to the
server. The stamp holds the identity's uuid, which is not a proof value, and
the binding is rewritten in-process under the cache lock, never through a
process argument list.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_IDLE_HOURS = 24.0


def idle_hours() -> float:
    try:
        value = float(os.environ.get("UNITARES_BINDING_IDLE_HOURS", DEFAULT_IDLE_HOURS))
    except ValueError:
        return DEFAULT_IDLE_HOURS
    return value if value > 0 else DEFAULT_IDLE_HOURS


def activity_path(slot: str) -> Path:
    """The slot's stamp, under HOME whichever cache copy a hook resolved.

    A hook's working directory decides whether it reads the workspace cache or
    the HOME mirror; the stamp must not split with them.
    """
    from _session_lookup import _slot_filename

    return Path.home() / ".unitares" / Path(_slot_filename(slot)).with_suffix(".active").name


def _incarnation(uuid: str, binding: str) -> str:
    """Name one binding: its uuid and a short digest of its proof.

    A rebind of the same uuid under a new client_session_id is a new
    incarnation, so an old stamp does not judge it. The digest is one-way and
    truncated; the proof value itself is never stored.
    """
    digest = hashlib.sha256(binding.encode("utf-8")).hexdigest()[:16]
    return f"{uuid} {digest}"


def last_active(cache_path: Path, slot: str, uuid: str, binding: str) -> float | None:
    """When this binding last ended a bound turn, else when it was written.

    The stamp names the binding it was refreshed for, so other hooks' cache
    writes (post-edit, post-checkin) are not activity, and a binding minted or
    rebound after the stamp is never judged by its predecessor's. Without a
    stamp for this binding (a cache written before stamps existed, or a
    binding that has not ended a turn yet), the cache file's own write time
    is used.
    """
    stamp = activity_path(slot)
    try:
        if stamp.read_text(encoding="ascii").strip() == _incarnation(uuid, binding):
            return stamp.stat().st_mtime
    except (OSError, UnicodeDecodeError):
        pass
    try:
        return cache_path.stat().st_mtime
    except OSError:
        return None


def is_idle(
    cache_path: Path, slot: str, uuid: str, binding: str, *, now: float | None = None
) -> bool:
    seen = last_active(cache_path, slot, uuid, binding)
    if seen is None:
        return False
    current = time.time() if now is None else now
    return current - seen > idle_hours() * 3600


def touch(slot: str, uuid: str, binding: str, *, at: float | None = None) -> None:
    """Record a bound turn end for this binding (no proof value is stored).

    ``at`` back-dates the stamp: marking an idle binding inherited records
    when it was last active, so a later cache write cannot make it look fresh.
    """
    stamp = activity_path(slot)
    tmp = None
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(stamp.parent), prefix=stamp.name + ".")
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(_incarnation(uuid, binding))
        os.chmod(tmp, 0o600)
        if at is not None:
            os.utime(tmp, (at, at))
        os.replace(tmp, stamp)
    except OSError:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def mark(workspace: str, stdin_payload: str, *, now: float | None = None) -> str:
    """Refresh a fresh binding's stamp, or mark an idle one inherited.

    Returns what it did: ``touched``, ``marked_prior_process`` or ``none``.
    """
    from _session_cache_io import (
        home_session_mirror_is_valid,
        read_json_dict_unlocked,
        update_session_cache,
    )
    from _session_lookup import (
        _extract_slot,
        load_authoritative_session_for_hook,
        resolve_proof_session_file,
    )
    from session_cache import _cache_path

    slot = _extract_slot(stdin_payload)
    path = resolve_proof_session_file(slot) if slot else None
    if path is None:
        return "none"
    data = load_authoritative_session_for_hook(workspace, stdin_payload) or {}
    uuid = str(data.get("uuid") or "")
    binding = str(data.get("client_session_id") or "")
    if not (uuid and binding) or data.get("prior_process") is True:
        return "none"
    if not is_idle(path, slot, uuid, binding, now=now):
        touch(slot, uuid, binding)
        return "touched"
    # Keep the idle evidence with the binding itself: another workspace's
    # unmarked copy may later be written back over the marked one, and its
    # fresh mtime must not count as activity.
    touch(slot, uuid, binding, at=last_active(path, slot, uuid, binding))

    home_cache = _cache_path("session", Path.home(), slot)

    def same_binding(record: dict) -> bool:
        return record.get("uuid") == uuid and record.get("client_session_id") == binding

    def inherit(current: dict) -> dict | None:
        # Under the cache locks: a mint that committed since the read above
        # replaced this binding, and must not be overwritten with the old one.
        # That includes a mint from another workspace, which reaches only the
        # HOME mirror and leaves this workspace's copy stale.
        if not same_binding(current) or current.get("prior_process") is True:
            return None
        # A mirror a failed write invalidated is no evidence of a newer mint.
        if home_session_mirror_is_valid(home_cache):
            mirror = read_json_dict_unlocked(home_cache)
            if mirror.get("uuid") and not same_binding(mirror):
                return None
        # A legacy v1 token at rest would make the write refuse every time.
        if current.get("continuity_token"):
            current["continuity_token"] = ""
        current["prior_process"] = True
        return current

    # HOME is authoritative; the workspace copy is repaired as a mirror.
    try:
        _, committed = update_session_cache(
            _cache_path("session", Path(workspace), slot),
            inherit,
            home_path=home_cache,
        )
    except Exception:
        return "none"
    return "marked_prior_process" if committed else "none"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["mark"])
    parser.add_argument("--workspace", default=os.getcwd())
    args = parser.parse_args(argv)
    try:
        mark(args.workspace, sys.stdin.read())
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
