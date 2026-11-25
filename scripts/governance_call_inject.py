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

This hook also normalizes the `tags` list on knowledge-graph calls (casing,
separators, de-dup) before they reach the server, so the shared graph does
not fragment on `Postgres`/`postgres`/`PostgreSQL`. That pass is
formatting-only, runs independently of (and never disables) identity
injection, and lives in scripts/tag_normalize.py — see docs/ontology-need.md.
Codex does not rewrite multi-action knowledge calls because applying any input
rewrite there would also pre-approve the call.

Contract:
- Only fires for MCP tools whose case-normalized server segment exactly matches
  a configured UNITARES alias. Lookalike names receive no rewrite or implicit
  Codex approval.
- Only fires for an explicit suffix allowlist of attribution-relevant tools
  verified to accept client_session_id (schemas inherit AgentIdentityMixin
  server-side). Unknown/new tools get NO injection — they degrade to today's
  pin behavior rather than risking an extra-field validation error.
- For identity minting tools, only fires for orchestrated anchored
  onboard/start_session calls: UNITARES_CLIENT_SESSION_ID and a truthy
  UNITARES_ORCHESTRATED marker must both be present, and force_new must be
  false. A process-owned anchor adds orchestrated=true so strict first bind
  succeeds; an unmarked or inherited anchor is ignored.
  Explicit fresh mint and explicit proof fields still win. Never
  injects into bind_session/identity.
- Denies a force_new onboard/start_session when the slot cache already names
  an identity and the call declares neither parent_agent_id nor a deliberate
  spawn_reason: that mint would give one process two identities. The deny
  reason names the bound identity and the explicit opt-in.
- Denies proofless identity() in an already-bound current-process slot. The
  server promotes that apparently read-shaped call to force_new=true, so
  letting it through would silently replace the slot's working identity.
- Preserves explicit identity fields only when they do not conflict with the
  protected current-slot binding. A foreign client_session_id or any cached-
  slot continuity_token is denied before the call can override attribution.
- Reads ONLY the slot-scoped session cache (slot = Claude session_id via
  _session_lookup; no workspace-flat fallback). With the post-identity
  subagent guard, the slot cache holds the driver's identity.
- Claude emits hookSpecificOutput.updatedInput without a permission decision,
  so its normal permission flow is unchanged. Codex requires
  permissionDecision="allow" whenever updatedInput is returned. Codex rewrites
  only anchored lifecycle, check-in, and read-only diagnostic calls; admin-
  capable tools keep their normal permission flow and require explicit proof.
- Denies, for Claude's bundled server only (matched by its tool-name
  prefix), any call whose input names a proof field anywhere (injected or
  agent-supplied, as a key, a value, text, or inside a string holding JSON)
  when UNITARES_SERVER_URL is not HTTPS or loopback HTTP as Claude's WHATWG
  URL parser reads it. The Claude manifest routes every tool on that server
  here. An input past the scan budget, a scan error, a JSON-shaped string the
  decoder rejects, and a payload the hook cannot read count as carrying
  proof. A lineage reference (parent_agent_id) alone is not proof. Calls
  that never name a proof field
  pass; other aliases are not checked because their transport is configured
  outside this plugin.
