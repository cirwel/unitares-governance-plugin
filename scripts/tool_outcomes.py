#!/usr/bin/env python3
"""Count what the host's transcript says happened to tool calls (Claude Stop).

The Claude Stop payload carries no tool calls, so the per-turn check-in says
"tool count unavailable". The host does write every tool call and its result to
the session transcript, and the Stop payload names that file. On each Stop this
helper reads the part of the transcript it has not read before and counts:

- ``tool_delta``: tool calls whose result landed in the window
- ``tool_error_delta``: results the host marked ``is_error``
- ``tool_not_executed_delta``: errors that the result text shows never ran: a
  refused permission, a blocking hook, an interrupt
- ``repeat_failure_delta``: executed errors identical (tool name plus input) to
  a failing call this session made before, in this window or an earlier one

The window is a position in the file, not "since the last prompt". A per-slot
ledger keeps the transcript's inode and the byte offset already counted, so a
Stop that fires twice for one prompt counts nothing twice, a turn that was
interrupted before its Stop is counted by the next one, and a new prompt
arriving while this hook runs does not empty the window. The first time a
transcript is seen, only its closing turn is counted, so history carried in by
a resume is not attributed to one window.

What the counts are, and are not:

- ``is_error`` is the host's flag. A command that exits non-zero sets it, and
  that includes a search that found nothing and a test that was meant to fail.
  The count is "results the host marked as errors", not agent failure.
- ``tool_not_executed_delta`` recognizes a short list of fixed host strings. A
  host version that words them differently leaves those results in the plain
  error count, so it is a lower bound.
- Subagent work is not counted. Subagent entries are skipped, and a subagent
  that fails many calls shows up here as one parent call.
- The last window of a session is lost when the session ends without a Stop.

Privacy boundary (same as ``activity_observer`` and ``failure_reflex``): only
counts leave the machine. Tool names, inputs, outputs and error text are read
to count and to compare, and are never sent, logged or stored. A call is kept
locally only as a sha256 digest of its name and execution-relevant input, and
the ledger is named and keyed by a digest of the host session id, not the id.

Delivery is at most once: the ledger advances before the post, so a failed post
loses that window instead of risking a double count. Disable with
``UNITARES_TOOL_OUTCOMES=off``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

from activity_observer import (  # noqa: E402
    _lock_timeout_s,
    _read_state,
    _state_lock,
    _truthy,
    _write_state,
)

SCHEMA_VERSION = 1
OBSERVATION_KIND = "tool_outcome_rollup"
WINDOW_BASIS = "transcript_cursor"
LEDGER_DIRNAME = "claude-tool-outcomes"
READ_BOUND_BYTES = 8 * 1024 * 1024
MAX_PENDING_CALLS = 256
MAX_FAILED_DIGESTS = 64
MAX_COMPLETED_IDS = 256
_RESULT_TEXT_PREFIX = 400
_EVENT_NAMESPACE = uuid.UUID("0b0f3a5e-6a55-4d59-9a39-6f2f4b0f8a11")

# Bash input fields that describe or schedule a command without changing what
# it runs. A retried command usually carries a reworded description, which
# would otherwise hide the repeat.
_NON_EXECUTION_KEYS = {
    "Bash": frozenset({"description", "timeout", "run_in_background"}),
}

# Fixed strings the host writes at the start of the result of a call that
# never ran. Compared casefolded against the start of the result text only.
# Anchored there on purpose: a command that ran and merely printed one of them
# is an executed error.
_NOT_EXECUTED_PREFIXES = (
    "pretooluse:",
    "[request interrupted by user",
    "permission to use",
    "the user doesn't want to proceed with this tool use",
    "the user doesn't want to take this action right now",
    "claude requested permissions to",
)


def enabled() -> bool:
    return (
        _truthy(os.environ.get("UNITARES_TOOL_OUTCOMES"), default=True)
        and os.environ.get("UNITARES_CHECKINS", "on").strip().lower() != "off"
    )


def ledger_path(home: Path, slot: str) -> Path:
    configured = os.environ.get("UNITARES_TOOL_OUTCOMES_DIR")
    root = (
        Path(configured).expanduser().resolve()
        if configured
        else home / ".unitares" / LEDGER_DIRNAME
    )
    # Named by digest only: the raw host session id is not written anywhere.
    return root / f"outcomes-{_slot_hash(slot)}.json"


def _slot_hash(slot: str) -> str:
    return hashlib.sha256(slot.encode("utf-8")).hexdigest()[:32]


def _open_transcript(path: Any) -> int | None:
    if not isinstance(path, str) or not path.startswith("/") or not path.endswith(".jsonl"):
        return None
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError):  # ValueError: embedded NUL or lone surrogate
        return None
    try:
        # fstat on the opened descriptor, so the regular-file check cannot race.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


def _read_range(fd: int, start: int, end: int) -> bytes:
    os.lseek(fd, start, os.SEEK_SET)
    chunks = []
    remaining = end - start
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _entries(data: bytes) -> list[tuple[int, Mapping[str, Any]]]:
    """Parse complete lines into ``(offset_after_line, entry)`` pairs."""
    parsed: list[tuple[int, Mapping[str, Any]]] = []
    position = 0
    for line in data.split(b"\n")[:-1]:  # the last piece has no newline yet
        position += len(line) + 1
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            # RecursionError: absurdly nested JSON. Skipped like any bad line,
            # so one such line cannot stall the cursor for good.
            continue
        if isinstance(entry, dict):
            parsed.append((position, entry))
    return parsed


def _blocks(entry: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, Mapping)]


def _opens_a_turn(entry: Mapping[str, Any]) -> bool:
    """A user entry that is not a tool result. Used only on first sight."""
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return False
    message = entry.get("message")
    if not isinstance(message, Mapping):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(
            isinstance(block, Mapping) and block.get("type") == "tool_result"
            for block in content
        )
    return False


def _call_digest(name: Any, tool_input: Any) -> str:
    dropped = _NON_EXECUTION_KEYS.get(str(name), frozenset())
    if dropped and isinstance(tool_input, Mapping):
        tool_input = {key: value for key, value in tool_input.items() if key not in dropped}
    try:
        canonical = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        canonical = repr(tool_input)
    return hashlib.sha256(f"{name}\x00{canonical}".encode("utf-8", "replace")).hexdigest()


def _result_text(block: Mapping[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content[:_RESULT_TEXT_PREFIX]
    if isinstance(content, list):
        for part in content:
            if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                return part["text"][:_RESULT_TEXT_PREFIX]
    return ""


def _never_ran(block: Mapping[str, Any]) -> bool:
    return _result_text(block).casefold().lstrip().startswith(_NOT_EXECUTED_PREFIXES)


def collect_window(
    transcript_path: Any,
    state: Mapping[str, Any] | None,
    *,
    read_bound: int = READ_BOUND_BYTES,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Count the unread part of a transcript.

    Returns ``(counts, next_state)``, or None when the transcript cannot be
    read as a regular ``*.jsonl`` file. ``next_state`` is what to store so the
    next call starts where this one stopped. A window with nothing in it
    returns zeros: that is a reading, not a missing one.
    """
    fd = _open_transcript(transcript_path)
    if fd is None:
        return None
    try:
        info = os.fstat(fd)
        size = info.st_size
        inode = int(info.st_ino)
        previous = dict(state or {})
        same_file = previous.get("inode") == inode
        try:
            offset = int(previous.get("offset", 0)) if same_file else 0
        except (TypeError, ValueError):
            offset = 0
        first_sight = not same_file
        truncated = False
        if same_file and offset > size:
            # The file shrank under the same inode: the cursor means nothing.
            offset, first_sight, truncated = 0, True, True
        start = offset
        if size - start > read_bound:
            start = size - read_bound
            truncated = True
        data = _read_range(fd, start, size)
    except OSError:
        return None
    finally:
        os.close(fd)

    if start > 0 and (start != offset or first_sight):
        # The read began mid-file at a point that is not a known line start.
        cut = data.find(b"\n")
        if cut < 0:
            data, start = b"", start + len(data)
        else:
            data, start = data[cut + 1:], start + cut + 1

    entries = _entries(data)
    # The cursor stops after the last complete line, parsed or not, so a
    # half-written last line is left whole for the next call.
    # A line larger than the bound was already stepped past by the trim above,
    # which leaves nothing here.
    end = start + data.rfind(b"\n") + 1

    if first_sight:
        # Count only the closing turn, so history carried in by a resume is
        # not attributed to one window.
        for index in range(len(entries) - 1, -1, -1):
            if _opens_a_turn(entries[index][1]):
                entries = entries[index + 1:]
                break
        else:
            truncated = truncated or start > 0

    completed: list[str] = []
    if first_sight:
        pending: dict[str, str] = {}
        failed: list[str] = []
    else:
        raw_completed = previous.get("completed")
        if isinstance(raw_completed, list):
            completed = [str(c) for c in raw_completed]
        raw_pending = previous.get("pending")
        pending = (
            {str(k): str(v) for k, v in raw_pending.items()}
            if isinstance(raw_pending, Mapping)
            else {}
        )
        raw_failed = previous.get("failed")
        failed = [str(d) for d in raw_failed] if isinstance(raw_failed, list) else []

    tool_delta = errors = not_executed = repeats = 0
    # Results already counted, in this window or a recent one: the host can
    # log an entry twice, and the second copy may land past the cursor.
    seen_results: set[str] = set(completed)
    for _, entry in entries:
        if entry.get("isSidechain"):
            continue
        kind = entry.get("type")
        if kind == "assistant":
            for block in _blocks(entry):
                if block.get("type") != "tool_use":
                    continue
                call_id = str(block.get("id") or "")
                # The host can log one message as several entries; an id is
                # recorded once.
                if call_id and call_id not in pending and call_id not in seen_results:
                    pending[call_id] = _call_digest(block.get("name"), block.get("input"))
        elif kind == "user":
            for block in _blocks(entry):
                if block.get("type") != "tool_result":
                    continue
                call_id = str(block.get("tool_use_id") or "")
                if not call_id or call_id in seen_results:
                    continue
                seen_results.add(call_id)
                completed.append(call_id)
                digest = pending.pop(call_id, None)
                tool_delta += 1
                if block.get("is_error") is not True:
                    if digest in failed:
                        failed.remove(digest)  # the call worked: no longer a failing call
                    continue
                errors += 1
                if _never_ran(block):
                    not_executed += 1
                    continue
                if digest is None:
                    continue  # the call itself was outside what could be read
                if digest in failed:
                    repeats += 1
                else:
                    failed.append(digest)

    while len(pending) > MAX_PENDING_CALLS:
        pending.pop(next(iter(pending)))
    failed = failed[-MAX_FAILED_DIGESTS:]
    completed = completed[-MAX_COMPLETED_IDS:]

    counts = {
        "tool_delta": tool_delta,
        "tool_error_delta": errors,
        "tool_not_executed_delta": not_executed,
        "repeat_failure_delta": repeats,
        "window_truncated": truncated,
        "window_basis": WINDOW_BASIS,
    }
    next_state = {
        "schema_version": SCHEMA_VERSION,
        "inode": inode,
        "offset": end,
        "window_start": start,
        "pending": pending,
        "failed": failed,
        "completed": completed,
    }
    return counts, next_state


