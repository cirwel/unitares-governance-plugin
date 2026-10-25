#!/usr/bin/env python3
"""Normalize host-specific Stop hooks into one turn-summary contract."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    from _redact import redact_secrets
except ModuleNotFoundError:  # Imported as ``scripts.stop_hook_event`` in tests.
    from scripts._redact import redact_secrets


SUPPORTED_HOSTS = ("claude", "codex", "agy")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]*$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_AGY_ONBOARD_TOOLS = frozenset({"onboard", "start_session", "identity", "bind_session"})
_AGY_GOVERNANCE_SERVERS = frozenset({"unitares-governance", "unitares_governance", "governance", "unitares"})
_SPILLED_FILE_RE = re.compile(r"file://(/[^\s\"'<>]+)")


class StopHookPayloadError(ValueError):
    """The host payload does not satisfy the declared Stop-hook contract."""


@dataclass(frozen=True)
class StopHookEvent:
    host: str
    session_id: str
    response_text: str
    tool_count: int | None
    tool_names: tuple[str, ...]
    model: str = ""
    model_provider: str = ""
    model_source: str = "unavailable"
    harness_type: str = ""
    harness_version: str = ""
    harness_source: str = "harness_reported"
    workspace_dir: str = ""
    is_subagent: bool = False

    @property
    def summary(self) -> str:
        excerpt = self.response_text.strip().replace("\n", " ")[:120]
        if self.tool_count is None:
            return f"Turn summary ({self.host}; tool count unavailable); response excerpt: {excerpt}"
        top = ", ".join(self.tool_names[:3]) if self.tool_names else "no tools"
        return (
            f"Turn summary: {self.tool_count} tool calls ({top}); "
            f"response excerpt: {excerpt}"
        )

    @property
    def floor_summary(self) -> str:
        excerpt = self.response_text.strip().replace("\n", " ")[:100]
        if self.tool_count is None:
            return f"Floor (un-onboarded {self.host}): tool count unavailable; {excerpt}"
        return f"Floor (un-onboarded): {self.tool_count} tool calls; {excerpt}"

    @property
    def complexity(self) -> float:
        if self.tool_count is None:
            return 0.3
        return min(self.tool_count / 10.0, 0.85)

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tool_names"] = list(self.tool_names)
        payload["summary"] = self.summary
        payload["floor_summary"] = self.floor_summary
        # The floor transport predates nullable counts. Its summary retains the
        # distinction; zero is only the wire-compatible fallback value.
        payload["floor_tool_count"] = self.tool_count if self.tool_count is not None else 0
        payload["complexity"] = self.complexity
        return payload


def _decode_payload(raw: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        decoded = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise StopHookPayloadError("hook input is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise StopHookPayloadError("hook input must be a JSON object")
    return decoded


def _safe_identifier_text(value: Any, maximum: int) -> str:
    """Return ``value`` if it is one retainable exact identifier, else empty."""
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if len(text) > maximum:
        return ""
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return ""
    if "://" in text or redact_secrets(text) != text:
        return ""
    if not _SAFE_IDENTIFIER_RE.fullmatch(text):
        return ""
    return text


def _provenance_identifier(
    payload: Mapping[str, Any], *keys: str, maximum: int = 160
) -> str:
    """Return one safe exact identifier, or empty when it cannot be retained.

    Exact identifiers are rejected rather than truncated because truncation can
    silently merge distinct model cohorts. Sanitization happens before the
    value reaches a command-line argument in ``hooks/post-stop``.
    """
    for key in keys:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        return _safe_identifier_text(value, maximum)
    return ""


# Claude Stop payloads carry no model field, but the host records the model on
# every assistant entry of its transcript. The newest entries are at the end, so
# only a bounded tail is read: this runs once per turn.
_TRANSCRIPT_TAIL_BYTES = 256 * 1024
# Antigravity transcripts record one GENERIC step (up to ~4.1 KB in compact
# transcript.jsonl) per tool call, so a 90-tool /pipeline turn reaches ~300 KB.
# Use a 2 MB window for agy so long turns are not truncated mid-turn.
_AGY_TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024


def _model_from_transcript(payload: Mapping[str, Any], maximum: int = 160) -> str:
    """Return the newest usable assistant model in the host transcript tail.

    Empty on any problem (no path, relative path, not a ``.jsonl`` regular file,
    unreadable, no usable entry). Never raises: a turn summary must not fail
    because provenance could not be read. Entries whose model is not a safe
    identifier (for example ``<synthetic>``) are skipped, not retained.
    """
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path.startswith("/") or not path.endswith(".jsonl"):
        return ""
    try:
        # O_NONBLOCK so a FIFO named ``*.jsonl`` cannot hang the hook; fstat on
        # the opened descriptor (not a prior stat) so the check cannot race.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return ""
            size = os.lseek(fd, 0, os.SEEK_END)
            start = max(0, size - _TRANSCRIPT_TAIL_BYTES)
            os.lseek(fd, start, os.SEEK_SET)
            data = os.read(fd, _TRANSCRIPT_TAIL_BYTES)
        finally:
            os.close(fd)
    except (OSError, ValueError):  # ValueError: embedded NUL or lone surrogate
        return ""
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # first line is probably cut mid-record
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        model = _safe_identifier_text(message.get("model"), maximum)
        if model:
            return model
    return ""


def normalize_claude(payload: Mapping[str, Any]) -> StopHookEvent:
    """Normalize current Claude Stop input, with a legacy payload fallback."""
    if "last_assistant_message" in payload:
        message = payload.get("last_assistant_message") or ""
    else:
        message = payload.get("final_text") or ""
    if not isinstance(message, str):
        raise StopHookPayloadError(
            "Claude Stop payload last_assistant_message must be a string or null"
        )

    # Current Claude Stop events do not include tool calls. Older plugin
    # harnesses did, so preserve their observed count without claiming zero
    # when the field is absent.
    tool_calls = payload.get("tool_calls")
    if tool_calls is None:
        count = None
        names: tuple[str, ...] = ()
    elif isinstance(tool_calls, list):
        count = len(tool_calls)
        names = tuple(
            str(call["name"])
            for call in tool_calls
            if isinstance(call, dict) and call.get("name")
        )
    else:
        raise StopHookPayloadError("legacy Claude Stop tool_calls must be a list")
    model = _provenance_identifier(payload, "model") or _model_from_transcript(payload)
    return StopHookEvent(
        host="claude",
        session_id=str(payload.get("session_id") or "").strip(),
        response_text=message[:512],
        tool_count=count,
        tool_names=names,
        model=model,
        model_provider=_provenance_identifier(
            payload, "model_provider", "provider", maximum=80
        ),
        model_source="harness_reported" if model else "unavailable",
        harness_type="claude-code",
        harness_version=_provenance_identifier(
            payload, "harness_version", "client_version", "version", maximum=80
        ),
    )


def normalize_codex(payload: Mapping[str, Any]) -> StopHookEvent:
    message = payload.get("last_assistant_message") or ""
    if not isinstance(message, str):
        raise StopHookPayloadError("Codex Stop payload last_assistant_message must be a string or null")
    model = _provenance_identifier(payload, "model")
    return StopHookEvent(
        host="codex",
        session_id=str(payload.get("session_id") or "").strip(),
        response_text=message[:512],
        tool_count=None,
        tool_names=(),
        model=model,
        model_provider=_provenance_identifier(
            payload, "model_provider", "provider", maximum=80
        ),
        model_source="harness_reported" if model else "unavailable",
        harness_type="codex-cli",
        harness_version=_provenance_identifier(
            payload, "harness_version", "client_version", "version", maximum=80
        ),
    )


def _brain_session_mismatch(parts: tuple[str, ...], session_id: str) -> bool:
    """Return True if ``parts`` contains ``brain/<id>`` that does not match ``session_id``."""
    if not session_id or "brain" not in parts:
        return False
    idx = parts.index("brain")
    return idx + 1 < len(parts) and parts[idx + 1] != session_id


def _agy_transcript_candidates(payload: Mapping[str, Any], session_id: str) -> list[str]:
    """Return validated candidate transcript paths for an Antigravity payload."""
    candidates: list[str] = []
    for key in ("transcriptPath", "transcript_path"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            candidates.append(raw.strip())

    artifact_dir = payload.get("artifactDirectoryPath")
    if isinstance(artifact_dir, str) and artifact_dir.strip():
        art_clean = artifact_dir.strip().rstrip("/")
        if art_clean.startswith("/") and ".." not in Path(art_clean).parts:
            candidates.append(
                f"{art_clean}/.system_generated/logs/transcript.jsonl"
            )

    valid: list[str] = []
    for cand in candidates:
        if not cand.startswith("/") or not cand.endswith(".jsonl"):
            continue
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in cand):
            continue
        parts = Path(cand).parts
        if ".." in parts:
            continue
        if _brain_session_mismatch(parts, session_id):
            continue
        real_parts = Path(os.path.realpath(cand)).parts
        if _brain_session_mismatch(real_parts, session_id):
            continue
        if cand not in valid:
            valid.append(cand)
    return valid


def _open_regular_transcript_fd(path: str) -> int | None:
    """Open ``path`` non-blocking without following a terminal symlink, verifying regular file and owner."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            return None
        getuid = getattr(os, "getuid", None)
        if callable(getuid) and st.st_uid != getuid():
            os.close(fd)
            return None
        return fd
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None


