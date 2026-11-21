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
    anchor_bound      the slot holds an identity, and this report cannot say
                      whether the hooks created or resumed it, or whether
                      something else bound it while the hooks were trying
    anchor_bind_fail  the hooks tried to bind the anchor, could not, and the
                      slot holds no identity
    anchor_unbound    the turn ended with nothing bound and no bind attempted

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


NO_RESULT = "the onboarding helper returned no result"


def classify(raw: str, bound_csid: str = "") -> tuple[str, str, str]:
    """(status, uuid, error) for the onboarding helper's JSON result.

    ``bound_csid`` is the slot's binding read after the helper returned. A
    non-empty one means the turn has an identity even when this request did
    not provide it: another hook bound the slot while it was in flight.
    """
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        result = None
    if not isinstance(result, dict):
        result = {"status": "", "error": NO_RESULT}
    uuid = str(result.get("uuid") or "")
    bound = bool(bound_csid.strip())
    if result.get("status") == "ok" and uuid and not bound:
        # The server bound it, and the slot still reads as unbound: the local
        # cache could not be written, so this turn's check-in has no identity.
        return "anchor_bind_fail", "", "bound on the server, but the session cache holds no usable binding"
    if result.get("status") == "ok" and uuid:
        if result.get("is_new") is False:
            return "anchor_resumed", uuid, ""
        if result.get("is_new") is True:
            return "anchor_minted", uuid, ""
        return "anchor_bound", uuid, ""
    if bound:
        return "anchor_bound", "", ""
    if result.get("status") == "ok":
        error = "the onboarding helper returned no identity"
    else:
        error = str(result.get("error") or result.get("status") or "unknown error")
    return "anchor_bind_fail", "", error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slot", required=True)
    parser.add_argument("--bound-csid", default="")
    parser.add_argument(
        "--successes-only",
        action="store_true",
        help="Say nothing about a failed bind: a later hook retries and reports it.",
    )
    parser.add_argument(
        "--withdrawn",
        action="store_true",
        help="The slot had a binding the Stop hook could not use for this turn.",
    )
    parser.add_argument(
        "--unbound-reason",
        default=None,
        help="Report an anchor nothing bound, with why the hooks did not try.",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0
    try:
        if args.unbound_reason is not None:
            status, uuid, error = "anchor_unbound", "", args.unbound_reason
        else:
            status, uuid, error = classify(sys.stdin.read(), args.bound_csid)
    except Exception:
        return 0
    if args.successes_only and status == "anchor_bind_fail":
        return 0
    try:
        _append_log(
            slot=args.slot,
            event="anchor_bind",
            uuid=uuid,
            status=status,
            error=error or None,
        )
    except Exception:
        pass
    # A log that could not be written must not also cost the warning.
    try:
        # The anchor itself is not printed: a host may capture this stream,
        # and the value is what a later turn presents to resume the identity.
        if status == "anchor_unbound" and args.withdrawn:
            print(
                "[UNITARES] this turn ended with no identity for its thread "
                f"anchor: {error}. The next turn binds again.",
                file=sys.stderr,
            )
        elif status == "anchor_unbound":
            print(
                "[UNITARES] this turn ended with no identity for its thread "
                f"anchor ({error}): {UNBOUND_HELP}",
                file=sys.stderr,
            )
        elif status == "anchor_bind_fail":
            print(
                f"[UNITARES] could not bind this turn's thread anchor: {error}. "
                "This turn has no identity.",
                file=sys.stderr,
            )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
