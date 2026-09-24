#!/usr/bin/env python3
"""Release this session's governance presence lease at SessionEnd.

A clean exit tells the server the process is gone, so a successor can declare
it as parent right away instead of waiting out the presence-lease TTL. This is
lease cleanup, not governance delivery: it sends no check-in. It is bounded by
``--budget`` seconds, never raises, and always exits 0. A crash never reaches
here; the server's TTL remains the backstop.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _http_auth import authorization_safe_urlopen, governance_json_headers  # noqa: E402
from _session_lookup import load_session_for_hook  # noqa: E402

DEFAULT_SERVER_URL = "http://localhost:8767"


def release(workspace: str, payload: str, budget: float) -> bool:
    session = load_session_for_hook(workspace, payload)
    client_session_id = str(session.get("client_session_id") or "").strip()
    if not client_session_id:
        return False
    url = os.environ.get("UNITARES_SERVER_URL", DEFAULT_SERVER_URL).rstrip("/")
    body = json.dumps(
        {
            "name": "agent",
            "arguments": {
                "action": "release_presence",
                "client_session_id": client_session_id,
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{url}/v1/tools/call", data=body, headers=governance_json_headers()
    )
    # The server acts on the request itself; the body is never read, so a slow
    # or chunked response cannot hold the hook past its budget.
    with authorization_safe_urlopen(request, timeout=budget):
        pass
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--budget", type=float, default=0.6)
    args = parser.parse_args()
    try:
        payload = sys.stdin.read() or "{}"
    except Exception:
        return 0

    def _run() -> None:
        try:
            release(args.workspace, payload, args.budget)
        except Exception:
            pass

    # urllib's timeout bounds each socket operation, not the whole exchange, so
    # slow headers could run past the budget. A daemon thread joined for the
    # budget is a hard deadline: when it expires the process exits and the
    # thread dies with it.
    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(args.budget)
    return 0


if __name__ == "__main__":
    sys.exit(main())