- Fails open: any error or missing cache → no output, tool runs untouched.
  Antigravity (--host agy) is different because its hook reference requires
  a `decision` in every PreToolUse response. There, "untouched" is printed as
  {"decision": "allow"}. Per that reference, `allow` defers to the next hook
  or the default policy and does not approve on the user's behalf; only
  `auto_approve` skips a prompt. The agy path prints only `allow`, `allow`
  with `overwrite`, or `deny`, and never `auto_approve` or
  `permissionOverrides`. `overwrite` is merged shallowly into the tool args,
  so a call_mcp_tool rewrite returns the whole updated `Arguments` object, in
  its original dict or JSON-string form. Fail-open is not fail-closed for
  identity: a call_mcp_tool whose `Arguments` cannot be parsed loses its
  target and is allowed. The manifest matches only `call_mcp_tool`, so
  eagerly loaded `mcp_<server>_<tool>` calls never reach this hook (#445).

Known residual (KNOWINGLY ACCEPTED, council-reviewed 2026-06-12): both this
guard and server #604 key on spawn_reason=="subagent" being DECLARED, and a
subagent's tool calls fire this hook under the DRIVER's session_id. A
non-declaring subagent's onboard no longer replaces the driver's slot cache:
post-identity keeps the protected slot owner and caches a mint for another
uuid only as the declared successor of a prior-process binding. What remains
is attribution: this injector gives a subagent's argless calls, declared or
not, the driver's client_session_id, so they are recorded as the driver at
tier=strong. For DECLARED subagents that adds nothing: their argless calls
already resolve to the driver via the standing SET-NX pin, so injection
makes the same attribution explicit and lintable. The deferred close-path for
the non-declaring residual is subagent discrimination from hook context
(e.g. transcript_path), rejected for now because misclassifying a
succession onboard would dark-session the driver — the inverse failure.

Epistemic note: rows resolved through an injected client_session_id read
server-side as identity_assurance tier=strong / explicit_client_session_id.
That tier here reflects HARNESS-MAINTAINED process continuity, not an
agent-asserted proof — the agent never typed the token.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _http_auth import is_secure_governance_url
from governance_tool_name import governance_server_aliases, governance_tool_suffix

# Tag normalization (formatting-only, fail-open) is folded into this single
# hook on purpose: a second PreToolUse hook emitting its own updatedInput for
# the same call would risk clobbering the client_session_id injected here. The
# import is guarded so a tag_normalize failure can never disable the
# identity-injection path below. See scripts/tag_normalize.py and
# docs/ontology-need.md.
try:
    from tag_normalize import TAG_BEARING_SUFFIXES, normalize_call_tags
except Exception:  # pragma: no cover - identity injection must work alone
    TAG_BEARING_SUFFIXES = frozenset()

    def normalize_call_tags(_tool_input: dict) -> bool:  # type: ignore[misc]
        return False

# Attribution-relevant tools whose server schemas accept client_session_id
# (all inherit AgentIdentityMixin), plus their friendly-workflow aliases.
# Deliberately EXCLUDES every identity-minting/resuming/binding tool from the
# general slot-cache injector. onboard/start_session have a narrower anchored
# exception below: inject UNITARES_CLIENT_SESSION_ID only when force_new is
# absent. identity/bind_session remain excluded because injecting proof there
# would silently flip assert/bind semantics (council finding 2026-06-12).
# Also excludes any tool not verified against the server schemas.
INJECT_SUFFIXES = frozenset({
    "process_agent_update", "sync_state",
    "get_governance_metrics", "check_working_state",
    "outcome_event", "record_result",
    "knowledge", "search_shared_memory", "leave_note",
    "store_finding", "update_finding",
    "dialectic", "request_review",
    "observe", "calibration", "export", "config",
    "agent", "self_recovery", "archive_orphan_agents",
    "list_tools", "describe_tool", "health_check",
    # The server's initial tool advertisement also carries these two. Both
    # accept client_session_id; without injection they fell back to the
    # server's fingerprint guess and could resolve to a neighbouring session
    # (observed 2026-10-20: a consult attributed to another agent's UUID).
    "consult", "use_tool",
})

# Codex can only apply updatedInput by also allowing the tool call. Keep that
# implicit approval narrowly scoped to lifecycle/check-in and read-only
# diagnostics. Administrative and multi-action tools must carry identity
# explicitly so their normal permission flow remains intact. Shared-memory
# writes (knowledge, leave_note, store_finding, update_finding) stay out for
# the same reason: rewriting them would pre-approve a durable write.
CODEX_REWRITE_SUFFIXES = frozenset({
    "process_agent_update", "sync_state",
    "get_governance_metrics", "check_working_state",
    "list_tools", "describe_tool", "health_check",
})

# The server forwards an outer client_session_id into the dispatched tool's
# arguments when they lack one, so a gateway call is injected only when its
# target would be injected if called directly (see _gateway_target_injectable).
GATEWAY_SUFFIXES = frozenset({"use_tool"})

ANCHORED_MINT_SUFFIXES = frozenset({"onboard", "start_session"})
IDENTITY_ASSERT_SUFFIXES = frozenset({"identity"})
PROOF_FIELDS = ("client_session_id", "continuity_token", "agent_uuid", "agent_id")
SLOT_BOUND_PROOF_FIELDS = ("client_session_id", "continuity_token")
# Claude scopes the plugin's own .mcp.json server under this alias. Its URL is
# ${UNITARES_SERVER_URL:-http://localhost:8767}/mcp/, so the hook can see the
# transport it uses. Other aliases are registered elsewhere and stay unchecked.
BUNDLED_CLAUDE_SERVER_ALIAS = "plugin_unitares-governance_unitares-governance"
BUNDLED_CLAUDE_DEFAULT_SERVER_URL = "http://localhost:8767"
# spawn_reason values that declare a deliberate new identity inside an already
# bound slot. "new_session" and an absent reason do not: a process that is
# already bound is not a new session.
DELIBERATE_SPAWN_REASONS = frozenset({
    "explicit", "subagent", "dialectic_reviewer", "dispatch", "compaction",
})


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _current_slot_binding(raw: str, *, idle_is_inherited: bool = False) -> dict:
    """Return a usable binding owned by this process, or an empty dict.

    With ``idle_is_inherited``, a binding idle past the server's session
    lifetime is treated like one marked ``prior_process``: an earlier process
    whose resume the host did not report (``binding_activity.py``). Only the
    force_new guard uses it. A mint through start_session or onboard always
    replaces the cache; a bare identity() over an unmarked binding is
    discarded by post-identity, so that guard keeps refusing.
    """
    try:
        from _session_lookup import (
            _extract_slot,
            load_usable_session_for_hook,
            resolve_proof_session_file,
        )
        sess = load_usable_session_for_hook(Path.cwd(), raw) or {}
    except Exception:
        return {}
    uuid = str(sess.get("uuid") or "").strip()
    csid = str(sess.get("client_session_id") or "").strip()
    if not uuid or not csid or sess.get("prior_process") is True:
        return {}
    if idle_is_inherited:
        try:
            from binding_activity import is_idle
            slot = _extract_slot(raw)
            path = resolve_proof_session_file(slot) if slot else None
            if path is not None and is_idle(path, slot, uuid, csid):
                return {}
        except Exception:
            pass
    return sess


def _duplicate_mint_reason(tool_input: dict, raw: str) -> str:
    """Why a force_new mint would split an already bound process, or "".

    A slot whose cache already names an identity belongs to one process. A
    second force_new mint there gives that process two identities that the
    lineage rules can never join (its first identity is still live, so naming
    it as parent is rejected as coincidental). Seen live 2026-10-14. A call
    that declares lineage (parent_agent_id) or a deliberate spawn_reason
    passes through: that is the explicit opt-in the identity invariants keep.
    """
    if not _truthy(tool_input.get("force_new")):
        return ""
    parent = tool_input.get("parent_agent_id")
    if isinstance(parent, str) and parent.strip():
        return ""
    reason = str(tool_input.get("spawn_reason") or "").strip().lower()
    if reason in DELIBERATE_SPAWN_REASONS:
        return ""
    sess = _current_slot_binding(raw, idle_is_inherited=True)
    uuid = str(sess.get("uuid") or "").strip()
    csid = str(sess.get("client_session_id") or "").strip()
    # Deny only over a usable binding: the advice to keep using it depends on
    # the injected client_session_id, so a cache without one is no binding.
    if not uuid or not csid:
        return ""
    origin = str(sess.get("onboard_origin") or "").strip() or "unknown"
    return (
        f"This process is already bound to governance as agent_uuid {uuid} "
        f"(onboard_origin={origin}). force_new would mint a second identity for "
        "the same process. Keep using the bound one: the hook adds its "
        f"client_session_id ({csid}) to governance calls, so call "
        "check_working_state or sync_state directly. If you deliberately need "
        "a new identity here (a real spawn or handoff), call again with "
        "parent_agent_id or spawn_reason=\"explicit\"."
    )


def _proofless_identity_reason(tool_input: dict, raw: str) -> str:
    """Why bare identity() would silently replace this process's binding."""
    if _has_proof_field(tool_input):
        return ""
    name = tool_input.get("name")
    if isinstance(name, str) and name.strip():
        return ""
    if _truthy(tool_input.get("force_new")):
        return ""
    sess = _current_slot_binding(raw)
    uuid = str(sess.get("uuid") or "").strip()
    csid = str(sess.get("client_session_id") or "").strip()
    if not uuid or not csid:
        return ""
    return (
        f"This process is already bound to governance as agent_uuid {uuid}. "
        "Bare identity() has no read-only meaning: the server treats it as "
        "force_new=true and would replace this process's working identity. "
        f"Inspect the binding with identity(client_session_id=...) using {csid}, "
        "or call check_working_state directly."
    )


def _deny_output(reason: str, host: str = "claude") -> dict:
    if host == "agy":
        return {"decision": "deny", "reason": reason}
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def _hook_output(
    updated: dict,
    host: str,
    *,
    is_call_mcp_tool: bool = False,
    args_was_str: bool = False,
) -> dict:
    """Build the host-specific PreToolUse rewrite envelope."""
    if host == "agy":
        overwrite: dict[str, object] = (
            {"Arguments": json.dumps(updated) if args_was_str else updated}
            if is_call_mcp_tool
            else updated
        )
        return {"decision": "allow", "overwrite": overwrite}
    specific: dict[str, object] = {
        "hookEventName": "PreToolUse",
        "updatedInput": updated,
    }
    if host == "codex":
        # Codex ignores updatedInput unless the hook explicitly allows it.
        specific["permissionDecision"] = "allow"
    return {"hookSpecificOutput": specific}


def _unwrap_agy_scalar(value: object) -> str:
    """Decode plain or JSON-quoted string values in Antigravity toolCall.args."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            decoded = json.loads(text)
            if isinstance(decoded, str):
                return decoded
        except ValueError:
            return text
    return text


def _agy_direct_mcp_suffix(tool_name: str) -> str | None:
    """Resolve a direct Antigravity MCP tool name (mcp__ or mcp_ prefix)."""
    suffix = governance_tool_suffix(tool_name, host="agy")
    if suffix is not None:
        return suffix
    lowered = tool_name.strip().casefold()
    if lowered.startswith("mcp_"):
        rest = lowered[4:]
        for server in sorted(governance_server_aliases("agy"), key=len, reverse=True):
            prefix = f"{server}_"
            if rest.startswith(prefix) and len(rest) > len(prefix):
                return rest[len(prefix):]
    return None


def _extract_agy_call(
    data: dict,
) -> tuple[str | None, dict | None, bool, bool]:
    """Extract ``(suffix, tool_input, is_call_mcp_tool, args_was_str)`` for agy PreToolUse."""
    tool_call = data.get("toolCall")
    if not isinstance(tool_call, dict):
        return None, None, False, False
    name = str(tool_call.get("name") or "").strip()
    raw_args = tool_call.get("args")
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        return None, None, False, False
    if name == "call_mcp_tool":
        server = _unwrap_agy_scalar(raw_args.get("ServerName")).strip().casefold()
        tool = _unwrap_agy_scalar(raw_args.get("ToolName")).strip().casefold()
        if not server or not tool or server not in governance_server_aliases("agy"):
            return None, None, True, False
        raw_inner = raw_args.get("Arguments")
        if raw_inner is None:
            return tool, {}, True, False
        if isinstance(raw_inner, dict):
            return tool, raw_inner, True, False
        if isinstance(raw_inner, str):
            if not raw_inner.strip():
                return tool, {}, True, True
            try:
                parsed = json.loads(raw_inner)
            except ValueError:
                return None, None, True, False
            if isinstance(parsed, dict):
                return tool, parsed, True, True
        return None, None, True, False
    suffix = _agy_direct_mcp_suffix(name)
    if suffix is None:
        return None, None, False, False
    return suffix, dict(raw_args), False, False


_AGY_TURN_MINT_TTL_S = 1800.0


def _has_slot_bound_proof(tool_input: dict) -> bool:
    """True if the caller supplied a real session proof (client_session_id/continuity_token)."""
    for field in SLOT_BOUND_PROOF_FIELDS:
        value = tool_input.get(field)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False


def _agy_claim_turn_mint_guard(
    data: dict, raw: str, *, kind: str
) -> str:
    """Block a second unparented start_session/onboard mint within the same unbound turn.

    Checks both the current turn's transcript (catching completed in-turn mints
    before Stop writes the slot cache, and allowing retries when an earlier
    marked attempt already finished without returning a usable binding) and a
    per-slot flock-guarded turn marker (catching parallel or pre-flush double
    mints).
    """
    turn_boundary_step: int | None = None
    failed_attempts: list[dict] = []
    try:
        from stop_hook_event import (
            _agy_turn_mint_marker_path,
            inspect_current_turn_agy_onboards,
        )

        candidates, failed_attempts, turn_boundary_step = inspect_current_turn_agy_onboards(
            data
        )
        if candidates:
            first_cand, _ = candidates[0]
            first_uuid = str(first_cand.get("uuid") or "").strip()
            first_csid = str(first_cand.get("client_session_id") or "").strip()
            if first_uuid and first_csid:
                return (
                    f"This Antigravity turn already minted governance identity "
                    f"agent_uuid {first_uuid} (client_session_id={first_csid}). "
                    f"A second {kind} mint in the same turn would create a duplicate "
                    f"identity. Reuse client_session_id={first_csid} on governance "
                    'calls, or pass spawn_reason="subagent" only when spawning a '
                    "separate subagent."
                )
    except Exception:
        _agy_turn_mint_marker_path = None  # type: ignore[assignment]

    try:
        from _session_lookup import _extract_slot

        slot = _extract_slot(raw)
    except Exception:
        slot = None
    if not slot:
        return ""

    if _agy_turn_mint_marker_path is not None:
        marker_path = _agy_turn_mint_marker_path(slot)
    else:
        safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in slot)[:64]
        marker_path = Path.home() / ".unitares" / f".agy-turn-mint-{safe}.json"

    tool_call = data.get("toolCall") if isinstance(data.get("toolCall"), dict) else {}
    call_id = str(tool_call.get("id") or "").strip()
    step_idx = data.get("stepIdx")
    now = time.time()

    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            str(marker_path),
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError:
        return ""

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return ""
        getuid = getattr(os, "getuid", None)
        if callable(getuid) and st.st_uid != getuid():
            return ""
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.lseek(fd, 0, os.SEEK_SET)
        raw_existing = os.read(fd, 8192).decode("utf-8", errors="replace").strip()
        if raw_existing:
            try:
                existing = json.loads(raw_existing)
            except ValueError:
                existing = None
            if isinstance(existing, dict):
                prev_ts = existing.get("ts")
                fresh = isinstance(prev_ts, (int, float)) and (now - prev_ts) < _AGY_TURN_MINT_TTL_S
                prev_boundary = existing.get("turn_boundary_step")
                new_turn = (
                    turn_boundary_step is not None
                    and prev_boundary is not None
                    and turn_boundary_step != prev_boundary
                )
                prev_call_id = str(existing.get("tool_call_id") or "").strip()
                same_call = bool(call_id and prev_call_id and call_id == prev_call_id)
                prev_baseline = existing.get("baseline_failed_count")
                if not isinstance(prev_baseline, int) or prev_baseline < 0:
                    prev_baseline = 0
                mint_failed = [
                    item
                    for item in failed_attempts
                    if item.get("tool") in ANCHORED_MINT_SUFFIXES
                ]
                failed_ids = {
                    str(item.get("call_id") or "").strip()
                    for item in mint_failed
                    if item.get("call_id")
                }
                if prev_call_id:
                    # When the pending marker has a call ID, require the failed
                    # mint attempt in the transcript to match that exact call ID
                    # so an unrelated failure (e.g. identity(resume=true) or a
                    # different call) cannot unlock a concurrent duplicate mint.
                    prior_failed = prev_call_id in failed_ids
                else:
                    prior_failed = len(mint_failed) > prev_baseline
                if fresh and not new_turn and not same_call and not prior_failed:
                    return (
                        "An unparented start_session/onboard mint is already in progress "
                        f"in this Antigravity turn. A second {kind} mint would create a "
                        "duplicate identity before Stop caches the first. Reuse the first "
                        "call's client_session_id, or pass spawn_reason=\"subagent\" only "
                        "when spawning a separate subagent."
                    )
        mint_failed_count = sum(
            1 for item in failed_attempts if item.get("tool") in ANCHORED_MINT_SUFFIXES
        )
        record = json.dumps(
            {
                "tool_call_id": call_id,
                "step_idx": step_idx,
                "turn_boundary_step": turn_boundary_step,
                "baseline_failed_count": mint_failed_count,
                "ts": now,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, record)
    except OSError:
        return ""
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return ""


def _agy_duplicate_mint_reason(
    tool_input: dict, data: dict, raw: str, *, anchor: str
) -> str:
    """Why a start_session/onboard mint would split an agy slot."""
    reason = str(tool_input.get("spawn_reason") or "").strip().lower()
    if reason == "subagent":
        return ""
    parent_raw = tool_input.get("parent_agent_id")
    parent = parent_raw.strip() if isinstance(parent_raw, str) else ""
    has_parent = bool(parent)
    has_deliberate_reason = reason in DELIBERATE_SPAWN_REASONS
    is_force_new = _truthy(tool_input.get("force_new"))
    # On start_session/onboard, only client_session_id or continuity_token
    # proves session resume; agent_id/agent_uuid alone still mints/name-claims.
    has_session_proof = _has_slot_bound_proof(tool_input)
    is_anchor_resume = bool(
        anchor
        and not is_force_new
        and not _has_proof_field(tool_input)
        and not has_parent
        and not reason
        and "orchestrated" not in tool_input
        and "resume" not in tool_input
    )
    kind = "force_new" if is_force_new else "bare start_session/onboard"
    sess = _current_slot_binding(raw, idle_is_inherited=True)
    uuid = str(sess.get("uuid") or "").strip()
    csid = str(sess.get("client_session_id") or "").strip()
    if uuid and csid:
        origin = str(sess.get("onboard_origin") or "").strip() or "unknown"
        if has_parent and parent != uuid:
            return (
                f"This Antigravity slot is already bound to governance as agent_uuid {uuid} "
                f"(onboard_origin={origin}), so parent_agent_id={parent} does not match "
                f"the active slot owner. Reuse client_session_id={csid} on governance calls "
                '(or omit it so the hook injects it). Pass spawn_reason="subagent" only '
                "when spawning a separate subagent."
            )
        if has_parent and parent == uuid and has_deliberate_reason:
            return ""
        if not is_force_new and (has_session_proof or is_anchor_resume):
            return ""
        return (
            f"This Antigravity slot is already bound to governance as agent_uuid {uuid} "
            f"(onboard_origin={origin}). A {kind} mint would create a second identity for "
            f"the same conversation. Reuse client_session_id={csid} (or omit it and let "
            "the hook inject it on governance calls). Only pass "
            'spawn_reason="subagent" when spawning a separate subagent.'
        )
    if has_parent and has_deliberate_reason:
        return ""
    if not is_force_new and (has_session_proof or is_anchor_resume):
        return ""
    return _agy_claim_turn_mint_guard(data, raw, kind=kind)


def _handle_agy_mint_call(
    mint_args: dict, data: dict, raw: str
) -> tuple[dict | None, dict | None]:
    """Evaluate a direct or use_tool-wrapped start_session/onboard call for agy.

    Returns ``(deny_output, updated_mint_args)``.
    """
    mismatch = _foreign_slot_proof_reason(mint_args, raw)
    if mismatch:
        return _deny_output(mismatch, host="agy"), None
    anchor = _env_anchor()
    duplicate = _agy_duplicate_mint_reason(mint_args, data, raw, anchor=anchor)
    if duplicate:
        return _deny_output(duplicate, host="agy"), None
    if (
        anchor
        and not _truthy(mint_args.get("force_new"))
        and not _has_proof_field(mint_args)
        and not mint_args.get("parent_agent_id")
        and not mint_args.get("spawn_reason")
        and "orchestrated" not in mint_args
        and "resume" not in mint_args
    ):
        updated_mint = dict(mint_args)
        updated_mint["client_session_id"] = anchor
        updated_mint["orchestrated"] = True
        return None, updated_mint
    return None, None


def _parse_agy_gateway_args(tool_input: dict) -> tuple[dict | None, bool, str]:
    """Parse ``use_tool``'s nested ``arguments``/``args`` payload.

    Returns ``(parsed_dict_or_None, was_json_str, field_key)``.
    """
    field_key = "arguments" if "arguments" in tool_input or "args" not in tool_input else "args"
    raw_inner = tool_input.get(field_key)
    if raw_inner is None:
        return {}, False, field_key
    if isinstance(raw_inner, dict):
        return raw_inner, False, field_key
    if isinstance(raw_inner, str):
        if not raw_inner.strip():
            return {}, True, field_key
        try:
            parsed = json.loads(raw_inner)
        except ValueError:
            return None, True, field_key
        if isinstance(parsed, dict):
            return parsed, True, field_key
    return None, False, field_key


def _decide_agy(data: dict, raw: str) -> dict:
    """Return Antigravity PreToolUse JSON output (always includes decision)."""
    allow = {"decision": "allow"}
    suffix, tool_input, is_call_mcp_tool, args_was_str = _extract_agy_call(data)
    if suffix is None or tool_input is None:
        return allow
    if (
        suffix not in INJECT_SUFFIXES
        and suffix not in ANCHORED_MINT_SUFFIXES
        and suffix not in IDENTITY_ASSERT_SUFFIXES
    ):
        return allow

    if suffix in GATEWAY_SUFFIXES:
        inner_tool = str(
            tool_input.get("tool_name") or tool_input.get("name") or ""
        ).strip().casefold()
        inner_args, inner_was_str, inner_key = _parse_agy_gateway_args(tool_input)
        if inner_tool in ANCHORED_MINT_SUFFIXES:
            if inner_args is None:
                return _deny_output(
                    "Refusing use_tool identity mint with unparseable arguments.",
                    host="agy",
                )
            deny_resp, updated_mint = _handle_agy_mint_call(inner_args, data, raw)
            if deny_resp is not None:
                return deny_resp
            if updated_mint is not None:
                updated_outer = dict(tool_input)
                updated_outer[inner_key] = (
                    json.dumps(updated_mint) if inner_was_str else updated_mint
                )
                return _hook_output(
                    updated_outer,
                    "agy",
                    is_call_mcp_tool=is_call_mcp_tool,
                    args_was_str=args_was_str,
                )
            return allow
        if inner_tool in IDENTITY_ASSERT_SUFFIXES:
            if inner_args is None:
                return _deny_output(
                    "Refusing use_tool identity call with unparseable arguments.",
                    host="agy",
                )
            mismatch = _foreign_slot_proof_reason(inner_args, raw)
            if mismatch:
                return _deny_output(mismatch, host="agy")
            duplicate = _proofless_identity_reason(inner_args, raw)
            if duplicate:
                return _deny_output(duplicate, host="agy")
            return allow
        if not _gateway_target_injectable(tool_input):
            mismatch = (
                _foreign_slot_proof_reason(inner_args, raw)
                if isinstance(inner_args, dict)
                else None
            )
            if mismatch:
                return _deny_output(mismatch, host="agy")
            return allow

    if suffix in IDENTITY_ASSERT_SUFFIXES:
        mismatch = _foreign_slot_proof_reason(tool_input, raw)
        if mismatch:
            return _deny_output(mismatch, host="agy")
        duplicate = _proofless_identity_reason(tool_input, raw)
        if duplicate:
            return _deny_output(duplicate, host="agy")
        return allow

    if suffix in ANCHORED_MINT_SUFFIXES:
        deny_resp, updated_mint = _handle_agy_mint_call(tool_input, data, raw)
        if deny_resp is not None:
            return deny_resp
        if updated_mint is not None:
            return _hook_output(
                updated_mint,
                "agy",
                is_call_mcp_tool=is_call_mcp_tool,
                args_was_str=args_was_str,
            )
        return allow

    mismatch = _foreign_slot_proof_reason(tool_input, raw)
    if mismatch:
        return _deny_output(mismatch, host="agy")

    updated = dict(tool_input)
    changed = False

    if suffix in TAG_BEARING_SUFFIXES:
        try:
            if normalize_call_tags(updated):
                changed = True
        except Exception:
            updated = dict(tool_input)

    if not _has_proof_field(tool_input):
        sess = _current_slot_binding(raw, idle_is_inherited=True)
        sid = str(sess.get("client_session_id") or "").strip()
        if sid:
            updated["client_session_id"] = sid
            changed = True

    if not changed:
        return allow
    return _hook_output(
        updated,
        "agy",
        is_call_mcp_tool=is_call_mcp_tool,
        args_was_str=args_was_str,
    )


def _has_proof_field(tool_input: dict) -> bool:
    """True if the caller supplied a non-empty identity proof field."""
    for field in PROOF_FIELDS:
        value = tool_input.get(field)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        # Any non-empty proof field: the caller declared identity intent.
        return True
    return False


def _env_anchor() -> str:
    orchestrated = os.environ.get("UNITARES_ORCHESTRATED", "").strip().lower()
    if orchestrated not in {"1", "true", "yes", "on"}:
        return ""
    owner = os.environ.get("UNITARES_ORCHESTRATED_OWNER_PID", "").strip()
    if not owner or owner != os.environ.get("UNITARES_HOOK_HOST_PID", "").strip():
        return ""
    value = os.environ.get("UNITARES_CLIENT_SESSION_ID", "")
    return value.strip()


def _foreign_slot_proof_reason(tool_input: dict, raw: str) -> str | None:
    """Reject proof that conflicts with this host slot's protected owner."""
    try:
        from _session_lookup import load_usable_session_for_hook

        session = load_usable_session_for_hook(Path.cwd(), raw)
    except Exception:
        session = {}
    if not session:
        return None
    for field in SLOT_BOUND_PROOF_FIELDS:
        value = tool_input.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        expected = session.get(field)
        if (
            not isinstance(value, str)
            or not isinstance(expected, str)
            or value.strip() != expected.strip()
        ):
            return (
                f"Refusing {field} that conflicts with the protected current-slot "
                "identity. Use the active slot binding or start a new governed session."
            )
    return None


