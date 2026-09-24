#!/usr/bin/env python3
"""Local-only resource observation collector (cirwel/unitares#2410).

Shadow study: does resource-grounded state predict near-term operational
events better than current EISV and plain resource thresholds? This helper is
the collection half. It appends one small JSON line per hook event to a local
file and does nothing else.

Hard boundaries, each covered by a test:

- Default OFF. Nothing is read or written unless ``UNITARES_RESOURCE_SHADOW``
  is truthy.
- Local only. No network import, no governance call, no check-in field. The
  governance server never sees these observations; the offline join in the
  unitares repository reads the file afterwards.
- Minimal content. Context occupancy is quantized to a coarse band. Tool
  failures record the tool name only: never tool input, error text, response
  text, or the working directory. The session cache is read for the agent
  UUID only; its continuity token is never copied.
- Never blocks or fails a turn. Every error path exits 0 silently.

Events (Claude host only in v1):
  stop          one row per turn: context occupancy band of the last main-chain
                assistant message in the transcript
  tool-failure  a PostToolUseFailure: sanitized tool name, interrupt flag
  pre-compact   a PreCompact: trigger (auto/manual) plus occupancy band

Context *occupancy* is recorded, not headroom: the transcript carries token
usage but not the model's context-window size, so headroom cannot be derived
here without guessing.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

SCHEMA = "resource_shadow.v1"
SUPPORTED_HOSTS = ("claude",)
EVENTS = ("stop", "tool-failure", "pre-compact")
TRUTHY = {"1", "true", "on", "yes"}

# Lower bounds of the occupancy bands, in tokens. Coarse on purpose.
CTX_BANDS = (
    0,
    8_000,
    16_000,
    32_000,
    64_000,
    128_000,
    192_000,
    256_000,
    384_000,
    512_000,
    768_000,
    1_000_000,
)

MAX_STDIN_BYTES = 1_000_000
TRANSCRIPT_TAIL_BYTES = 2_000_000
_SAFE_TOOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\[\]-]{0,63}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def enabled(env: Mapping[str, str]) -> bool:
    return env.get("UNITARES_RESOURCE_SHADOW", "").strip().lower() in TRUTHY


def ctx_band(tokens: Optional[int]) -> Optional[int]:
    if tokens is None or tokens < 0:
        return None
    band = CTX_BANDS[0]
    for lower in CTX_BANDS:
        if tokens >= lower:
            band = lower
    return band


def _usage_tokens(usage: Mapping[str, Any]) -> Optional[int]:
    total = 0
    seen = False
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            total += value
            seen = True
    return total if seen else None


def last_main_chain_usage(transcript_path: str) -> tuple[Optional[int], str]:
    """Return (occupied context tokens, model) from the transcript tail.

    Reads at most TRANSCRIPT_TAIL_BYTES from the end. Current hosts write
    subagent turns to separate files under ``<session>/subagents/``, so the
    main transcript normally holds no sidechain rows; the ``isSidechain`` skip
    is defensive, for hosts that still inline them. Returns (None, "") when
    nothing usable is found.
    """
    try:
        path = Path(transcript_path)
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > TRANSCRIPT_TAIL_BYTES:
                fh.seek(size - TRANSCRIPT_TAIL_BYTES)
                fh.readline()  # drop the partial first line
            lines = fh.read().splitlines()
    except (OSError, ValueError):
        return None, ""
    for raw in reversed(lines):
        try:
            entry = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        if entry.get("isSidechain") is True:
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        tokens = _usage_tokens(usage)
        if tokens is None:
            continue
        model = message.get("model")
        model = model if isinstance(model, str) and _SAFE_MODEL_RE.match(model) else ""
        return tokens, model
    return None, ""


def _agent_uuid(payload_text: str) -> Optional[str]:
    """Read only the agent UUID from the slot-scoped session cache."""
    try:
        here = Path(__file__).resolve().parent
        if str(here) not in sys.path:
            sys.path.insert(0, str(here))
        from _session_lookup import load_session_for_hook

        data = load_session_for_hook(os.getcwd(), payload_text)
    except Exception:
        return None
    value = data.get("uuid") if isinstance(data, dict) else None
    if isinstance(value, str) and _UUID_RE.match(value):
        return value.lower()
    return None


def _safe_tool(name: Any) -> str:
    if isinstance(name, str) and _SAFE_TOOL_RE.match(name):
        return name
    return "other"


def build_record(
    event: str,
    host: str,
    payload: Mapping[str, Any],
    *,
    agent_uuid: Optional[str],
    now: Optional[_dt.datetime] = None,
) -> dict[str, Any]:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    session_id = payload.get("session_id")
    if not (isinstance(session_id, str) and _SAFE_SESSION_RE.match(session_id)):
        session_id = None
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "ts": now.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "host": host,
        "event": event,
        "session_id": session_id,
        "agent_uuid": agent_uuid,
    }
    tokens: Optional[int] = None
    model = ""
    transcript = payload.get("transcript_path")
    if event in ("stop", "pre-compact") and isinstance(transcript, str) and transcript:
        tokens, model = last_main_chain_usage(transcript)
    if event in ("stop", "pre-compact"):
        record["ctx_tokens_band"] = ctx_band(tokens)
        record["model"] = model or None
    if event == "tool-failure":
        record["tool_name"] = _safe_tool(payload.get("tool_name"))
        record["is_interrupt"] = payload.get("is_interrupt") is True
    if event == "pre-compact":
        trigger = payload.get("trigger")
        record["compact_trigger"] = trigger if trigger in ("auto", "manual") else "unknown"
    return record


def shadow_dir(env: Mapping[str, str]) -> Path:
    override = env.get("UNITARES_RESOURCE_SHADOW_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(env.get("HOME", str(Path.home()))) / ".unitares" / "resource-shadow"


def append_record(directory: Path, record: Mapping[str, Any]) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    day = str(record["ts"])[:10]
    path = directory / f"{day}.jsonl"
    line = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return path


def main(argv: Optional[list[str]] = None, env: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if env is None else env
    if not enabled(env):
        return 0
    try:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--host", default="claude")
        parser.add_argument("--event", required=True)
        args, _unknown = parser.parse_known_args(argv)
    except SystemExit:
        return 0
    if args.host not in SUPPORTED_HOSTS or args.event not in EVENTS:
        return 0
    try:
        payload_text = sys.stdin.read(MAX_STDIN_BYTES)
        payload = json.loads(payload_text) if payload_text.strip() else {}
        if not isinstance(payload, dict):
            return 0
        record = build_record(
            args.event, args.host, payload, agent_uuid=_agent_uuid(payload_text)
        )
        append_record(shadow_dir(env), record)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
