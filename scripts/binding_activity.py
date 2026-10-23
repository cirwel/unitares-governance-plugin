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
import os
import sys
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


def last_active(cache_path: Path, slot: str, uuid: str) -> float | None:
    """When this binding last ended a bound turn, else when it was written.

    The stamp names the identity it was refreshed for, so other hooks' cache
    writes (post-edit, post-checkin) are not activity, and a binding minted
    after the stamp is never judged by its predecessor's. Without a stamp for
    this identity (a cache written before stamps existed, or a binding that
    has not ended a turn yet), the cache file's own write time is used.
    """
    stamp = activity_path(slot)
    try:
        if stamp.read_text(encoding="ascii").strip() == uuid:
            return stamp.stat().st_mtime
    except (OSError, UnicodeDecodeError):
        pass
    try:
        return cache_path.stat().st_mtime
    except OSError:
        return None


def is_idle(cache_path: Path, slot: str, uuid: str, *, now: float | None = None) -> bool:
    seen = last_active(cache_path, slot, uuid)
    if seen is None:
        return False
    current = time.time() if now is None else now
    return current - seen > idle_hours() * 3600


def touch(slot: str, uuid: str) -> None:
    """Record a bound turn end for ``uuid`` (an identifier, not a proof)."""
    stamp = activity_path(slot)
    tmp = stamp.with_name(stamp.name + ".tmp")
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(uuid, encoding="ascii")
        os.chmod(tmp, 0o600)
        os.replace(tmp, stamp)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def mark(workspace: str, stdin_payload: str, *, now: float | None = None) -> str:
    """Refresh a fresh binding's stamp, or mark an idle one inherited.

    Returns what it did: ``touched``, ``marked_prior_process`` or ``none``.
    """
    from _session_cache_io import update_session_cache
    from _session_lookup import _extract_slot, load_session_for_hook, resolve_session_file
    from session_cache import _cache_path

    slot = _extract_slot(stdin_payload)
    path = resolve_session_file(workspace, slot) if slot else None
    if path is None:
        return "none"
    data = load_session_for_hook(workspace, stdin_payload) or {}
    uuid = str(data.get("uuid") or "")
    binding = str(data.get("client_session_id") or "")
    if not (uuid and binding) or data.get("prior_process") is True:
        return "none"
    if not is_idle(path, slot, uuid, now=now):
        touch(slot, uuid)
        return "touched"

    def inherit(current: dict) -> dict | None:
        # Under the cache lock: a mint that committed since the read above
        # replaced this binding, and must not be overwritten with the old one.
        if (
            current.get("uuid") != uuid
            or current.get("client_session_id") != binding
            or current.get("prior_process") is True
        ):
            return None
        # A legacy v1 token at rest would make the write refuse every time.
        if current.get("continuity_token"):
            current["continuity_token"] = ""
        current["prior_process"] = True
        return current

    # The whole resolved binding stays in the write, as SessionStart's resume
    # path does, so a binding found only in the HOME mirror is written out
    # complete rather than shadowed. It never leaves this process.
    workspace_path = Path(workspace)
    try:
        _, committed = update_session_cache(
            _cache_path("session", workspace_path, slot),
            inherit,
            home_path=_cache_path("session", Path.home(), slot),
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