def _gateway_target_injectable(tool_input: dict) -> bool:
    """Whether a use_tool call may carry the cached session to its target.

    Only when the target is itself injectable (never identity, onboard,
    start_session or bind_session, whose exclusions would otherwise be
    bypassed through the gateway) and its nested arguments declare no
    identity of their own.
    """
    target = tool_input.get("tool_name")
    if not isinstance(target, str):
        return False
    target = target.strip().casefold()
    if target not in INJECT_SUFFIXES or target in GATEWAY_SUFFIXES:
        return False
    nested = tool_input.get("arguments")
    if nested is None:
        nested = {}
    if not isinstance(nested, dict):
        return False
    return not _has_proof_field(nested)


# One budget for the whole proof scan: each node visited costs one, and each
# string costs its length, because the scan may decode it as JSON. A call that
# needs more is denied rather than half-scanned.
_PROOF_SCAN_BUDGET = 4_000_000
_BUNDLED_CLAUDE_TOOL_PREFIX = f"mcp__{BUNDLED_CLAUDE_SERVER_ALIAS}__"
_PROOF_KEYS = frozenset(PROOF_FIELDS)
# A proof field name in text, unless a name character runs into it from the
# left: parent_agent_id names a lineage parent, not this caller, so it alone
# must not read as agent_id. Nothing on the right is excluded, so agent_ids
# or agent_id_x still count.
_PROOF_NAME_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(f) for f in PROOF_FIELDS) + ")"
)
_JSON_START = ("{", "[", '"')


