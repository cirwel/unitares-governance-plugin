"""Extract sanitized slot from hook stdin JSON.

Single sanitization site for slot derivation in hooks that need to know
which slot-scoped cache file to write under (S20.1a, 2026-04-26). Hooks
that pre-date this helper (post-identity, session-start) inline the same
logic; converging them is scoped as a follow-up — see plan.md S20.1a
resolution notes.

The sanitization rule mirrors session_cache.py:_slot_suffix and
_session_lookup.py:_slot_filename — alphanumeric, hyphen, underscore;
everything else replaced with underscore; truncated to 64 chars.

Usage:
    SLOT=$(printf '%s' "${HOOK_INPUT}" | python3 scripts/_slot_from_stdin.py)

Outputs the sanitized slot on stdout. Prefer ``session_id``. For clients such
as Codex that may not expose that field on every hook event, fall back to
stable conversation/transcript fields and hash them into safe slots. Emits
empty stdout when stdin is empty, non-JSON, not a JSON object, or lacks any
known stable session field — the caller decides what to do with empty
(typically: skip slot-scoped writes rather than collapsing onto flat
session.json).
"""

from __future__ import annotations

import hashlib
import json
import sys


def _safe_slot(raw: str) -> str:
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in raw)
    return safe[:64]


def _hash_slot(prefix: str, raw: str) -> str:
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def slot_from_payload(payload: str) -> str:
    """Pure function — exposed for unit tests."""
    if not payload:
        return ""
    try:
        data = json.loads(payload)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    sid = data.get("session_id")
    if isinstance(sid, str) and sid.strip():
        return _safe_slot(sid.strip())

    cid = data.get("conversationId")
    if isinstance(cid, str) and cid.strip():
        return _safe_slot(cid.strip())

    for field in ("thread_id", "conversation_id", "transcript_path"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return _hash_slot(f"codex-{field}", value.strip())

    return ""


_HOST_SESSION_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)


def host_session_id_from_payload(payload: str) -> str:
    """The host's own session id, unsanitized, or "" when there is none.

    Unlike the slot, this is never hashed, truncated or rewritten: it is the
    id the vendor resumes by. Only an explicit ``session_id`` (or
    Antigravity's ``conversationId``) counts; the hashed fallback slots name
    a cache file, not a resumable session. Ids outside a conservative
    character set are dropped rather than rewritten.
    """
    try:
        data = json.loads(payload) if payload else None
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    for field in ("session_id", "conversationId"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            value = value.strip()
            if (len(value) <= 128 and value[0].isalnum()
                    and set(value) <= _HOST_SESSION_ID_CHARS):
                return value
            return ""
    return ""


def main() -> int:
    payload = sys.stdin.read()
    if sys.argv[1:] == ["--host-session-id"]:
        print(host_session_id_from_payload(payload))
    else:
        print(slot_from_payload(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
