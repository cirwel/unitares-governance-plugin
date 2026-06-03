#!/usr/bin/env python3
"""Transport-neutral local cache helper for UNITARES client adapters.

Stores lightweight continuity state in:

    .unitares/session-<slot>.json
    .unitares/last-milestone.json

The flat `.unitares/session.json` path exists only as a legacy/shared cache
surface. New session writes must be slot-scoped unless the caller explicitly
opts into the substrate-earned single-tenant escape hatch.

This helper is intentionally small and dependency-free so Claude hooks, Codex
commands, and other thin clients can share one cache format.

Session-cache schema versions
-----------------------------

* v1 (pre-S11): ``continuity_token`` was written by ``hooks/post-identity``
  and treated by ``hooks/session-start`` as a resume credential. Under the
 identity ontology (````), this
  performatively claimed cross-process-instance continuity without earning
  it. v1 caches may still exist on disk; the token field is treated as
  read-only legacy — downstream readers must not promote it back into a
  resume suggestion.
* v2 (post-S11): ``hooks/post-identity`` writes
  ``schema_version: 2`` and empties ``continuity_token``. The cache's UUID
  is surfaced by the next session's ``session-start`` hook as a
  ``parent_agent_id`` *lineage candidate* — a predecessor the fresh
  process-instance declares it inherits from, not an identity it resumes.

This helper itself is schema-agnostic (it marshals any JSON dict). The
schema contract lives at the hook layer; this docstring records it for
readers who grep for ``schema_version`` or ``continuity_token`` here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CACHE_DIR = ".unitares"
CACHE_FILES = {
    "session": "session.json",
    "milestone": "last-milestone.json",
}

# Mirrors the post-sanitization shape produced by `_slot_suffix`. Used by
# `_parse_session_filename` to reject filenames that bypassed the writer
# (a same-UID actor can drop arbitrary `session-*.json` directly on disk;
# the parsed slot is later reflected into agent context, where backticks
# or whitespace would break the surrounding markdown code-span).
_SLOT_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _workspace_path(raw: str | None) -> Path:
    base = raw or os.getcwd()
    return Path(base).expanduser().resolve()


def _slot_suffix(slot: str | None) -> str:
    """Safe-filename slot suffix. Matches onboard_helper/_session_lookup."""
    if not slot:
        return ""
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slot)
    return safe[:64]


def _cache_path(kind: str, workspace: Path, slot: str | None = None) -> Path:
    try:
        filename = CACHE_FILES[kind]
    except KeyError as exc:
        raise ValueError(f"unknown cache kind: {kind}") from exc
    # Only the session cache is slot-scoped — milestone accumulator is
    # workspace-level (per the auto-checkin design).
    safe_slot = _slot_suffix(slot) if kind == "session" else ""
    if safe_slot:
        stem, _, ext = filename.rpartition(".")
        filename = f"{stem}-{safe_slot}.{ext}"
    return workspace / CACHE_DIR / filename


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic write with mode 0600.

    The session cache carries continuity tokens. A world-readable cache
    (the default when using Path.write_text, which inherits umask 022)
    lets any same-UID process impersonate the cached identity against
    the governance API. Inlined rather than imported from unitares_sdk
    because this helper is intentionally dependency-free — shared by
    thin plugin clients that don't pull in the SDK.

    On any write/chmod/replace failure, the temp file is unlinked rather
    than left as a turd in the cache directory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        try:
            os.write(fd, data)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    raw = args.json
    if raw is None and not sys.stdin.isatty():
        raw = sys.stdin.read()
    if raw is None:
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")
    return data


def cmd_path(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    print(_cache_path(args.kind, workspace, getattr(args, "slot", None)))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    payload = _read_json(_cache_path(args.kind, workspace, getattr(args, "slot", None)))
    if args.key:
        value = payload.get(args.key)
        if value is None:
            return 0
        if isinstance(value, (dict, list)):
            print(json.dumps(value))
        else:
            print(value)
        return 0
    print(json.dumps(payload))
    return 0


_SESSION_IDENTITY_FIELDS = ("uuid", "client_session_id", "continuity_token")


def cmd_set(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    slot = getattr(args, "slot", None)
    allow_shared = bool(getattr(args, "allow_shared", False))

    if args.kind == "session" and not slot and not allow_shared:
        # Slotless session writes produce flat `session.json`, the workspace-
        # shared "current owner" file every same-UID process can read. Hook
        # layer (PR #19) refuses to read it; helper now refuses to write it.
        # Convention-level: a determined caller can still write JSON directly
        # to the path (axiom #14). The earned defense lives in S1-A′ + S19.
        print(
            "session_cache.py: refusing slotless session write — pass --slot <id> "
            "(substrate-earned single-tenant: --allow-shared; convention-level — "
            "direct file writes still bypass)",
            file=sys.stderr,
        )
        return 2

    path = _cache_path(args.kind, workspace, slot)
    payload = _load_payload(args)
    if args.merge:
        existing = _read_json(path)
        if args.kind == "session":
            # Auto-migrate v1 legacy tokens during merge: a pre-existing
            # slot file from before S11/S20 may carry a real continuity_token
            # at rest. Without this strip, the token-rejection check below
            # would fire on every merge against such a cache and brick
            # callers like the post-edit auto-checkin stamp (errors swallowed
            # via `|| true`). The strip is one-way: we never re-introduce a
            # legacy token; we only let new writes (whose own continuity_token
            # is checked below) succeed. Stderr breadcrumb keeps the
            # migration legible per axiom #14.
            stale_token = existing.get("continuity_token")
            if isinstance(stale_token, str) and stale_token.strip():
                existing.pop("continuity_token", None)
                print(
                    f"session_cache.py: [V1_LEGACY_STRIP] dropped pre-existing "
                    f"continuity_token from {path} during merge",
                    file=sys.stderr,
                )
        existing.update(payload)
        payload = existing

    if args.kind == "session":
        token = payload.get("continuity_token")
        # Literal empty string is the v2 hook erasure path (passes). Any
        # non-empty string is rejected — including whitespace-only values,
        # since `bool(" ")` is True in Python and downstream readers that
        # test `if continuity_token:` would treat it as a credential.
        if isinstance(token, str) and token:
            print(
                "session_cache.py: refusing session payload with non-empty "
                "continuity_token — v2 ontology stores lineage, not resume "
                "credentials (write empty string to erase, or omit the field; "
                "to recover a legacy slot file, run: clear session --slot <id>)",
                file=sys.stderr,
            )
            return 2
        if not any(k in payload for k in _SESSION_IDENTITY_FIELDS):
            # A session cache with NONE of [uuid, client_session_id, continuity_token]
            # is a stub: subsequent hooks read it, find no addressable identity,
            # and silently no-op. Refuse so the failure is visible (caller
            # ignores via `|| true`) instead of silently bricking the next
            # hook's identity lookup.
            print(
                "session_cache.py: refusing to write session cache without any identity field "
                f"(need at least one of {list(_SESSION_IDENTITY_FIELDS)})",
                file=sys.stderr,
            )
            return 1

    if args.stamp:
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(path, payload)

    # Slotted HOME mirror for session caches (2026-05-30).
    # `_session_lookup.resolve_session_file` has a slotted-HOME fallback
    # added 2026-05-10 to fix the PWD-mismatch failure mode where
    # post-identity writes from PWD=X but a later hook reads from PWD=Y and
    # misses. The fix was read-side only — the writer never populated HOME,
    # so the fallback never had a file to find. This mirror closes the loop:
    # session writes hit BOTH workspace AND $HOME/.unitares/session-<slot>.json,
    # so the slotted-HOME read fallback actually works.
    #
    # Identity-honesty unchanged: slot is the Claude Code session_id, globally
    # unique per session, so cross-agent siphoning is structurally precluded
    # (cf. the unslotted-HOME removal noted in _session_lookup.py). Milestone
    # accumulator stays workspace-scoped per the auto-checkin design.
    if args.kind == "session" and slot:
        home_path = _cache_path("session", Path.home(), slot)
        if home_path != path:
            try:
                _write_json(home_path, payload)
            except Exception as exc:
                # Best-effort — primary workspace write already succeeded.
                # Failure here only loses the PWD-mismatch fallback path,
                # not the primary cache.
                print(
                    f"session_cache.py: home-mirror write failed ({exc!r}) — "
                    f"primary cache at {path} unaffected",
                    file=sys.stderr,
                )

    if args.echo:
        print(json.dumps(payload))
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    workspace = _workspace_path(args.workspace)
    path = _cache_path(args.kind, workspace, getattr(args, "slot", None))
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return 0


def _parse_session_filename(name: str) -> str | None:
    """Recover the slot suffix from a session-*.json filename.

    Returns the slot string (the segment between ``session-`` and ``.json``),
    or ``None`` for the flat ``session.json`` (no slot) and for any name that
    does not match the pattern. Slot strings here are pre-sanitized (the
    writer ran them through ``_slot_suffix``) so callers receive the safe
    form already on disk — but a same-UID actor can write filenames
    directly, bypassing the writer, so the parsed slot is re-validated
    against ``_SLOT_PATTERN`` before being returned.
    """
    if not name.startswith("session-") or not name.endswith(".json"):
        return None
    raw = name[len("session-") : -len(".json")]
    if not raw or not _SLOT_PATTERN.fullmatch(raw):
        return None
    return raw


def cmd_list(args: argparse.Namespace) -> int:
    """List slot inventory for the session cache, newest first.

    Emits a JSON array of ``{slot, parent_agent_id, prior_client_session_id,
    updated_at, path}`` objects. Field names are deliberately the v2
    declared-lineage parameters of ``onboard()`` so consumers naturally
    flow into ``onboard(force_new=true, parent_agent_id=entry["parent_agent_id"])``
    — declared lineage, not resume. The scan-newest fallback (S20 §2b) is
    a *lineage candidate surface*, never a resume credential.

    Entries with neither identity field are filtered: a null-identity row
    has no actionable lineage hint and would silently mis-rank the
    scan-newest pick if it sorted to the top by ``updated_at``. Malformed
    JSON is skipped silently — this is a discovery surface, not a
    validator.
    """
    workspace = _workspace_path(args.workspace)
    cache_dir = workspace / CACHE_DIR
    entries: list[dict[str, Any]] = []
    if cache_dir.is_dir():
        for path in cache_dir.iterdir():
            if not path.is_file():
                continue
            slot = _parse_session_filename(path.name)
            # path.name == "session.json" → slot is None (the flat fallback).
            # Surface legacy/--allow-shared files alongside slotted ones so
            # operators can see them; consumers decide whether to use them.
            if path.name != "session.json" and slot is None:
                continue
            data = _read_json(path)
            if not data:
                continue
            uuid = data.get("uuid")
            sid = data.get("client_session_id")
            if not uuid and not sid:
                continue
            entries.append({
                "slot": slot,
                "parent_agent_id": uuid,
                "prior_client_session_id": sid,
                "updated_at": data.get("updated_at"),
                "path": str(path),
            })
    # Sort by parsed UTC datetime, not raw ISO string. Mixed-offset
    # timestamps (e.g., +05:30 vs +00:00) sort incorrectly by string
    # comparison even though Python's `fromisoformat` normalizes them.
    # Entries that fail to parse fall back to a sentinel that sorts last
    # under reverse=True, so they don't displace real entries.
    _MIN_UTC = datetime.min.replace(tzinfo=timezone.utc)

    def _sort_ts(entry: dict[str, Any]) -> datetime:
        raw = entry.get("updated_at")
        if not isinstance(raw, str) or not raw:
            return _MIN_UTC
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return _MIN_UTC
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    entries.sort(key=_sort_ts, reverse=True)
    print(json.dumps(entries))
    return 0


# Per-workspace cap on how many distinct file paths we remember in the
# milestone accumulator. The accumulator exists so auto-checkin can report a
# concrete file list; beyond ~20 entries the summary becomes noise and the
# cache starts growing unbounded in long-running sessions.
MILESTONE_FILE_CAP = 20


def cmd_bump_edit(args: argparse.Namespace) -> int:
    """Append an edit event to the milestone accumulator.

    Increments edit_count, dedupes file_path into files_touched (capped),
    stamps first_edit_ts on the first bump since reset, and always refreshes
    last_edit_ts + updated_at. Backwards-compatible keys (event, file_path,
    timestamp) are preserved so existing readers keep working.
    """
    workspace = _workspace_path(args.workspace)
    path = _cache_path("milestone", workspace)
    existing = _read_json(path)

    now_epoch = int(datetime.now(timezone.utc).timestamp())
    now_iso = datetime.now(timezone.utc).isoformat()

    existing["edit_count"] = int(existing.get("edit_count") or 0) + 1
    if not existing.get("first_edit_ts"):
        existing["first_edit_ts"] = now_epoch
    existing["last_edit_ts"] = now_epoch
    existing["updated_at"] = now_iso

    files = existing.get("files_touched")
    if not isinstance(files, list):
        files = []
    fp = (args.file_path or "").strip()
    if fp and fp not in files:
        files.append(fp)
        if len(files) > MILESTONE_FILE_CAP:
            files = files[-MILESTONE_FILE_CAP:]
    existing["files_touched"] = files

    # Legacy shape — keep for readers that predate the accumulator.
    existing.setdefault("event", "edit")
    if fp:
        existing["file_path"] = fp
    existing["timestamp"] = now_epoch

    _write_json(path, existing)
    if args.echo:
        print(json.dumps(existing))
    return 0


def cmd_reset_milestone(args: argparse.Namespace) -> int:
    """Reset the milestone accumulator after a successful check-in."""
    workspace = _workspace_path(args.workspace)
    path = _cache_path("milestone", workspace)
    existing = _read_json(path)
    existing["edit_count"] = 0
    existing["files_touched"] = []
    existing["first_edit_ts"] = None
    existing["last_edit_ts"] = None
    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(path, existing)
    if args.echo:
        print(json.dumps(existing))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_path = sub.add_parser("path", help="Print the absolute cache path")
    p_path.add_argument("kind", choices=sorted(CACHE_FILES))
    p_path.add_argument("--workspace")
    p_path.add_argument("--slot", help="Claude Code session_id for slotted cache")
    p_path.set_defaults(func=cmd_path)

    p_get = sub.add_parser("get", help="Read cached JSON")
    p_get.add_argument("kind", choices=sorted(CACHE_FILES))
    p_get.add_argument("--workspace")
    p_get.add_argument("--slot", help="Claude Code session_id for slotted cache")
    p_get.add_argument("--key")
    p_get.set_defaults(func=cmd_get)

    p_set = sub.add_parser("set", help="Write cached JSON")
    p_set.add_argument("kind", choices=sorted(CACHE_FILES))
    p_set.add_argument("--workspace")
    p_set.add_argument("--slot", help="Claude Code session_id for slotted cache")
    p_set.add_argument(
        "--allow-shared",
        action="store_true",
        help=(
            "Permit slotless session writes for substrate-earned single-tenant "
            "deployments (e.g., Lumen on dedicated Pi). Operator-asserted — no "
            "runtime substrate-claim attestation here; the principled gate "
            "lives with S19 substrate attestation (see "
 " §6)."
        ),
    )
    p_set.add_argument("--json")
    p_set.add_argument("--merge", action="store_true")
    p_set.add_argument("--stamp", action="store_true")
    p_set.add_argument("--echo", action="store_true")
    p_set.set_defaults(func=cmd_set)

    p_clear = sub.add_parser("clear", help="Delete a cache file")
    p_clear.add_argument("kind", choices=sorted(CACHE_FILES))
    p_clear.add_argument("--workspace")
    p_clear.add_argument("--slot", help="Claude Code session_id for slotted cache")
    p_clear.set_defaults(func=cmd_clear)

    p_list = sub.add_parser(
        "list",
        help="List session slot inventory (newest first) as JSON",
    )
    p_list.add_argument("--workspace")
    p_list.set_defaults(func=cmd_list)

    p_bump = sub.add_parser(
        "bump-edit",
        help="Append an edit event to the milestone accumulator",
    )
    p_bump.add_argument("--workspace")
    p_bump.add_argument("--file-path", default="")
    p_bump.add_argument("--echo", action="store_true")
    p_bump.set_defaults(func=cmd_bump_edit)

    p_reset = sub.add_parser(
        "reset-milestone",
        help="Reset the milestone accumulator after a check-in",
    )
    p_reset.add_argument("--workspace")
    p_reset.add_argument("--echo", action="store_true")
    p_reset.set_defaults(func=cmd_reset_milestone)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"session_cache.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