class _JsonObject(list):
    """Every (key, value) pair of one decoded JSON object, duplicates kept."""


def _is_proof_key(key: object) -> bool:
    """Whether a decoded key is a proof field name (case and spaces ignored)."""
    return isinstance(key, str) and key.strip().casefold() in _PROOF_KEYS


def _carries_proof(value: object) -> bool:
    """Whether a call input names a proof field anywhere it would be sent.

    The whole input crosses the wire, whatever the server later accepts or
    rejects, so the scan walks all of it: every dict key and value, every
    list item, and every string. A key equal to a proof field name counts as
    proof, and so does any string that contains one (see _PROOF_NAME_IN_TEXT),
    whether it is a key, a value or text.

    A string that could be JSON (it starts with {, [ or " once leading
    whitespace is stripped) is decoded with every object pair kept,
    duplicate keys included, and walked again, so escaped names are compared
    after decoding. The scan fails closed: running out of budget, or any
    exception while decoding such a string, counts as proof.
    """
    budget = _PROOF_SCAN_BUDGET
    pending = [value]
    while pending:
        item = pending.pop()
        budget -= 1 + (len(item) if isinstance(item, str) else 0)
        if budget < 0:
            return True
        if isinstance(item, dict):
            item = _JsonObject(item.items())
        if isinstance(item, _JsonObject):
            for key, val in item:
                if _is_proof_key(key):
                    return True
                pending.append(key)
                pending.append(val)
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
        elif isinstance(item, str):
            if _PROOF_NAME_IN_TEXT.search(item):
                return True
            if not item.lstrip().startswith(_JSON_START):
                continue
            try:
                pending.append(json.loads(item, object_pairs_hook=_JsonObject))
            except Exception:
                return True
    return False