_SUBAGENT_STEP0_RE = re.compile(
    rb'^\s*\{(?=[^\n]{0,512}"step_index"\s*:\s*0\b)(?=[^\n]{0,512}"type"\s*:\s*"SYSTEM_MESSAGE")'
)


def _is_subagent_first_line(first_line: bytes) -> bool:
    """Return True if step 0 of an agy transcript is a SYSTEM_MESSAGE (invoke_subagent)."""
    if not first_line.strip():
        return False
    try:
        entry = json.loads(first_line)
        return (
            isinstance(entry, dict)
            and entry.get("step_index") == 0
            and entry.get("type") == "SYSTEM_MESSAGE"
        )
    except ValueError:
        return bool(_SUBAGENT_STEP0_RE.search(first_line[:512]))


def _read_agy_transcript_tail(
    payload: Mapping[str, Any], session_id: str
) -> tuple[list[bytes], bool, bool] | None:
    """Return ``(lines, truncated_at_start, is_subagent)`` for the first readable agy transcript."""
    window = _AGY_TRANSCRIPT_TAIL_BYTES
    for path in _agy_transcript_candidates(payload, session_id):
        fd = _open_regular_transcript_fd(path)
        if fd is None:
            continue
        try:
            size = os.lseek(fd, 0, os.SEEK_END)
            os.lseek(fd, 0, os.SEEK_SET)
            head_peek = os.read(fd, 65536)
            first_line = head_peek.split(b"\n", 1)[0]
            start = max(0, size - window)
            os.lseek(fd, start, os.SEEK_SET)
            data = os.read(fd, window)
        except OSError:
            continue
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        lines = data.split(b"\n")
        if start > 0:
            lines = lines[1:]
        return lines, start > 0, _is_subagent_first_line(first_line)
    return None


