#!/usr/bin/env python3
"""Record a host context compaction on the identity that was bound when it happened.

Called by ``hooks/session-start`` on ``source=compact`` once the server has
answered its health check. Posts one ``context_compaction`` observation to the
identity-bound ``/v1/runtime/observe`` route, which stores it in the audit plane
only: no EISV, no check-in, no session-activity refresh, and no identity or
lineage write. The server pairs it later with what followed (identity kept or
a ``spawn_reason="compaction"`` child minted).

It reports only for the exact slot's usable binding, the same one the
compaction guidance names. A resumed session's inherited ``prior_process``
cache is not this process's binding and is never reported. Proof values are
sent to the server and nowhere else. The script prints only the opt-in
continuity question (``continuity_probe.py``), and only once the compaction
was recorded. Every failure is silent, and ``UNITARES_CHECKINS=off`` disables
it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _http_auth import authorization_safe_urlopen, governance_json_headers  # noqa: E402
from _session_cache_io import read_json_dict_unlocked  # noqa: E402
from _session_lookup import _extract_slot, resolve_session_file  # noqa: E402
from checkin import _plugin_version  # noqa: E402

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
HOSTS = {"claude", "codex"}
DEFAULT_TIMEOUT_S = 1.5
# urllib's timeout is per socket operation, so a slow-trickling server could
# hold SessionStart longer; this caps the whole report where SIGALRM exists.
WALL_CLOCK_LIMIT_S = 3


def build_payload(event_text: str, *, host: str, cwd: str) -> dict[str, Any] | None:
    """Return the observation for a compact event with a usable binding, else None."""
    if host not in HOSTS:
        return None
    try:
        event = json.loads(event_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict) or event.get("source") != "compact":
        return None
    slot = _extract_slot(event_text)
    if not slot:
        return None
    path = resolve_session_file(cwd, slot)
    cache = read_json_dict_unlocked(path) if path is not None else {}
    if cache.get("prior_process") is True:
        return None
    agent_uuid = cache.get("uuid") or cache.get("agent_uuid")
    binding = cache.get("client_session_id")
    if not (isinstance(agent_uuid, str) and _UUID_RE.fullmatch(agent_uuid)):
        return None
    if not (isinstance(binding, str) and binding.strip()):
        return None
    return {
        "agent_uuid": agent_uuid,
        "client_session_id": binding.strip(),
        "observation_kind": "context_compaction",
        "host_family": host,
        "execution_mode": "unknown",
        "execution_mode_source": "unspecified",
        "slot_hash": hashlib.sha256(slot.encode("utf-8")).hexdigest()[:32],
        "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "plugin_version": _plugin_version(),
    }


def post(server_url: str, payload: dict[str, Any], *, timeout: float) -> bool:
    request = urllib.request.Request(
        f"{server_url.rstrip('/')}/v1/runtime/observe",
        data=json.dumps(payload).encode("utf-8"),
        headers=governance_json_headers(),
        method="POST",
    )
    try:
        with authorization_safe_urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        return isinstance(body, dict) and body.get("success") is True
    except (urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("event", help="The SessionStart hook payload (JSON).")
    args = parser.parse_args(argv)
    if os.environ.get("UNITARES_CHECKINS", "on").strip().lower() == "off":
        return 0
    try:
        payload = build_payload(args.event, host=args.host, cwd=os.getcwd())
        if payload is not None and post(args.server_url, payload, timeout=args.timeout):
            # The opt-in continuity question, asked only about a compaction
            # that was recorded, so its answer has something to sit beside.
            from continuity_probe import ask

            question = ask(args.event, cwd=os.getcwd(), host=args.host, compaction=payload)
            if question:
                print(question, flush=True)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    # The wall-clock cap belongs to the hook's own process only; installing it
    # inside main() would leave a pending alarm in any process that imports it.
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
        signal.alarm(WALL_CLOCK_LIMIT_S)
    raise SystemExit(main())
