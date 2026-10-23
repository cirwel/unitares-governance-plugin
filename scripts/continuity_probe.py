#!/usr/bin/env python3
"""Ask an agent, after a context compaction, whether it is the same process.

Opt-in with ``UNITARES_CONTINUITY_PROBE=on``. Two halves:

- ``ask`` runs inside ``compaction_report.py`` once the compaction itself was
  recorded. It writes a marker beside the slot's session cache and returns the
  question, which ``hooks/session-start`` places ahead of the compaction
  guidance.
- ``collect`` (this script's command line) runs from ``hooks/post-stop`` at the
  end of that turn. It reads the agent's last message for one
  ``continuity-probe:`` line and posts the answer, or ``no_answer``, as a
  ``context_compaction_probe`` observation on the identity the cache holds
  now. The marker is removed first, so each compaction is answered once.

The answer is the agent's report, keyed to the compaction it answers; the
server pairs it with what happened (docs/ontology/identity.md, *Compaction*,
in the server repo). The question is asked with the compaction guidance in the
same context, recorded as ``guidance_present``. Only the answer's enum value is
sent, never message text. Every failure is silent, and ``UNITARES_CHECKINS=off``
disables it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _session_cache_io import read_json_dict_unlocked  # noqa: E402
from _session_lookup import _extract_slot, resolve_session_file  # noqa: E402
from checkin import _plugin_version  # noqa: E402
from compaction_report import DEFAULT_TIMEOUT_S, HOSTS, WALL_CLOCK_LIMIT_S, post  # noqa: E402

PROBE_VERSION = "v1"
MARKER_SUFFIX = ".continuity-probe"
# A marker no Stop collected (the session ended mid-turn) is dropped, not
# answered by whatever session reuses the slot later.
MAX_MARKER_AGE_S = 6 * 3600
QUESTION = """Continuity question (an opt-in UNITARES measurement; there is no right answer, and "cannot tell" is a real answer).
Your context was just replaced by a summary. Is the process continuing now the same running process that did the earlier work, a new process, or can you not tell? The note below states the operator's position on compactions; answer from your own reading, which may differ.
End your reply this turn with exactly one of these lines:
continuity-probe: same
continuity-probe: new
continuity-probe: cannot-tell"""
_ANSWER_RE = re.compile(
    r"^[\s`*_>-]*continuity-probe\s*:\s*(same|new|cannot-tell)\b", re.IGNORECASE | re.MULTILINE
)
_ANSWERS = {"same": "same_process", "new": "new_process", "cannot-tell": "cannot_tell"}
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def enabled() -> bool:
    if os.environ.get("UNITARES_CHECKINS", "on").strip().lower() == "off":
        return False
    return os.environ.get("UNITARES_CONTINUITY_PROBE", "").strip().lower() in {
        "1", "on", "true", "yes",
    }


def marker_path(session_path: Path) -> Path:
    return session_path.with_name(session_path.name + MARKER_SUFFIX)


def _session_path(event_text: str, cwd: str) -> Path | None:
    slot = _extract_slot(event_text)
    return resolve_session_file(cwd, slot) if slot else None


def ask(event_text: str, *, cwd: str, host: str, compaction: dict[str, Any]) -> str | None:
    """Write the marker for a recorded compaction and return the question."""
    if not enabled():
        return None
    path = _session_path(event_text, cwd)
    if path is None:
        return None
    marker = {
        "compaction_agent_uuid": compaction["agent_uuid"],
        "compaction_observed_at": compaction["observed_at"],
        "host": host,
        "probe_version": PROBE_VERSION,
        "guidance_present": True,
        "asked_at": time.time(),
    }
    target = marker_path(path)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(marker, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return QUESTION


def parse_answer(message: str) -> str:
    """Return the last answer line's value, or ``no_answer``."""
    matches = _ANSWER_RE.findall(message or "")
    return _ANSWERS[matches[-1].lower()] if matches else "no_answer"


def _last_message(stop_payload: dict[str, Any]) -> str:
    message = stop_payload.get("last_assistant_message") or stop_payload.get("final_text") or ""
    return message if isinstance(message, str) else ""


def build_payload(
    stop_text: str, *, host: str, cwd: str, now: float | None = None
) -> dict[str, Any] | None:
    """Consume this slot's marker and return the answer's observation, else None."""
    if host not in HOSTS:
        return None
    try:
        stop_payload = json.loads(stop_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(stop_payload, dict):
        return None
    path = _session_path(stop_text, cwd)
    if path is None:
        return None
    target = marker_path(path)
    marker = read_json_dict_unlocked(target)
    if not marker:
        return None
    try:
        target.unlink()
    except OSError:
        return None  # Another Stop took it; answer once.
    asked_at = marker.get("asked_at")
    current = time.time() if now is None else now
    if not isinstance(asked_at, (int, float)) or current - asked_at > MAX_MARKER_AGE_S:
        return None
    compacted = marker.get("compaction_agent_uuid")
    compacted_at = marker.get("compaction_observed_at")
    if not (isinstance(compacted, str) and _UUID_RE.fullmatch(compacted)):
        return None
    if not (isinstance(compacted_at, str) and compacted_at):
        return None
    cache = read_json_dict_unlocked(path)
    if cache.get("prior_process") is True:
        return None
    agent_uuid = cache.get("uuid") or cache.get("agent_uuid")
    binding = cache.get("client_session_id")
    if not (isinstance(agent_uuid, str) and _UUID_RE.fullmatch(agent_uuid)):
        return None
    if not (isinstance(binding, str) and binding.strip()):
        return None
    slot = _extract_slot(stop_text) or ""
    return {
        "agent_uuid": agent_uuid,
        "client_session_id": binding.strip(),
        "observation_kind": "context_compaction_probe",
        "host_family": host,
        "execution_mode": "unknown",
        "execution_mode_source": "unspecified",
        "slot_hash": hashlib.sha256(slot.encode("utf-8")).hexdigest()[:32],
        "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "plugin_version": _plugin_version(),
        "probe_answer": parse_answer(_last_message(stop_payload)),
        "probe_version": str(marker.get("probe_version") or PROBE_VERSION),
        "compaction_agent_uuid": compacted,
        "compaction_observed_at": compacted_at,
        "guidance_present": marker.get("guidance_present") is True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    args = parser.parse_args(argv)
    try:
        stop_text = sys.stdin.read()
        # A marker is consumed even when the probe is now off, so a stale one
        # cannot be answered after it is turned back on.
        payload = build_payload(stop_text, host=args.host, cwd=os.getcwd())
        if payload is not None and enabled():
            post(args.server_url, payload, timeout=args.timeout)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
        signal.alarm(WALL_CLOCK_LIMIT_S)
    raise SystemExit(main())