def _agy_turn_from_transcript(
    payload: Mapping[str, Any], session_id: str
) -> tuple[str, int | None, tuple[str, ...], bool]:
    """Extract ``(response_text, tool_count, tool_names, is_subagent)`` for the current agy turn."""
    tail = _read_agy_transcript_tail(payload, session_id)
    if tail is None:
        return "", None, (), False
    lines, truncated_at_start, is_subagent = tail
    response_text = ""
    step_tool_groups: list[list[str]] = []
    found_turn_start = False
    saw_planner = False

    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        step_type = entry.get("type")
        if step_type in ("USER_INPUT", "SYSTEM_MESSAGE"):
            found_turn_start = True
            break
        if step_type != "PLANNER_RESPONSE":
            continue
        saw_planner = True
        if not response_text:
            content = entry.get("content")
            if isinstance(content, str) and content.strip():
                response_text = content
        raw_calls = entry.get("tool_calls")
        if isinstance(raw_calls, list):
            step_tools = [
                str(call["name"]).strip()
                for call in raw_calls
                if isinstance(call, dict)
                and isinstance(call.get("name"), str)
                and call["name"].strip()
            ]
            if step_tools:
                step_tool_groups.append(step_tools)

    if not found_turn_start and (truncated_at_start or not saw_planner):
        return response_text, None, (), is_subagent

    ordered_names = tuple(
        name for group in reversed(step_tool_groups) for name in group
    )
    return response_text, len(ordered_names), ordered_names, is_subagent


