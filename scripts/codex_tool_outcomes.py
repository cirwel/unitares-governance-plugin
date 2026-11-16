"""Count Codex's structured completed items, never text printed by a tool.

Only the hook-named, session-matching rollout is read. Native item_completed
events include nested commands/MCP calls in code mode; response_item wrappers
are not counted again. Older rollouts without native items are unsupported,
not a zero-outcome reading. No tool arguments or results leave this process.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Mapping

import tool_outcomes as common

TOOL_TYPES = {"CommandExecution", "McpToolCall", "FileChange", "DynamicToolCall"}
PASSIVE_TYPES = {"UserMessage", "AgentMessage", "Reasoning", "Plan"}
HEADER_BOUND_BYTES = 1024 * 1024


def _status(item: Mapping[str, Any]) -> tuple[bool, bool, bool]:
    """Return (error, not_executed, unknown) from host fields only."""
    status = item.get("status")
    if not isinstance(status, str):
        return False, False, True
    if status in {"declined", "rejected"}:
        return True, True, False
    if item.get("type") == "CommandExecution":
        if status not in {"completed", "failed"}:
            return False, False, True
        code = item.get("exit_code")
        if isinstance(code, int) and not isinstance(code, bool):
            if status == "failed" and code == 0:
                return False, False, True
            return code != 0, False, False
        # Failed without an exit code does not establish whether it executed.
        return False, False, True
    if status == "failed":
        return True, False, False
    if status != "completed":
        return False, False, True
    if item.get("type") == "McpToolCall":
        result = item.get("result")
        if isinstance(result, Mapping):
            flag = result.get("isError", False)  # MCP's optional flag defaults false
            if isinstance(flag, bool):
                return flag, False, False
        return False, False, True
    if item.get("type") == "DynamicToolCall":
        success = item.get("success")
        return (not success, False, False) if isinstance(success, bool) else (False, False, True)
    return False, False, False


def _digest(item: Mapping[str, Any]) -> str:
    kind = item.get("type")
    if kind == "CommandExecution":
        inputs = {key: item.get(key) for key in ("command", "cwd")}
    elif kind == "McpToolCall":
        inputs = {key: item.get(key) for key in ("server", "tool", "arguments")}
    else:
        inputs = {key: item.get(key) for key in ("tool", "arguments", "changes")}
    return common._call_digest(kind, inputs)


def collect_window(
    transcript_path: Any,
    state: Mapping[str, Any] | None,
    *,
    slot: str,
    read_bound: int = common.READ_BOUND_BYTES,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    fd = common._open_transcript(transcript_path)
    if fd is None:
        return None
    try:
        info = os.fstat(fd)
        # Validate ownership on the opened descriptor, including on later reads
        # and after rotation. Never choose a rollout by recency or filename.
        header = common._read_range(fd, 0, min(info.st_size, HEADER_BOUND_BYTES))
        line, separator, _ = header.partition(b"\n")
        if not separator:
            return None
        meta = json.loads(line)
        payload = meta.get("payload", {})
        if meta.get("type") != "session_meta" or not isinstance(payload, dict):
            return None
        ids = [payload[key] for key in ("id", "session_id") if key in payload]
        if not ids or any(value != slot for value in ids):
            return None
        previous = dict(state or {})
        same = previous.get("inode") == info.st_ino and previous.get("device") == info.st_dev
        offset = previous.get("offset", 0) if same else 0
        if not isinstance(offset, int) or offset < 0:
            offset, same = 0, False
        truncated = bool(previous) and not same
        if offset > info.st_size:
            truncated = True
            offset, same = 0, False
        start = max(offset, info.st_size - read_bound)
        if start > offset:
            truncated = True
        data = common._read_range(fd, start, info.st_size)
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return None
    finally:
        os.close(fd)
    if start > offset:
        cut = data.find(b"\n")
        if cut < 0:
            data, start = b"", start + len(data)
        else:
            data, start = data[cut + 1:], start + cut + 1
    entries = common._entries(data)
    end = start + data.rfind(b"\n") + 1
    if not same:
        for index in range(len(entries) - 1, -1, -1):
            row = entries[index][1]
            p = row.get("payload", {})
            if row.get("type") == "event_msg" and isinstance(p, dict) and p.get("type") == "task_started":
                entries = entries[index + 1:]
                break
        else:
            # No reliable boundary: don't attribute imported/resumed history.
            return None
    completed = list(previous.get("completed", [])) if same else []
    failed = list(previous.get("failed", [])) if same else []
    wrappers = list(previous.get("wrappers", [])) if same else []
    response_results: set[str] = set()
    seen = set(completed)
    supported = bool(previous.get("native_items_seen")) if same else False
    total = errors = not_executed = repeats = unknown = 0
    for _, entry in entries:
        p = entry.get("payload")
        if entry.get("type") == "response_item" and isinstance(p, dict):
            call_id = p.get("call_id")
            if isinstance(call_id, str) and call_id:
                key = hashlib.sha256(call_id.encode()).hexdigest()
                if p.get("type") in {"custom_tool_call", "function_call"} and p.get("name") in {"functions.exec", "functions.wait"}:
                    wrappers.append(key)
                elif p.get("type") in {"custom_tool_call_output", "function_call_output"}:
                    response_results.add(key)
            continue
        if entry.get("type") != "event_msg" or not isinstance(p, dict):
            continue
        if p.get("type") != "item_completed" or p.get("thread_id") != slot:
            continue  # never attribute a child/foreign thread to the parent
        item = p.get("item")
        if not isinstance(item, dict):
            truncated = True
            continue
        kind = item.get("type")
        if not isinstance(kind, str):
            truncated = True
            continue
        if kind in PASSIVE_TYPES:
            continue
        if kind not in TOOL_TYPES:
            # New/unsupported activity is missing coverage, not success.
            truncated = True
            continue
        supported = True
        call_id = item.get("id")
        if not isinstance(call_id, str) or not call_id:
            truncated = True
            continue
        key = hashlib.sha256(call_id.encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        completed.append(key)
        total += 1
        error, never_ran, missing = _status(item)
        unknown += int(missing)
        errors += int(error)
        not_executed += int(never_ran)
        digest = _digest(item)
        if error and not never_ran:
            if digest in failed:
                repeats += 1
            else:
                failed.append(digest)
        elif not missing and not error and digest in failed:
            failed.remove(digest)
    if not supported:
        return None
    # A mixed/older host can itemize messages but omit native tool results.
    # Only explicitly known code-mode wrappers are exempt from matching.
    if response_results - seen - set(wrappers):
        truncated = True
    return {
        "tool_delta": total,
        "tool_error_delta": errors,
        "tool_not_executed_delta": not_executed,
        "repeat_failure_delta": repeats,
        "tool_unknown_status_delta": unknown,
        # Older servers discard the new unknown count; keep their lower-bound
        # coverage flag honest too.
        "window_truncated": truncated or unknown > 0,
        "window_basis": common.WINDOW_BASIS,
    }, {
        "schema_version": common.SCHEMA_VERSION,
        "inode": info.st_ino,
        "device": info.st_dev,
        "offset": end,
        "window_start": start,
        "native_items_seen": supported,
        "completed": completed[-common.MAX_COMPLETED_IDS:],
        "failed": failed[-common.MAX_FAILED_DIGESTS:],
        "wrappers": wrappers[-common.MAX_COMPLETED_IDS:],
    }
