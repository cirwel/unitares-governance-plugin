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
server, and the stamp holds no proof value.
"""

from __future__ import annotations

import argparse
import json
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


def activity_path(cache_path: Path) -> Path:
    return cache_path.with_suffix(".active")


def last_active(cache_path: Path) -> float | None:
    """The later of the cache's own write and the last bound turn end."""
    times = []
    for path in (cache_path, activity_path(cache_path)):
        try:
            times.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(times) if times else None


def is_idle(cache_path: Path, *, now: float | None = None) -> bool:
    seen = last_active(cache_path)
    if seen is None:
        return False
    current = time.time() if now is None else now
    return current - seen > idle_hours() * 3600


def touch(cache_path: Path) -> None:
    stamp = activity_path(cache_path)
    try:
        stamp.touch(mode=0o600, exist_ok=True)
        os.utime(stamp, None)
    except OSError:
        pass


def mark(workspace: str, stdin_payload: str, *, now: float | None = None) -> str:
    """Refresh a fresh binding's stamp, or mark an idle one inherited.

    Returns what it did: ``touched``, ``marked_prior_process`` or ``none``.
    """
    from _session_lookup import _extract_slot, load_session_for_hook, resolve_session_file

    slot = _extract_slot(stdin_payload)
    path = resolve_session_file(workspace, slot) if slot else None
    if path is None:
        return "none"
    data = load_session_for_hook(workspace, stdin_payload) or {}
    if not (data.get("uuid") and data.get("client_session_id")):
        return "none"
    if data.get("prior_process") is True:
        return "none"
    if not is_idle(path, now=now):
        touch(path)
        return "touched"
    import subprocess

    # The whole resolved binding is merged with the marker, as SessionStart's
    # resume path does, so a binding found only in the HOME mirror is written
    # out complete rather than shadowed by an identity-less file. It goes on
    # stdin: the proof value never reaches a process argument list.
    data["prior_process"] = True
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "session_cache.py"),
         "set", "session", "--workspace", workspace, "--slot", slot, "--merge"],
        input=json.dumps(data), capture_output=True, text=True, timeout=10, check=False,
    )
    return "marked_prior_process" if result.returncode == 0 else "none"


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