def _unwrap_agy_arg_string(value: Any) -> Any:
    """Unwrap JSON-encoded string values recorded inside agy transcript tool_calls.args."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if len(text) >= 2 and text[0] in ('"', "{") and text[-1] in ('"', "}"):
        try:
            return json.loads(text)
        except ValueError:
            return value
    return value


def _agy_driver_onboard_call_offsets(entry: Mapping[str, Any]) -> set[int]:
    """Return 0-based tool_calls offsets in a PLANNER_RESPONSE that invoke driver onboarding."""
    raw_calls = entry.get("tool_calls")
    if not isinstance(raw_calls, list):
        return set()
    offsets: set[int] = set()
    for idx, call in enumerate(raw_calls):
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "").strip()
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        tool_args: dict[str, Any] = {}
        matched_tool = ""
        if name == "call_mcp_tool":
            server = str(_unwrap_agy_arg_string(args.get("ServerName")) or "").strip().casefold()
            tool = str(_unwrap_agy_arg_string(args.get("ToolName")) or "").strip().casefold()
            if server in _AGY_GOVERNANCE_SERVERS:
                unwrapped_args = _unwrap_agy_arg_string(args.get("Arguments"))
                if tool in _AGY_ONBOARD_TOOLS:
                    matched_tool = tool
                    if isinstance(unwrapped_args, dict):
                        tool_args = unwrapped_args
                elif tool == "use_tool" and isinstance(unwrapped_args, dict):
                    inner_tool = str(
                        unwrapped_args.get("tool_name") or unwrapped_args.get("name") or ""
                    ).strip().casefold()
                    if inner_tool in _AGY_ONBOARD_TOOLS:
                        matched_tool = inner_tool
                        inner_args = _unwrap_agy_arg_string(
                            unwrapped_args.get("arguments")
                            if "arguments" in unwrapped_args
                            else unwrapped_args.get("args")
                        )
                        if isinstance(inner_args, dict):
                            tool_args = inner_args
        else:
            lowered = name.casefold()
            for prefix in ("mcp__", "mcp_"):
                if lowered.startswith(prefix):
                    rest = lowered[len(prefix):]
                    for server in _AGY_GOVERNANCE_SERVERS:
                        for sep in ("__", "_"):
                            tag = f"{server}{sep}"
                            if rest.startswith(tag) and rest[len(tag):] in _AGY_ONBOARD_TOOLS:
                                matched_tool = rest[len(tag):]
                                tool_args = {
                                    k: _unwrap_agy_arg_string(v) for k, v in args.items()
                                }
        if not matched_tool:
            continue
        if matched_tool == "identity" and not (
            tool_args.get("resume") is True
            or tool_args.get("continuity_token")
            or tool_args.get("client_session_id")
        ):
            continue
        spawn_reason = str(tool_args.get("spawn_reason") or "").strip().casefold()
        if spawn_reason == "subagent":
            continue
        offsets.add(idx)
    return offsets


def _parse_onboard_json_lines(text: str) -> dict[str, Any] | None:
    """Parse a successful start_session/onboard/identity JSON object from lines of text."""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{") or not stripped.endswith("}"):
            continue
        try:
            resp = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(resp, dict):
            continue
        canonical = resp.get("raw_governance")
        if not isinstance(canonical, dict):
            canonical = {}
        success = resp.get("success")
        if success is None:
            success = canonical.get("success", True)
        if success is not True:
            continue
        uuid_val = str(
            resp.get("agent_uuid")
            or resp.get("uuid")
            or canonical.get("agent_uuid")
            or canonical.get("uuid")
            or ""
        ).strip()
        csid_val = str(
            resp.get("client_session_id")
            or canonical.get("client_session_id")
            or ""
        ).strip()
        if not _UUID_RE.fullmatch(uuid_val) or not csid_val:
            continue
        summary = resp.get("state_summary") if isinstance(resp.get("state_summary"), dict) else {}
        canon_summary = (
            canonical.get("state_summary")
            if isinstance(canonical.get("state_summary"), dict)
            else {}
        )
        return {
            "uuid": uuid_val,
            "agent_id": str(resp.get("agent_id") or canonical.get("agent_id") or "").strip(),
            "client_session_id": csid_val,
            "display_name": str(
                resp.get("display_name") or canonical.get("display_name") or ""
            ).strip(),
            "session_resolution_source": str(
                resp.get("session_resolution_source")
                or canonical.get("session_resolution_source")
                or ""
            ).strip(),
            "onboard_origin": str(
                summary.get("onboard_origin")
                or canon_summary.get("onboard_origin")
                or resp.get("onboard_origin")
                or "agent"
            ).strip(),
            "onboard_origin_basis": str(
                summary.get("onboard_origin_basis")
                or canon_summary.get("onboard_origin_basis")
                or resp.get("onboard_origin_basis")
                or ""
            ).strip(),
        }
    return None


def _extract_onboard_response_from_generic(
    entry: Mapping[str, Any], transcript_path: str = ""
) -> dict[str, Any] | None:
    """Parse a successful start_session/onboard JSON response from an agy GENERIC step."""
    content = entry.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    inline = _parse_onboard_json_lines(content)
    if inline is not None:
        return inline
    if transcript_path and "file://" in content:
        match = _SPILLED_FILE_RE.search(content)
        if match:
            spilled_path = match.group(1)
            try:
                steps_root = Path(os.path.realpath(transcript_path)).parent.parent / "steps"
                spilled_real = Path(os.path.realpath(spilled_path))
                if steps_root in spilled_real.parents and ".." not in Path(spilled_path).parts:
                    fd = _open_regular_transcript_fd(spilled_path)
                    if fd is not None:
                        try:
                            spilled_text = os.read(fd, _TRANSCRIPT_TAIL_BYTES).decode(
                                "utf-8", errors="replace"
                            )
                        finally:
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                        return _parse_onboard_json_lines(spilled_text)
            except OSError:
                return None
    return None


def recover_agy_identity_from_transcript(
    raw: str | Mapping[str, Any],
    *,
    workspace: str | Path,
    slot: str,
) -> bool:
    """Recover an agent-minted start_session/onboard binding from the current agy turn."""
    slot_clean = (slot or "").strip()
    if not slot_clean:
        return False
    payload = _decode_payload(raw)
    session_id = str(payload.get("conversationId") or payload.get("session_id") or "").strip()

    window = _AGY_TRANSCRIPT_TAIL_BYTES
    turn_entries_reversed: list[dict[str, Any]] = []
    transcript_path_used = ""
    for path in _agy_transcript_candidates(payload, session_id):
        fd = _open_regular_transcript_fd(path)
        if fd is None:
            continue
        try:
            size = os.lseek(fd, 0, os.SEEK_END)
            start = max(0, size - window)
            os.lseek(fd, start, os.SEEK_SET)
            data = os.read(fd, window)
        except OSError:
            continue
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        lines = data.split(b"\n")
        if start > 0:
            lines = lines[1:]
        for raw_line in reversed(lines):
            if not raw_line.strip():
                continue
            try:
                item = json.loads(raw_line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("USER_INPUT", "SYSTEM_MESSAGE"):
                break
            turn_entries_reversed.append(item)
        transcript_path_used = path
        break

    if not turn_entries_reversed:
        return False

    recovered: dict[str, Any] | None = None
    pending_offsets: set[int] = set()
    generic_idx = 0
    for entry in reversed(turn_entries_reversed):
        step_type = entry.get("type")
        if step_type == "PLANNER_RESPONSE":
            pending_offsets = _agy_driver_onboard_call_offsets(entry)
            generic_idx = 0
        elif step_type == "GENERIC":
            if generic_idx in pending_offsets:
                candidate = _extract_onboard_response_from_generic(
                    entry, transcript_path_used
                )
                if candidate is not None:
                    recovered = candidate
                pending_offsets.discard(generic_idx)
            generic_idx += 1

    if recovered is None:
        return False

    try:
        from onboard_helper import _update_cache_transaction
    except ModuleNotFoundError:
        from scripts.onboard_helper import _update_cache_transaction

    new_cache = {
        **recovered,
        "continuity_token": "",
        "prior_process": False,
        "schema_version": 2,
        "slot": slot_clean,
    }

    def _commit_if_eligible(current: dict[str, Any]) -> dict[str, Any] | None:
        cur_uuid = str(current.get("uuid") or "").strip()
        cur_csid = str(current.get("client_session_id") or "").strip()
        if (
            not cur_uuid
            or not cur_csid
            or current.get("prior_process")
            or (recovered["uuid"], recovered["client_session_id"]) != (cur_uuid, cur_csid)
        ):
            return {**current, **new_cache}
        return None

    _auth, committed = _update_cache_transaction(
        Path(workspace).expanduser().resolve(),
        _commit_if_eligible,
        slot_clean,
    )
    return bool(
        committed
        or (
            _auth.get("uuid")
            and _auth.get("client_session_id")
            and not _auth.get("prior_process")
        )
    )


def normalize_agy(payload: Mapping[str, Any]) -> StopHookEvent:
    """Normalize Antigravity (agy) Stop input and its transcript tail."""
    session_id = str(
        payload.get("conversationId") or payload.get("session_id") or ""
    ).strip()
    transcript_text, tool_count, tool_names, is_subagent = _agy_turn_from_transcript(
        payload, session_id
    )
    if "last_assistant_message" in payload and payload.get("last_assistant_message") is not None:
        raw_message = payload.get("last_assistant_message")
        if not isinstance(raw_message, str):
            raise StopHookPayloadError(
                "Antigravity Stop payload last_assistant_message must be a string or null"
            )
        message = raw_message
    else:
        message = transcript_text

    raw_model = _provenance_identifier(payload, "model", "modelName")
    model = "" if raw_model.casefold() == "auto" else raw_model

    workspace_dir = ""
    workspaces = payload.get("workspacePaths")
    if isinstance(workspaces, list):
        for item in workspaces:
            if isinstance(item, str) and item.strip().startswith("/"):
                workspace_dir = item.strip()
                break

    return StopHookEvent(
        host="agy",
        session_id=session_id,
        response_text=redact_secrets(message)[:512],
        tool_count=tool_count,
        tool_names=tool_names,
        model=model,
        model_provider=_provenance_identifier(
            payload, "model_provider", "provider", maximum=80
        ),
        model_source="harness_reported" if model else "unavailable",
        harness_type="antigravity",
        harness_version=_provenance_identifier(
            payload, "harness_version", "client_version", "version", maximum=80
        ),
        workspace_dir=workspace_dir,
        is_subagent=is_subagent,
    )


def normalize_stop_hook(
    raw: str | Mapping[str, Any],
    *,
    host: str,
) -> StopHookEvent:
    payload = _decode_payload(raw)
    if host == "claude":
        return normalize_claude(payload)
    if host == "codex":
        return normalize_codex(payload)
    if host == "agy":
        return normalize_agy(payload)
    raise StopHookPayloadError(f"unsupported hook host: {host or '<missing>'}")


def main(argv: list[str] | None = None, stdin_text: str | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=SUPPORTED_HOSTS, required=True)
    parser.add_argument("--recover-agy-identity", action="store_true")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--slot", default="")
    args = parser.parse_args(argv)
    raw = sys.stdin.read() if stdin_text is None else stdin_text
    if args.recover_agy_identity:
        if args.host != "agy":
            return 2
        try:
            ok = recover_agy_identity_from_transcript(
                raw,
                workspace=args.workspace or os.getcwd(),
                slot=args.slot,
            )
        except Exception:
            return 1
        return 0 if ok else 1
    try:
        event = normalize_stop_hook(raw, host=args.host)
    except StopHookPayloadError as exc:
        print(f"stop_hook_event.py: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(event.to_json_dict(), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
