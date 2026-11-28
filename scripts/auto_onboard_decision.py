#!/usr/bin/env python3
"""Decide whether a hook should ask the server for an identity.

One place answers it for every hook, so the session-start text and the Stop
hook cannot disagree about what will happen. Prints ``bind`` or
``skip:<reason>`` and always exits 0.

``UNITARES_AUTO_ONBOARD`` is read as:

    off, 0, false, no   never ask
    anchored            ask only in the process that owns a thread anchor
    on (the default),   ask
    or any other word

``UNITARES_DISABLE_AUTO_ONBOARD`` (1, true, on, yes) and
``UNITARES_CHECKINS=off`` each turn it off whatever the value.

Two things can still say no after that. The transport refuses to send
governance data anywhere but HTTPS or a loopback address, so a request to
such a server would never leave; and a server that declined this session
slot within the last hour is not asked again.

Asking is not binding. The call is marked as a harness request, and a server
whose operator accepts only agent-claimed identities declines it.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OFF_WORDS = frozenset({"0", "off", "false", "no"})
DISABLE_WORDS = frozenset({"1", "true", "on", "yes"})
DEFAULT_MODE = "on"


def transport_allows(url: str) -> bool:
    """Whether the governance transport would send a request to ``url``.

    The same rule the request itself is held to (``_http_auth``): HTTPS, or
    cleartext HTTP to a loopback address. Asking through it here keeps what
    the agent is told in step with what will actually be sent.
    """
    try:
        from _http_auth import _http_origin, _is_loopback_host
    except Exception:
        return False
    origin = _http_origin(url)
    if origin is None:
        return False
    scheme, host, _port = origin
    return scheme == "https" or (scheme == "http" and _is_loopback_host(host))


def decide(
    *,
    mode: str | None,
    disable: str | None,
    checkins: str | None,
    owns_anchor: bool,
    server_url: str,
    slot: str | None = None,
    declined=None,
) -> tuple[bool, str]:
    """(bind, reason). ``reason`` names why not, or the mode that said yes.

    ``declined(slot, server_url)`` reports a recent decline; it defaults to
    the on-disk marker.
    """
    if (disable or "").strip().lower() in DISABLE_WORDS:
        return False, "disabled"
    if (checkins or "on").strip().lower() == "off":
        return False, "checkins_off"
    word = (mode or "").strip().lower() or DEFAULT_MODE
    if word in OFF_WORDS:
        return False, "off"
    if word == "anchored" and not owns_anchor:
        return False, "not_anchor_owner"
    if not transport_allows(server_url):
        return False, "insecure_transport"
    if not owns_anchor:
        if declined is None:
            from harness_decline_marker import recently_declined as declined
        if declined(slot, server_url):
            return False, "server_declined"
    return True, "anchored" if word == "anchored" else "on"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", default="")
    parser.add_argument("--disable", default="")
    parser.add_argument("--checkins", default="on")
    parser.add_argument("--owns-anchor", default="0")
    parser.add_argument("--server-url", default="")
    parser.add_argument("--slot", default="")
    try:
        args = parser.parse_args(argv)
        bind, reason = decide(
            mode=args.mode,
            disable=args.disable,
            checkins=args.checkins,
            owns_anchor=args.owns_anchor == "1",
            server_url=args.server_url,
            slot=args.slot or None,
        )
    except (SystemExit, Exception):
        print("skip:error")
        return 0
    print("bind" if bind else f"skip:{reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