def advance(
    transcript_path: Any,
    slot: str,
    *,
    home: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Collect one window and store the cursor, under the slot's lock."""
    path = ledger_path(home or Path.home(), slot)
    with _state_lock(path, timeout_s=_lock_timeout_s()):
        state = _read_state(path)
        if state.get("slot_hash") not in (None, _slot_hash(slot)):
            state = {}
        collected = collect_window(transcript_path, state)
        if collected is None:
            return None
        counts, next_state = collected
        next_state["slot_hash"] = _slot_hash(slot)
        next_state["updated_at"] = time.time()
        _write_state(path, next_state)
    return counts, next_state


def build_payload(
    counts: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    agent_uuid: str,
    client_session_id: str,
    slot: str,
    plugin_version: str,
    now: float | None = None,
) -> dict[str, Any]:
    observed_at = datetime.fromtimestamp(
        time.time() if now is None else now, timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    key = "|".join([
        agent_uuid,
        client_session_id,
        OBSERVATION_KIND,
        str(state.get("inode")),
        str(state.get("window_start")),
        str(state.get("offset")),
    ])
    return {
        "agent_uuid": agent_uuid,
        "client_session_id": client_session_id,
        "observation_kind": OBSERVATION_KIND,
        "host_family": "claude",
        "execution_mode": "unknown",
        "execution_mode_source": "unspecified",
        "slot_hash": _slot_hash(slot),
        "observed_at": observed_at,
        "plugin_version": plugin_version,
        "event_id": str(uuid.uuid5(_EVENT_NAMESPACE, key)),
        **{field: counts[field] for field in (
            "tool_delta",
            "tool_error_delta",
            "tool_not_executed_delta",
            "repeat_failure_delta",
            "window_truncated",
            "window_basis",
        )},
    }


def run(
    payload_text: str,
    *,
    agent_uuid: str,
    client_session_id: str,
    slot: str,
    server_url: str,
    home: Path | None = None,
    sender: Any = None,
) -> str:
    """Count the window this Stop closes and post it. Returns a status word."""
    if not enabled():
        return "skip_disabled"
    if not agent_uuid or not client_session_id or not slot:
        return "skip_unbound"
    try:
        payload = json.loads(payload_text) if payload_text.strip() else {}
    except json.JSONDecodeError:
        return "skip_invalid_payload"
    if not isinstance(payload, dict):
        return "skip_invalid_payload"
    try:
        advanced = advance(payload.get("transcript_path"), slot, home=home)
    except Exception:
        return "ledger_error"
    if advanced is None:
        return "skip_no_transcript"
    counts, state = advanced
    if state.get("offset") == state.get("window_start") and counts["tool_delta"] == 0:
        return "skip_nothing_new"
    from checkin import _plugin_version

    if sender is None:
        from runtime_observer import _post_runtime as sender
    body = build_payload(
        counts,
        state,
        agent_uuid=agent_uuid,
        client_session_id=client_session_id,
        slot=slot,
        plugin_version=_plugin_version(),
    )
    try:
        return "sent" if sender(server_url, body) else "post_failed"
    except Exception:
        return "post_failed"


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Post Claude tool outcomes for one Stop")
    parser.add_argument("--uuid", default="")
    parser.add_argument("--client-session-id", default="")
    parser.add_argument("--slot", default="")
    parser.add_argument("--server-url", default="http://localhost:8767")
    args = parser.parse_args()
    payload_text = "" if sys.stdin.isatty() else sys.stdin.read()
    print(run(
        payload_text,
        agent_uuid=args.uuid.strip(),
        client_session_id=args.client_session_id.strip(),
        slot=args.slot.strip(),
        server_url=args.server_url,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
