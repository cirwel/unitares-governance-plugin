#!/usr/bin/env python3
"""Claude hook helper for BEAM file leases.

This script is intentionally stdlib-only so the installed plugin can run in
any workspace without importing the UNITARES server repo. It talks to the
lease-plane HTTP API, acquires `file://` leases before Edit/Write/MultiEdit,
heartbeats held leases after edits, and releases the session's leases on
SessionEnd.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8788"
# Leases are released in PostToolUse right after each edit, so this TTL is now
# only a backstop for the crash-mid-edit window (acquire fired, the session
# died before release-edit ran). A short TTL means even that orphan self-heals
# fast via the reaper. Overridable via UNITARES_FILE_LEASE_TTL_S.
DEFAULT_TTL_S = 300
STATE_VERSION = 1


@dataclass
class HookPayload:
    raw: dict[str, Any]
    session_id: str
    tool_name: str
    file_path: str


def _load_hook_payload(stdin_text: str) -> HookPayload:
    try:
        raw = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        raw = {}
    tool_input = raw.get("tool_input", raw.get("input", {}))
    if not isinstance(tool_input, dict):
        tool_input = {}
    file_path = tool_input.get("file_path") or tool_input.get("path") or ""
    return HookPayload(
        raw=raw if isinstance(raw, dict) else {},
        session_id=str(raw.get("session_id") or "").strip() if isinstance(raw, dict) else "",
        tool_name=str(raw.get("tool_name") or "").strip() if isinstance(raw, dict) else "",
        file_path=str(file_path).strip(),
    )


def _safe_slot(slot: str) -> str:
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slot)
    return safe[:64]


def _state_path(workspace: Path, slot: str) -> Path:
    return workspace / ".unitares" / f"file-leases-{_safe_slot(slot)}.json"


def _session_cache_path(workspace: Path, slot: str) -> Path:
    return workspace / ".unitares" / f"session-{_safe_slot(slot)}.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _debug(message: str) -> None:
    if os.environ.get("UNITARES_HOOK_DEBUG") != "1":
        return
    log_path = os.path.expanduser(os.environ.get("UNITARES_HOOK_DEBUG_LOG", "~/.unitares/hook-skips.log"))
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} | hook=file-lease | {message}\n")
    except OSError:
        pass


def _load_env_file() -> None:
    """Load a simple KEY=VALUE env file without overriding existing env."""
    env_path = os.path.expanduser(
        os.environ.get("UNITARES_SECRETS_ENV", "~/.config/cirwel/secrets.env")
    )
    path = Path(env_path)
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _enabled() -> bool:
    return os.environ.get("UNITARES_FILE_LEASES_ENABLED", "1") not in {"0", "false", "False", "off"}


def _required() -> bool:
    return os.environ.get("UNITARES_FILE_LEASES_REQUIRED", "0") in {"1", "true", "True", "on"}


def _bearer_token() -> str:
    _load_env_file()
    return (
        os.environ.get("LEASE_PLANE_BEARER_TOKEN")
        or os.environ.get("UNITARES_LEASE_PLANE_BEARER_TOKEN")
        or os.environ.get("GOVERNANCE_TOKEN")
        or ""
    ).strip()


def _base_url() -> str:
    return (
        os.environ.get("LEASE_PLANE_BASE_URL")
        or os.environ.get("UNITARES_LEASE_PLANE_URL")
        or DEFAULT_BASE_URL
    ).rstrip("/")


def _timeout_s() -> float:
    raw = os.environ.get("UNITARES_FILE_LEASE_TIMEOUT_S", "1.0")
    try:
        return max(0.05, float(raw))
    except ValueError:
        return 1.0


def _ttl_s() -> int:
    raw = os.environ.get("UNITARES_FILE_LEASE_TTL_S", str(DEFAULT_TTL_S))
    try:
        return min(3600, max(1, int(raw)))
    except ValueError:
        return DEFAULT_TTL_S


def _http_json(
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        _base_url() + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=_timeout_s()) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
    except Exception as exc:
        return {"ok": False, "error": "service_unavailable", "reason": type(exc).__name__}

    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return {"ok": False, "error": "schema_invalid", "detail": "response was not JSON"}
    return payload if isinstance(payload, dict) else {"ok": False, "error": "schema_invalid"}


def _surface_id(path: str, workspace: Path) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = workspace / p
    return f"file://{p}"


def _load_state(workspace: Path, slot: str) -> dict[str, Any]:
    path = _state_path(workspace, slot)
    data = _read_json(path)
    if data.get("version") != STATE_VERSION:
        return {
            "version": STATE_VERSION,
            "slot": slot,
            "workspace": str(workspace),
            "holder_uuid": "",
            "leases": {},
        }
    leases = data.get("leases")
    if not isinstance(leases, dict):
        data["leases"] = {}
    return data


def _holder_uuid(workspace: Path, slot: str, state: dict[str, Any]) -> str:
    existing = str(state.get("holder_uuid") or "").strip()
    if existing:
        return existing
    cache = _read_json(_session_cache_path(workspace, slot))
    cached = str(cache.get("uuid") or cache.get("agent_uuid") or "").strip()
    if cached:
        return cached
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"unitares:file-lease:{workspace}:{slot}"))


def _save_state(workspace: Path, slot: str, state: dict[str, Any]) -> None:
    _write_json(_state_path(workspace, slot), state)


def _block(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def _fail_open_or_block(message: str) -> int:
    _debug(message)
    if _required():
        return _block(f"BLOCKED: file lease required but unavailable: {message}")
    return 0


def _heartbeat(token: str, lease_id: str) -> dict[str, Any]:
    return _http_json("POST", "/v1/lease/heartbeat", token=token, body={"lease_id": lease_id})


def _release(token: str, lease_id: str) -> dict[str, Any]:
    return _http_json(
        "POST",
        "/v1/lease/release",
        token=token,
        body={"lease_id": lease_id, "release_reason": "normal"},
    )


def _self_heal_eta(expires_at: Any) -> str:
    """Human phrase for when a `file://` lease auto-clears.

    File leases take the `remote_heartbeat` path: a pure TTL row with no
    auto-renewing holder, swept by the reaper at `expires_at`. So a lease held
    by a *dead* session is not permanent — it clears on its own. This phrase
    tells the operator that, so they don't reflexively force-release a lease
    that would have cleared by itself.
    """
    if not isinstance(expires_at, str) or not expires_at:
        return ""
    try:
        from datetime import datetime, timezone

        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        remaining = (exp - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return ""
    if remaining <= 30:
        return " (expiring now — should clear within a reaper cycle)"
    minutes = int(remaining // 60) or 1
    return f" (auto-clears in ~{minutes} min if the holder is gone — no action needed)"


def cmd_pre_edit(args: argparse.Namespace, stdin_text: str) -> int:
    if not _enabled():
        return 0

    workspace = Path(args.workspace).resolve()
    payload = _load_hook_payload(stdin_text)
    if not payload.session_id:
        return _fail_open_or_block("missing session_id")
    if not payload.file_path:
        return _fail_open_or_block("missing file_path")

    token = _bearer_token()
    if not token:
        return _fail_open_or_block("missing LEASE_PLANE_BEARER_TOKEN")

    state = _load_state(workspace, payload.session_id)
    holder_uuid = _holder_uuid(workspace, payload.session_id, state)
    surface_id = _surface_id(payload.file_path, workspace)
    leases = state.setdefault("leases", {})

    existing = leases.get(surface_id)
    if isinstance(existing, dict) and existing.get("lease_id"):
        heartbeat = _heartbeat(token, str(existing["lease_id"]))
        if heartbeat.get("ok") is True:
            existing["last_heartbeat_at"] = time.time()
            _save_state(workspace, payload.session_id, state)
            return 0
        if heartbeat.get("error") not in {"not_found", "expired"}:
            return _fail_open_or_block(f"heartbeat failed for {surface_id}: {heartbeat.get('error')}")
        leases.pop(surface_id, None)

    body = {
        "surface_id": surface_id,
        "holder_agent_uuid": holder_uuid,
        "holder_class": "process_instance",
        "holder_kind": "remote_heartbeat",
        "ttl_s": _ttl_s(),
        "holder_pid": str(os.getpid()),
        "intent": f"plugin {payload.tool_name or 'edit'}",
        "audit_session": payload.session_id,
    }
    result = _http_json("POST", "/v1/lease/acquire", token=token, body=body)
    if result.get("ok") is True:
        lease = result.get("lease") if isinstance(result.get("lease"), dict) else {}
        lease_id = str(lease.get("lease_id") or "")
        if not lease_id:
            return _fail_open_or_block("acquire response missing lease_id")
        state["holder_uuid"] = holder_uuid
        leases[str(lease.get("surface_id") or surface_id)] = {
            "lease_id": lease_id,
            "path": payload.file_path,
            "surface_id": str(lease.get("surface_id") or surface_id),
            "expires_at": lease.get("expires_at"),
            "acquired_at": time.time(),
            "idempotent": bool(result.get("idempotent")),
        }
        _save_state(workspace, payload.session_id, state)
        return 0

    if result.get("error") == "held_by_other":
        expires_at = result.get("expires_at", "?")
        return _block(
            "BLOCKED: file lease held by another agent\n"
            f"  Path: {payload.file_path}\n"
            f"  Surface: {result.get('surface_id') or surface_id}\n"
            f"  Blocking lease: {result.get('blocking_lease_id', '?')}\n"
            f"  Held by: {result.get('held_by_uuid', '?')}\n"
            f"  Expires: {expires_at}{_self_heal_eta(expires_at)}\n"
            "This lease self-heals: file leases auto-expire at the time above even if the "
            "holding session died without releasing.\n"
            "Best action: wait for it to clear or edit a different file. Force-release "
            "(operator) is only for when you genuinely can't wait."
        )

    return _fail_open_or_block(f"acquire failed for {surface_id}: {result.get('error') or result}")


def cmd_heartbeat_session(args: argparse.Namespace, stdin_text: str) -> int:
    if not _enabled():
        return 0
    workspace = Path(args.workspace).resolve()
    payload = _load_hook_payload(stdin_text)
    if not payload.session_id:
        return 0
    token = _bearer_token()
    if not token:
        _debug("heartbeat skipped: missing token")
        return 0
    state = _load_state(workspace, payload.session_id)
    leases = state.get("leases")
    if not isinstance(leases, dict) or not leases:
        return 0
    changed = False
    for surface, row in list(leases.items()):
        if not isinstance(row, dict) or not row.get("lease_id"):
            leases.pop(surface, None)
            changed = True
            continue
        result = _heartbeat(token, str(row["lease_id"]))
        if result.get("ok") is True:
            row["last_heartbeat_at"] = time.time()
            changed = True
        elif result.get("error") in {"not_found", "expired"}:
            leases.pop(surface, None)
            changed = True
        else:
            _debug(f"heartbeat failed for {surface}: {result.get('error')}")
    if changed:
        if leases:
            _save_state(workspace, payload.session_id, state)
        else:
            _state_path(workspace, payload.session_id).unlink(missing_ok=True)
    return 0


def cmd_release_session(args: argparse.Namespace, stdin_text: str) -> int:
    if not _enabled():
        return 0
    workspace = Path(args.workspace).resolve()
    payload = _load_hook_payload(stdin_text)
    if not payload.session_id:
        return 0
    token = _bearer_token()
    state_path = _state_path(workspace, payload.session_id)
    state = _load_state(workspace, payload.session_id)
    leases = state.get("leases")
    if not isinstance(leases, dict) or not leases:
        state_path.unlink(missing_ok=True)
        return 0
    if token:
        for surface, row in list(leases.items()):
            if isinstance(row, dict) and row.get("lease_id"):
                result = _release(token, str(row["lease_id"]))
                if result.get("ok") is not True:
                    _debug(f"release failed for {surface}: {result.get('error')}")
    else:
        _debug("release skipped: missing token")
    state_path.unlink(missing_ok=True)
    return 0


def cmd_release_edit(args: argparse.Namespace, stdin_text: str) -> int:
    """Release the lease for the just-edited file (PostToolUse).

    A `file://` lease only needs to exist for the duration of the write that
    holds it. Releasing it right after the edit — instead of heartbeating it
    until SessionEnd — means a file is leased only while actively being
    mutated, so a session that dies cannot strand a held lease (the immortal-
    lease class). The TTL remains a backstop for the crash-mid-edit window
    (acquire fired in pre-edit, the session died before this released).

    Fire-and-forget: always returns 0; never blocks the PostToolUse chain.
    """
    if not _enabled():
        return 0
    workspace = Path(args.workspace).resolve()
    payload = _load_hook_payload(stdin_text)
    if not payload.session_id or not payload.file_path:
        return 0
    token = _bearer_token()
    if not token:
        _debug("release-edit skipped: missing token")
        return 0
    state = _load_state(workspace, payload.session_id)
    leases = state.get("leases")
    if not isinstance(leases, dict) or not leases:
        return 0
    # Match on the stored raw `path` (what pre-edit recorded) rather than a
    # recomputed surface_id: the state key is the server's canonicalized
    # surface_id, which may differ from the client's `file://<abs>` form.
    surface_id = _surface_id(payload.file_path, workspace)
    key = next(
        (
            k
            for k, r in leases.items()
            if isinstance(r, dict)
            and r.get("lease_id")
            and (r.get("path") == payload.file_path or k == surface_id)
        ),
        None,
    )
    if key is None:
        return 0
    result = _release(token, str(leases[key]["lease_id"]))
    if result.get("ok") is not True and result.get("error") not in {"not_found", "expired"}:
        # Best-effort: leave it in state so a later heartbeat/session-end retries;
        # the TTL backstop releases it server-side regardless.
        _debug(f"release-edit failed for {key}: {result.get('error')}")
        return 0
    leases.pop(key, None)
    if leases:
        _save_state(workspace, payload.session_id, state)
    else:
        _state_path(workspace, payload.session_id).unlink(missing_ok=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["pre-edit", "release-edit", "heartbeat-session", "release-session"],
    )
    parser.add_argument("--workspace", default=os.getcwd())
    return parser


def main(argv: list[str] | None = None, stdin_text: str | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    text = sys.stdin.read() if stdin_text is None else stdin_text
    if args.command == "pre-edit":
        return cmd_pre_edit(args, text)
    if args.command == "release-edit":
        return cmd_release_edit(args, text)
    if args.command == "heartbeat-session":
        return cmd_heartbeat_session(args, text)
    if args.command == "release-session":
        return cmd_release_session(args, text)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