def _is_bundled_claude_tool(tool_name: object) -> bool:
    """Whether ``tool_name`` belongs to the plugin's bundled Claude server.

    Claude names a tool ``mcp__<server>__<tool>``, and ``<tool>`` may itself
    contain ``__``, so the server is matched by its known prefix rather than
    by splitting the name.
    """
    return isinstance(tool_name, str) and tool_name.casefold().startswith(
        _BUNDLED_CLAUDE_TOOL_PREFIX
    )


def _insecure_bundled_transport_reason(
    host: str, data: dict, output: dict | None
) -> str:
    """Why a proof-bearing call must not reach the bundled server, or "".

    The bundled Claude transport follows UNITARES_SERVER_URL exactly as
    .mcp.json expands it. Over cleartext HTTP to a non-loopback host, any
    client_session_id or continuity_token in the call, whether this hook
    injected it or the agent supplied it, could be read on the path and
    replayed. Such calls are denied; calls that never name a proof field
    pass unchanged.
    """
    if host != "claude" or not _is_bundled_claude_tool(data.get("tool_name")):
        return ""
    specific = (output or {}).get("hookSpecificOutput") or {}
    if specific.get("permissionDecision") == "deny":
        return ""
    if _bundled_transport_is_secure():
        return ""
    effective = specific.get("updatedInput", data.get("tool_input"))
    try:
        carries = _carries_proof(effective)
    except Exception:
        carries = True
    return _INSECURE_TRANSPORT_REASON if carries else ""


