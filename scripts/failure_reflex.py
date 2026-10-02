#!/usr/bin/env python3
"""Host-observed repeat-failure reflex.

A spinal reflex, not a verdict: when the host reports that the *same* tool call
has failed ``threshold`` times inside ``window`` seconds, inject one short
advisory line back into the agent's context so it stops retrying unchanged.

Why this signal: it is authored by the host, not the agent. EISV check-ins are
caller self-report and can be echoed back to look healthy; a tool failure event
cannot be talked away. The reflex fires locally in the hook process, needs no
governance identity, and performs no network I/O.

Privacy boundary (same as ``activity_observer``): the ledger never stores tool
input, tool output, or error text. A call is remembered only as a truncated
sha256 digest of its tool name plus normalized input, which is enough to say
"this exact call again" and nothing more.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from activity_observer import (  # noqa: E402
    _bounded_float,
    _read_state,
    _state_lock,
    _truthy,
    _write_state,
)
from _slot_from_stdin import slot_from_payload  # noqa: E402


SCHEMA_VERSION = 1
REFLEX_DIRNAME = "failure-reflex"
DEFAULT_THRESHOLD = 3
DEFAULT_WINDOW_S = 600.0
MAX_EVENTS = 64
# Input keys that annotate a call without changing what it does. Dropping them
# keeps a retry with a reworded Bash ``description`` recognisable as the same call.
COSMETIC_INPUT_KEYS = frozenset({"description", "timeout", "run_in_background"})


def reflex_enabled() -> bool:
    return _truthy(os.environ.get("UNITARES_FAILURE_REFLEX"), default=True)


def _threshold() -> int:
    return int(
        _bounded_float(
            os.environ.get("UNITARES_FAILURE_REFLEX_THRESHOLD"),
            DEFAULT_THRESHOLD,
            minimum=2,
            maximum=20,
        )
    )


def _window_s() -> float:
    return _bounded_float(
        os.environ.get("UNITARES_FAILURE_REFLEX_WINDOW_S"),
        DEFAULT_WINDOW_S,
        minimum=30,
        maximum=7200,
    )


def _reflex_root(home: Path | None = None) -> Path:
    configured = os.environ.get("UNITARES_FAILURE_REFLEX_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (home or Path.home()) / ".unitares" / REFLEX_DIRNAME


def reflex_state_path(home: Path, slot: str) -> Path:
    safe_slot = slot_from_payload(json.dumps({"session_id": slot})) or "slot"
    digest = hashlib.sha256(slot.encode("utf-8")).hexdigest()[:12]
    return _reflex_root(home) / f"reflex-{safe_slot}-{digest}.json"


def call_signature(tool_name: str, tool_input: Any) -> str:
    """Digest identifying "the same call" without retaining its content."""
    if isinstance(tool_input, dict):
        tool_input = {
            k: v for k, v in tool_input.items() if k not in COSMETIC_INPUT_KEYS
        }
    try:
        canonical = json.dumps(tool_input, sort_keys=True, default=str)
    except Exception:
        canonical = repr(tool_input)
    material = f"{tool_name}\0{canonical}".encode("utf-8", "replace")
    return hashlib.sha256(material).hexdigest()[:16]


def _slot(payload: dict[str, Any], payload_text: str) -> str | None:
    raw = payload.get("session_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:256]
    return slot_from_payload(payload_text)


def _is_failure(payload: dict[str, Any]) -> bool:
    return str(payload.get("hook_event_name") or "") == "PostToolUseFailure"


def reflex_message(tool_name: str, count: int, window_s: float) -> str:
    minutes = max(1, round(window_s / 60))
    return (
        f"UNITARES reflex (host-observed, advisory, not a verdict): this exact "
        f"{tool_name} call has failed {count} times in the last {minutes} min. "
        "Retrying it unchanged is unlikely to help. Read the error, change the "
        "input or the approach, or ask the operator."
    )


def observe(
    payload_text: str,
    *,
    now: float | None = None,
    home: Path | None = None,
) -> tuple[str, str | None]:
    """Record one host-reported tool failure; return (status, context or None)."""
    if not reflex_enabled():
        return "skip_disabled", None
    try:
        payload = json.loads(payload_text)
    except Exception:
        return "skip_invalid_payload", None
    if not isinstance(payload, dict):
        return "skip_invalid_payload", None
    if not _is_failure(payload):
        return "skip_not_failure", None
    if payload.get("is_interrupt") is True:
        # An operator interrupt is not the agent failing; don't count it.
        return "skip_interrupt", None
    slot = _slot(payload, payload_text)
    if not slot:
        return "skip_no_slot", None

    tool_name = str(payload.get("tool_name") or "tool")[:128]
    sig = call_signature(tool_name, payload.get("tool_input"))
    ts = time.time() if now is None else float(now)
    window_s = _window_s()
    threshold = _threshold()
    path = reflex_state_path(home or Path.home(), slot)

    try:
        with _state_lock(path, timeout_s=1.0):
            state = _read_state(path)
            events = [
                e
                for e in state.get("events", [])
                if isinstance(e, dict)
                and isinstance(e.get("t"), (int, float))
                and ts - float(e["t"]) <= window_s
            ]
            events.append({"sig": sig, "t": ts})
            events = events[-MAX_EVENTS:]
            count = sum(1 for e in events if e.get("sig") == sig)
            fire = count >= threshold and count % threshold == 0
            fired_total = int(state.get("reflex_fired_total", 0) or 0)
            if fire:
                fired_total += 1
            state.update(
                {
                    "schema_version": SCHEMA_VERSION,
                    "source": "post_tool_use_failure_hook",
                    "evidence_source": "hook_derived",
                    "network_emission": "none",
                    "events": events,
                    "failure_total": int(state.get("failure_total", 0) or 0) + 1,
                    "reflex_fired_total": fired_total,
                    "last_failure_at": ts,
                }
            )
            _write_state(path, state)
    except Exception:
        return "record_error", None

    if fire:
        return "fired", reflex_message(tool_name, count, window_s)
    return "recorded", None


def hook_output(context: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUseFailure",
                "additionalContext": context,
            }
        }
    )


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Repeat-failure reflex")
    parser.add_argument("--payload", default=None)
    args = parser.parse_args()
    payload = args.payload
    if payload is None and not sys.stdin.isatty():
        payload = sys.stdin.read()
    status, context = observe(payload or "")
    if context:
        print(hook_output(context))
    elif os.environ.get("UNITARES_FAILURE_REFLEX_DEBUG"):
        print(status, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
