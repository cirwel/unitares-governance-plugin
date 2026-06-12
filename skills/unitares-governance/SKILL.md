---
name: unitares-governance
description: >
  Compatibility umbrella skill for the UNITARES governance framework. Use this
  as the entrypoint when you need the overall model and route into the split
  governance skills.
license: Apache-2.0
compatibility: Requires UNITARES governance MCP server (gov.cirwel.org or local http://127.0.0.1:8767/mcp/)
metadata:
  unitares.last_verified: "2026-06-11"
  unitares.freshness_days: "14"
---

# UNITARES Governance

This umbrella skill exists for backward compatibility and as a stable top-level
entrypoint into the UNITARES framework.

## Core Model

UNITARES evaluates agent state with the **EISV** model:

- `E`: effective energy / execution drive
- `I`: integrity / coherence of alignment
- `S`: entropy / disorder / instability
- `V`: void pressure / collapse tendency

Agents typically start with `start_session(force_new=true)` and continue with
`sync_state()` as their main check-in loop. These are friendly aliases over
the canonical `onboard(...)` and `process_agent_update(...)` tools; alias
responses put `next_action` and compact state fields first while preserving the
full canonical payload under `raw_governance`.

## Session Continuity

Use `start_session(force_new=true)` to register a fresh process identity. If
the process is continuing prior work, declare that with
`parent_agent_id=<prior uuid>` and `spawn_reason="new_session"`.
Use `onboard(...)` instead when targeting older servers or when a raw canonical
response shape is required.

For continuing prior work in a fresh process, the v2 posture is lineage
declaration via `parent_agent_id` (above), not UUID rebind.
`identity(agent_uuid=..., continuity_token=..., resume=true)` is real
(PATH 0) and works as an ownership-proven rebind to a still-live UUID,
but it is the explicit-rebind case, not the default. The
`continuity_token` is short-lived (1h, rolling) anti-hijack proof, not
indefinite cross-process continuity.

S13/S1-c precision: the server's fresh-instance gate auto-promotes
`force_new=true` and emits `[FRESH_INSTANCE]` only for **truly arg-less**
`onboard()` calls. Proof-shaped arguments — including `onboard(name=...)`
— suppress that gate and can fall through to weak session/IP:UA pin
behavior. Token-only `onboard`, `identity`, and `bind_session` are now
retired and return `status=continuity_token_resume_rejected`. Pass
`force_new=true` explicitly whenever you mean to mint fresh. Bare
`identity(agent_uuid=<uuid>)` without a matching token remains the
canonical hijack pattern and is strict-mode rejected.

Use `sync_state()` after meaningful work to record progress,
complexity, and confidence, then read the returned governance verdict.
Use `process_agent_update(...)` as the canonical/raw equivalent.
The response includes an `identity_assurance` block (`tier`, `score`,
`session_source`, `reason`) — check it after check-in to confirm strong
continuity, especially when calling with `require_strong_identity=true`.

`get_governance_metrics()` is read-only. If no identity is bound, current
servers return an `unbound` diagnostic with a `next_action` hint instead of
minting a fresh identity as a side effect.

## Knowledge Layer

The governance system is coupled to the **knowledge graph**. Agents should
search existing knowledge before duplicating work, and contribute discoveries,
questions, and answers as they learn. Prefer the unified
`knowledge(action="search" | "note" | "store" | "synthesize" | ...)` surface;
legacy one-tool-per-operation aliases are compatibility paths. `synthesize`
is an explicit lifecycle action for topic rollups, not a write-time hook.

## Split Skills

The old monolithic skill was split into focused skills:

- `skills/governance-lifecycle/SKILL.md` for onboarding, check-ins, and recovery
- `skills/governance-fundamentals/SKILL.md` for EISV, basins, coherence, and verdicts
- `skills/knowledge-graph/SKILL.md` for knowledge graph search and contribution
- `skills/dialectic-reasoning/SKILL.md` for thesis/antithesis/synthesis workflows
- `skills/discord-bridge/SKILL.md` for the Discord governance bridge

If you need the full mental model, start here. If you know the task shape,
prefer the focused skill directly.
