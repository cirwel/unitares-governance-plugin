#!/usr/bin/env python3
"""Decide whether a hook should ask the server for an identity.

One place answers it for every hook, so the session-start text and the Stop
hook cannot disagree about what will happen. Prints ``bind`` or
``skip:<reason>`` and always exits 0.

``UNITARES_AUTO_ONBOARD`` is read as:

    off, 0, false, no   never ask
    anchored            ask only in the process that owns a thread anchor
    auto (the default)  ask when the server is one this machine has reason to
                        trust with the session's name, workspace and host
                        session id: a loopback address, or an HTTPS address
                        the operator set explicitly
    on, or any other    ask

``UNITARES_DISABLE_AUTO_ONBOARD`` (1, true, on, yes) and ``UNITARES_CHECKINS=off``
each turn it off whatever the value.

Asking is not binding. The call is marked as a harness request, and a server
whose operator accepts only agent-claimed identities declines it. Under
``auto`` the client decides only whether the endpoint may be asked at all.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from urllib.parse import urlsplit

OFF_WORDS = frozenset({"0", "off", "false", "no"})
DISABLE_WORDS = frozenset({"1", "true", "on", "yes"})
DEFAULT_MODE = "auto"


def is_loopback_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").strip().lower()
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def endpoint_trusted(url: str, *, explicit: bool) -> bool:
    """Whether ``auto`` may ask this endpoint.

    Loopback is this machine. Anything else must have been named by the
    operator and be HTTPS, so the answer comes from the server that was meant.
    """
    if is_loopback_url(url):
        return True
    try:
        scheme = (urlsplit(url).scheme or "").lower()
    except ValueError:
        return False
    return explicit and scheme == "https"


def decide(
    *,
    mode: str | None,
    disable: str | None,
    checkins: str | None,
    owns_anchor: bool,
    server_url: str,
    url_explicit: bool,
) -> tuple[bool, str]:
    """(bind, reason). ``reason`` names why not, or the mode that said yes."""
    if (disable or "").strip().lower() in DISABLE_WORDS:
        return False, "disabled"
    if (checkins or "on").strip().lower() == "off":
        return False, "checkins_off"
    word = (mode or "").strip().lower() or DEFAULT_MODE
    if word in OFF_WORDS:
        return False, "off"
    if word == "anchored":
        return (True, "anchored") if owns_anchor else (False, "not_anchor_owner")
    if word == "auto":
        if endpoint_trusted(server_url, explicit=url_explicit):
            return True, "auto"
        return False, "untrusted_endpoint"
    return True, "on"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", default="")
    parser.add_argument("--disable", default="")
    parser.add_argument("--checkins", default="on")
    parser.add_argument("--owns-anchor", default="0")
    parser.add_argument("--server-url", default="")
    parser.add_argument("--url-explicit", default="0")
    try:
        args = parser.parse_args(argv)
        bind, reason = decide(
            mode=args.mode,
            disable=args.disable,
            checkins=args.checkins,
            owns_anchor=args.owns_anchor == "1",
            server_url=args.server_url,
            url_explicit=args.url_explicit == "1",
        )
    except (SystemExit, Exception):
        print("skip:error")
        return 0
    print("bind" if bind else f"skip:{reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
