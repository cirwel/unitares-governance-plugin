#!/usr/bin/env python3
"""PreToolUse injector — ensure governance MCP calls carry client_session_id.

Claude Code's streamable HTTP transport is stateless (no Mcp-Session-Id), so
any governance call that omits identity arguments resolves server-side via
the `recent_onboard:<ip:ua>` Redis pin — a heuristic that any parallel
non-subagent onboard on the same host can legitimately displace (the
2026-06-10 driver-capture incident class; server PR #604 closed the subagent
displacement vector, this hook removes the driver's dependence on the pin
entirely). Calls carrying client_session_id resolve at step 2 with
identity_assurance tier=strong and are immune to the pin.

Contract:
- Only fires for MCP tools whose server segment contains "unitares".
- Only fires for an explicit suffix allowlist of attribution-relevant tools
  verified to accept client_session_id (schemas inherit AgentIdentityMixin
  server-side). Unknown/new tools get NO injection — they degrade to today's
  pin behavior rather than risking an extra-field validation error.
- NEVER fires for identity-minting/binding tools (onboard, start_session,
  bind_session): presence of client_session_id is a resume proof signal
  there, and injecting one would silently flip fresh-mint semantics.
- Skips when the call already carries any identity proof field
  (client_session_id, continuity_token, agent_uuid, agent_id) — explicit
  caller intent always wins.
- Reads ONLY the slot-scoped session cache (slot = Claude session_id via
  _session_lookup; no workspace-flat fallback). With the post-identity
  subagent guard, the slot cache holds the driver's identity.
- Emits hookSpecificOutput.updatedInput WITHOUT permissionDecision, so the
  normal permission flow is unchanged.
- Fails open: any error or missing cache → no output, tool runs untouched.

Known residual (KNOWINGLY ACCEPTED, council-reviewed 2026-06-12): both this
guard and server #604 key on spawn_reason=="subagent" being DECLARED. A
non-declaring subagent is invisible to both: its onboard still overwrites
the slot cache (post-identity can't see it), and this injector would then
propagate that poisoned identity into the driver's later argless calls at
tier=strong — overriding the otherwise-protective server pin. For DECLARED
subagents the no-widening claim holds: their argless calls already resolve
to the driver via the standing SET-NX pin, so injection makes the same
attribution explicit and lintable. The deferred close-path for the
non-declaring residual is subagent discrimination from hook context
(e.g. transcript_path), rejected for now because misclassifying a
succession onboard would dark-session the driver — the inverse failure.

Epistemic note: rows resolved through an injected client_session_id read
server-side as identity_assurance tier=strong / explicit_client_session_id.
That tier here reflects HARNESS-MAINTAINED process continuity, not an
agent-asserted proof — the agent never typed the token.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Attribution-relevant tools whose server schemas accept client_session_id
# (all inherit AgentIdentityMixin), plus their friendly-workflow aliases.
# Deliberately EXCLUDES every identity-minting/resuming/binding tool —
# onboard, start_session, identity, bind_session — because a present
# client_session_id is a resume proof signal there: injecting one would
# silently flip fresh-mint/assert semantics, and the post-identity hook
# would then cache the poisoned response (council finding 2026-06-12).
# Also excludes any tool not verified against the server schemas.
INJECT_SUFFIXES = frozenset({
    "process_agent_update", "sync_state",
    "get_governance_metrics", "check_working_state",
    "outcome_event", "record_result",
    "knowledge", "search_shared_memory", "leave_note",
    "dialectic", "request_review",
    "observe", "calibration", "export", "config",
    "agent", "self_recovery", "archive_orphan_agents",
    "list_tools", "describe_tool", "health_check",
})

PROOF_FIELDS = ("client_session_id", "continuity_token", "agent_uuid", "agent_id")


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw or "{}")
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0

    tool_name = data.get("tool_name") or ""
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return 0
    parts = tool_name.split("__")
    if len(parts) < 3:
        return 0
    server = "__".join(parts[1:-1]).lower()
    suffix = parts[-1].lower()
    if "unitares" not in server:
        return 0
    if suffix not in INJECT_SUFFIXES:
        return 0

    tool_input = data.get("tool_input")
    if tool_input is None:
        tool_input = {}
    if not isinstance(tool_input, dict):
        return 0
    for field in PROOF_FIELDS:
        value = tool_input.get(field)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        # Any non-empty proof field: the caller declared identity intent.
        return 0

    try:
        from _session_lookup import load_session_for_hook
        sess = load_session_for_hook(Path.cwd(), raw)
    except Exception:
        return 0
    sid = (sess or {}).get("client_session_id")
    if not isinstance(sid, str) or not sid.strip():
        return 0

    updated = dict(tool_input)
    updated["client_session_id"] = sid.strip()
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": updated,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
