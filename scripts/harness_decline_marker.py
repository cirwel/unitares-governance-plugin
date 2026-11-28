#!/usr/bin/env python3
"""Remember, per session slot, that the server declined a harness's request.

A server whose operator accepts only agent-claimed identities answers a
harness request with a decline. Without a memory of that, every hook of the
session asks again: session-start, then each Stop. The marker lets the hooks
stop asking for a while. It is advice to this machine's hooks and nothing
else: it is never an identity, grants nothing, and expires, because the
operator may change the policy.

Thread anchors are not marked. A conversation's agent may claim an identity
later in the same turn, and the next hook must ask again to resume it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

DECLINE_TTL_SECONDS = 3600


def _marker_dir() -> Path:
    return Path(os.environ.get("HOME") or Path.home()) / ".unitares" / "harness-declined"


def _marker_path(slot: str) -> Path:
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slot)[:64]
    return _marker_dir() / f"{safe}.json"


def record_decline(slot: str | None, server_url: str) -> None:
    """Note that ``server_url`` declined this slot. Never raises."""
    if not slot:
        return
    try:
        path = _marker_path(slot)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"server_url": server_url, "at": time.time()}))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass


def recently_declined(slot: str | None, server_url: str, *, now: float | None = None) -> bool:
    """True when this server declined this slot within the last hour."""
    if not slot:
        return False
    try:
        data = json.loads(_marker_path(slot).read_text())
        at = float(data.get("at"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    if data.get("server_url") != server_url:
        return False
    age = (now if now is not None else time.time()) - at
    return 0 <= age < DECLINE_TTL_SECONDS