_INSECURE_TRANSPORT_REASON = (
    "Refusing to send governance identity proof to the bundled UNITARES MCP "
    "server: UNITARES_SERVER_URL is not an https:// URL or a plain loopback "
    "address, so the proof could cross cleartext HTTP to another host and be "
    "read and replayed there. Set UNITARES_SERVER_URL to an https:// URL or a "
    "loopback address and restart the session."
)


def _bundled_transport_is_secure() -> bool:
    """Whether Claude's bundled server URL, as .mcp.json expands it, is safe."""
    url = os.environ.get("UNITARES_SERVER_URL") or BUNDLED_CLAUDE_DEFAULT_SERVER_URL
    try:
        return is_secure_governance_url(f"{url}/mcp/")
    except Exception:
        return False


def _unreadable_call_reason(host: str) -> str:
    """Deny reason for a hook payload the hook cannot read, or "".

    Neither the server nor the proof can be checked, so on Claude the call is
    denied while the bundled transport is insecure, whatever it names.
    """
    if host != "claude" or _bundled_transport_is_secure():
        return ""
    return _INSECURE_TRANSPORT_REASON


def _decide(host: str, data: dict, raw: str) -> dict | None:
    """Return the hook output for one governance call, or None to pass it."""
    suffix = governance_tool_suffix(data.get("tool_name"), host=host)
    if suffix is None:
        return None
    if (
        suffix not in INJECT_SUFFIXES
        and suffix not in ANCHORED_MINT_SUFFIXES
        and suffix not in IDENTITY_ASSERT_SUFFIXES
    ):
        return None
    if (
        host == "codex"
        and suffix not in CODEX_REWRITE_SUFFIXES
        and suffix not in ANCHORED_MINT_SUFFIXES
        and suffix not in IDENTITY_ASSERT_SUFFIXES
    ):
        return None

    tool_input = data.get("tool_input")
    if tool_input is None:
        tool_input = {}
    if not isinstance(tool_input, dict):
        return None

    if suffix in GATEWAY_SUFFIXES and not _gateway_target_injectable(tool_input):
        nested = tool_input.get("arguments")
        mismatch = (
            _foreign_slot_proof_reason(nested, raw)
            if isinstance(nested, dict)
            else None
        )
        if mismatch:
            return _deny_output(mismatch)
        return None

    if suffix in IDENTITY_ASSERT_SUFFIXES:
        duplicate = _proofless_identity_reason(tool_input, raw)
        if duplicate:
            return _deny_output(duplicate)
        return None

    updated = dict(tool_input)
    changed = False

    mismatch = _foreign_slot_proof_reason(tool_input, raw)
    if mismatch:
        return _deny_output(mismatch)

    if suffix in ANCHORED_MINT_SUFFIXES:
        # Anchored dispatch/beam conversations provide a stable per-thread
        # resume proof through the environment. A manual bare start_session()
        # should resume through that anchor just like the Stop hook's lazy
        # onboarding path. The exception is intentionally narrow: if the agent
        # explicitly names force_new or any proof field, pass the call through.
        anchor = _env_anchor()
        if not anchor and not _has_proof_field(tool_input):
            duplicate = _duplicate_mint_reason(tool_input, raw)
            if duplicate:
                return _deny_output(duplicate)
        if (
            anchor
            and not _truthy(tool_input.get("force_new"))
            and not _has_proof_field(tool_input)
            and not tool_input.get("parent_agent_id")
            and not tool_input.get("spawn_reason")
            and "orchestrated" not in tool_input
            and "resume" not in tool_input
        ):
            updated["client_session_id"] = anchor
            # The process-owner check in _env_anchor authorizes this first-bind
            # declaration; inherited or unmarked env pairs never reach here.
            updated["orchestrated"] = True
            changed = True
        if not changed:
            return None
        return _hook_output(updated, host)

    # Tag normalization runs independently of identity injection: it applies
    # whenever a tag-bearing knowledge call carries a `tags` list, regardless
    # of cache state or proof fields. Failures are swallowed and the partial
    # edit discarded so the identity path below is never affected.
    if suffix in TAG_BEARING_SUFFIXES:
        try:
            if normalize_call_tags(updated):
                changed = True
        except Exception:
            updated = dict(tool_input)

    # Identity injection: add the slot-cached client_session_id only when the
    # caller declared no identity intent of its own. A present proof field
    # means explicit caller intent always wins — skip injection, but still
    # honor any tag normalization above.
    if not _has_proof_field(tool_input):
        try:
            from _session_lookup import load_usable_session_for_hook
            sess = load_usable_session_for_hook(Path.cwd(), raw)
        except Exception:
            sess = None
        sid = (sess or {}).get("client_session_id")
        if isinstance(sid, str) and sid.strip():
            updated["client_session_id"] = sid.strip()
            changed = True

    if not changed:
        return None
    return _hook_output(updated, host)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize a governance PreToolUse call")
    parser.add_argument("--host", choices=("claude", "codex", "agy"), default="claude")
    args = parser.parse_args(argv)

    try:
        raw = sys.stdin.read()
        data = json.loads(raw or "{}")
    except Exception:
        if args.host == "agy":
            print(json.dumps({"decision": "allow"}))
            return 0
        unreadable = _unreadable_call_reason(args.host)
        if unreadable:
            print(json.dumps(_deny_output(unreadable)))
        return 0
    if not isinstance(data, dict):
        if args.host == "agy":
            print(json.dumps({"decision": "allow"}))
        return 0

    if args.host == "agy":
        try:
            output = _decide_agy(data, raw)
        except Exception:
            output = {"decision": "allow"}
        print(json.dumps(output))
        return 0

    try:
        output = _decide(args.host, data, raw)
    except Exception:
        # Injection fails open, but the transport check below still applies.
        output = None
    insecure = _insecure_bundled_transport_reason(args.host, data, output)
    if insecure:
        output = _deny_output(insecure)
    if output is not None:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
