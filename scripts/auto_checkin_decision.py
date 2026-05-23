#!/usr/bin/env python3
"""Auto-checkin threshold decision for the PostToolUse hook.

Kept in its own module so the shell hook and the test suite evaluate the same
logic. The hook calls this as a subprocess; tests import `decide` directly.

Inputs (JSON on stdin or via CLI args):

    session        — shape of .unitares/session.json
    milestone      — shape of .unitares/last-milestone.json (with accumulator)
    edit_threshold — fire after N edits since last checkin
    secs_threshold — fire after T seconds since last checkin (combined with
                     edit_count >= 1; prevents firing on an empty session)

Output: a JSON object the hook consumes — most importantly `fire` (bool),
`response_text`, `complexity`, and the active `client_session_id`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

MAX_FILES_IN_SUMMARY = 5
COMPLEXITY_BASE = 0.2
COMPLEXITY_PER_FILE = 0.08
COMPLEXITY_CAP = 0.85


def decide(
    session: dict[str, Any],
    milestone: dict[str, Any],
    edit_threshold: int = 5,
    secs_threshold: int = 600,
    now: int | None = None,
) -> dict[str, Any]:
    """Evaluate whether the post-edit hook should fire an auto-checkin."""
    edit_threshold = max(1, int(edit_threshold))
    secs_threshold = max(60, int(secs_threshold))
    now = int(now if now is not None else time.time())

    edit_count = int(milestone.get("edit_count") or 0)
    files = milestone.get("files_touched") or []
    if not isinstance(files, list):
        files = []
    file_count = len(files)

    first_edit_ts = int(milestone.get("first_edit_ts") or 0)
    last_checkin_ts = int(session.get("last_checkin_ts") or 0)

    # Anchor elapsed on whichever reference is more recent. last_checkin_ts
    # acts as an authoritative floor; first_edit_ts only matters when no
    # check-in has happened since the accumulator was reset.
    anchor = max(last_checkin_ts, first_edit_ts)
    elapsed = now - anchor if anchor else 0

    fire = False
    reason = ""
    if edit_count >= edit_threshold:
        fire = True
        reason = f"edits>={edit_threshold}"
    elif edit_count >= 1 and elapsed >= secs_threshold:
        fire = True
        reason = f"elapsed>={secs_threshold}s"

    # Complexity scales with file diversity, not edit churn: touching 6 files
    # is broader work than re-saving one file six times. Capped below 1 so
    # the hook never claims maximum complexity on the agent's behalf.
    complexity = round(
        min(COMPLEXITY_BASE + COMPLEXITY_PER_FILE * file_count, COMPLEXITY_CAP),
        3,
    )

    sample = files[:MAX_FILES_IN_SUMMARY]
    extra = file_count - len(sample)
    file_summary = ", ".join(sample)
    if extra > 0:
        file_summary = (
            f"{file_summary}, +{extra} more" if file_summary else f"{extra} files"
        )

    response_text = (
        f"Auto: {edit_count} edit{'s' if edit_count != 1 else ''} "
        f"across {file_count} file{'s' if file_count != 1 else ''}"
    )
    if file_summary:
        response_text = f"{response_text} ({file_summary})"
    response_text = f"{response_text} [hook]"

    return {
        "fire": fire,
        "reason": reason,
        "client_session_id": session.get("client_session_id") or "",
        "response_text": response_text,
        "complexity": complexity,
        "edit_count": edit_count,
        "file_count": file_count,
    }


def _load_json_arg(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-json", default="{}")
    parser.add_argument("--milestone-json", default="{}")
    parser.add_argument("--edit-threshold", type=int, default=5)
    parser.add_argument("--secs-threshold", type=int, default=600)
    parser.add_argument("--now", type=int, default=None)
    args = parser.parse_args(argv)

    result = decide(
        session=_load_json_arg(args.session_json),
        milestone=_load_json_arg(args.milestone_json),
        edit_threshold=args.edit_threshold,
        secs_threshold=args.secs_threshold,
        now=args.now,
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
