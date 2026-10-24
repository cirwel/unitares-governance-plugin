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
- For identity minting tools, only fires for orchestrated anchored bare
  onboard/start_session calls: UNITARES_CLIENT_SESSION_ID and a truthy
  UNITARES_ORCHESTRATED marker must both be present, and force_new must be
  absent. Explicit force_new and explicit proof fields still win. Never
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

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from governance_tool_name import governance_tool_suffix

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


def _deny_output(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def _hook_output(updated: dict, host: str) -> dict:
    """Build the host-specific PreToolUse rewrite envelope."""
    specific: dict[str, object] = {
        "hookEventName": "PreToolUse",
        "updatedInput": updated,
    }
    if host == "codex":
        # Codex ignores updatedInput unless the hook explicitly allows it.
        specific["permissionDecision"] = "allow"
    return {"hookSpecificOutput": specific}


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize a governance PreToolUse call")
    parser.add_argument("--host", choices=("claude", "codex"), default="claude")
    args = parser.parse_args(argv)

    try:
        raw = sys.stdin.read()
        data = json.loads(raw or "{}")
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0

    suffix = governance_tool_suffix(data.get("tool_name"), host=args.host)
    if suffix is None:
        return 0
    if (
        suffix not in INJECT_SUFFIXES
        and suffix not in ANCHORED_MINT_SUFFIXES
        and suffix not in IDENTITY_ASSERT_SUFFIXES
    ):
        return 0
    if (
        args.host == "codex"
        and suffix not in CODEX_REWRITE_SUFFIXES
        and suffix not in ANCHORED_MINT_SUFFIXES
        and suffix not in IDENTITY_ASSERT_SUFFIXES
    ):
        return 0

    tool_input = data.get("tool_input")
    if tool_input is None:
        tool_input = {}
    if not isinstance(tool_input, dict):
        return 0

    if suffix in GATEWAY_SUFFIXES and not _gateway_target_injectable(tool_input):
        nested = tool_input.get("arguments")
        mismatch = (
            _foreign_slot_proof_reason(nested, raw)
            if isinstance(nested, dict)
            else None
        )
        if mismatch:
            print(json.dumps(_deny_output(mismatch)))
        return 0

    if suffix in IDENTITY_ASSERT_SUFFIXES:
        duplicate = _proofless_identity_reason(tool_input, raw)
        if duplicate:
            print(json.dumps(_deny_output(duplicate)))
        return 0

    updated = dict(tool_input)
    changed = False

    mismatch = _foreign_slot_proof_reason(tool_input, raw)
    if mismatch:
        print(json.dumps(_deny_output(mismatch)))
        return 0

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
                print(json.dumps(_deny_output(duplicate)))
                return 0
        if anchor and "force_new" not in tool_input and not _has_proof_field(tool_input):
            updated["client_session_id"] = anchor
            changed = True
        if not changed:
            return 0
        print(json.dumps(_hook_output(updated, args.host)))
        return 0

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
        return 0
    print(json.dumps(_hook_output(updated, args.host)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
