#!/usr/bin/env python3
"""Say what happened to an orchestrated thread anchor at the end of a turn.

The Stop hook calls this only for a session that owns a thread anchor
(docs/orchestrators.md). Until this existed, every outcome was silent: an
anchor nobody bound left the conversation identity-free, a bind that failed
was swallowed, and a bind that created a new identity looked the same as one
that resumed the conversation's existing identity.

One line goes to the check-in diagnostic log (docs/check-ins.md):

    anchor_resumed    the hooks bound the anchor to its existing identity
    anchor_minted     the hooks bound the anchor and the server created a new
                      identity (the first turn, or a conversation whose earlier
                      identity the server no longer resolves)
    anchor_bind_fail  the hooks tried to bind the anchor and could not
    anchor_unbound    the turn ended with nothing bound to the anchor

The last two also go to standard error, because they mean the conversation has
no identity for this turn. Never raises and always exits 0: a diagnostic must
not fail a Stop hook.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from checkin import _append_log  # type: ignore
except Exception:  # pragma: no cover - standalone fallback

    def _append_log(**kwargs) -> None:
        pass


UNBOUND_HELP = (
    "nothing bound it. Have the agent call start_session() with no arguments, "
    "or let the hooks bind it: start the turn with `thread-anchor-exec --bind` "
    "or set UNITARES_AUTO_ONBOARD=anchored. See docs/orchestrators.md."
)


def classify(raw: str) -> tuple[str, str, str]:
    """(status, uuid, error) for the onboarding helper's JSON result."""
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        return "anchor_bind_fail", "", "onboarding helper returned no result"
    if not isinstance(result, dict):
        return "anchor_bind_fail", "", "onboarding helper returned no result"
    if result.get("status") != "ok" or not result.get("uuid"):
        error = str(result.get("error") or result.get("status") or "unknown error")
        return "anchor_bind_fail", "", error
    uuid = str(result.get("uuid"))
    if result.get("is_new") is False:
        return "anchor_resumed", uuid, ""
    if result.get("is_new") is True:
        return "anchor_minted", uuid, ""
    # A server that does not say which: report the bind, claim neither.
    return "anchor_bound", uuid, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slot", required=True)
    parser.add_argument("--anchor", default="")
    parser.add_argument(
        "--unbound-reason",
        default=None,
        help="Report an anchor nothing bound, with why the hooks did not try.",
    )
    args = parser.parse_args(argv)
    try:
        if args.unbound_reason is not None:
            status, uuid, error = "anchor_unbound", "", args.unbound_reason
        else:
            status, uuid, error = classify(sys.stdin.read())
        _append_log(
            slot=args.slot,
            event="anchor_bind",
            uuid=uuid,
            status=status,
            error=error or None,
        )
        anchor = args.anchor or "the thread anchor"
        if status == "anchor_unbound":
            print(
                f"[UNITARES] this turn ended with no identity for {anchor} "
                f"({error}): {UNBOUND_HELP}",
                file=sys.stderr,
            )
        elif status == "anchor_bind_fail":
            print(
                f"[UNITARES] could not bind {anchor}: {error}. "
                "This turn has no identity.",
                file=sys.stderr,
            )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
