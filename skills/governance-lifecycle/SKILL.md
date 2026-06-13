---
name: governance-lifecycle
description: >
  Use when an agent is interacting with UNITARES governance for the first time, needs to
  onboard, check in, or recover from a pause/reject verdict. Covers the full agent lifecycle
  from session start through check-ins to recovery.
license: Apache-2.0
compatibility: Requires UNITARES governance MCP server (gov.cirwel.org or local http://127.0.0.1:8767/mcp/)
metadata:
  unitares.last_verified: "2026-06-13"
  unitares.freshness_days: "14"
---

# Agent Lifecycle

## Friendly Workflow Names

Current UNITARES servers expose task-verb aliases for the core agent workflow.
Prefer them when you want the most agent-readable response shape; use the
canonical names when you need legacy/raw compatibility.

| Job | Friendly alias | Canonical tool |
| --- | --- | --- |
| Start working | `start_session(force_new=true, ...)` | `onboard` |
| Check in after meaningful work | `sync_state(response_text=..., complexity=...)` | `process_agent_update` |
| Check your working state | `check_working_state()` | `get_governance_metrics` |
| Avoid duplicate work | `search_shared_memory(query=...)` | `knowledge(action="search")` |
| Record what actually happened | `record_result(...)` | `outcome_event` |
| Ask for a structured review | `request_review(issue_description=...)` | `dialectic(action="request")` |

The aliases accept the same parameters and inherit the same identity rules as
their canonical tools. Alias responses put `next_action`, `state_summary`,
`risk_summary`, `memory_suggestions`, and `recovery_hint` first when present,
with the full canonical payload preserved under `raw_governance`.

## Starting a Session

**Onboard at the start of the session, before other work — then check in as you
go.** Until you onboard, your MCP transport is unbound and your work is invisible
to governance: no identity, no trajectory, and the turn/edit/end check-in hooks
have nothing to attach to, so they stay silent the entire session. A session
that never onboards is the main source of *uninitialized, 0-update* agents. The
lifecycle is: `start_session()` first, then land a real `sync_state()` after
each meaningful unit of work so the identity carries real signal.

Per identity.md v2 ontology, a fresh process-instance is a fresh agent. To continue prior work across processes, **declare lineage** — do not resume via token:

```
onboard(force_new=true, spawn_reason="explicit")            # genuinely new work, no lineage
onboard(force_new=true, parent_agent_id="<prior-uuid>",     # continuing prior work in a fresh process
        spawn_reason="new_session")
```

`name=` is cosmetic — passing `name="Same-Agent"` does not re-bind to an existing agent.

### Optional: seed a trajectory anchor at onboard

> This is a minor nicety, **not** a fix for showing up as "uninitialized." An
> agent reads as uninitialized / 0 updates until it lands a **real** check-in;
> a genesis seed does not change that (see below). The real lever is the
> lazy-onboard + real-`sync_state()` lifecycle above.

Once you *have* decided to onboard, you may pass an `initial_state` genesis seed
so that your first real `sync_state()` immediately produces a trajectory *delta*
instead of a lone point:

```
start_session(force_new=true, initial_state={
  "response_text": "Genesis: <one line on what this session is for>",
  "complexity": 0.1,
  "confidence": 0.5,
})
```

`initial_state` writes a synthetic `source='bootstrap'` state row immediately
after identity creation. Bootstrap rows seed **trajectory genesis only** — they
are excluded from calibration, outcome correlation, trust-tier counts, and
**real-check-in counts**. So the seed does **not** clear an "uninitialized / 0
real updates" status — only a genuine `sync_state()` does. Its sole benefit is
the trajectory baseline. (In the Claude adapter, `onboard_helper.py` leaves this
off by default; enable with `--bootstrap` or `UNITARES_ONBOARD_BOOTSTRAP=1`.)

### Subagents and dispatched work

Every `onboard`/`start_session` mints a **new** agent record. A dispatched
subagent that onboards but never checks in is the dominant source of
*uninitialized, 0-update* ghosts — the plugin's turn/edit/end check-ins route
to the **driver's** identity, not the subagent's, so the subagent's record gets
zero updates by construction.

So, for short-lived dispatched/Task subagents:

- **Prefer not to onboard at all.** Brief, attributable work can run under the
  driver's existing session — no separate identity, no ghost.
- **If a subagent genuinely needs its own identity** (long-running, separately
  governed work), then it must (a) declare `spawn_reason="subagent"` and
  `parent_agent_id=<driver uuid>`, **and** (b) land at least one real
  `sync_state()` before it exits (optionally seeding `initial_state` as above
  to anchor it). An identity that
  cannot meet (c) should not be minted.

You get back a **UUID** (your identity for this process), a **client_session_id** (within-process transport continuity), and a **continuity_token** (per-process anti-hijack proof, narrowly scoped — see `references/resume-semantics.md` before passing it forward to anything). The response also includes `session_resolution_source`, `continuity_token_supported`, `ownership_proof_version`, and a `deprecations` field when present.

The PATH semantics, the rare same-live-process rebind case, the S13 fresh-instance gate detail, the canonical hijack pattern, and why "save the token and pass it everywhere" is now an anti-pattern — all in `references/resume-semantics.md`. Read that before designing any client that handles tokens.

## Check-ins

Call `sync_state()` (`process_agent_update(...)` canonically) after meaningful work:

```
sync_state(
  response_text: "Brief summary of what you did",
  complexity: 0.0-1.0,   # task difficulty estimate
  confidence: 0.0-1.0,   # how confident you are (be honest)
  ethical_drift: [0.0, 0.0, 0.0]  # optional: primary_drift, coherence_loss, complexity_contribution
)
```

Ordinary check-ins use the active session binding or `client_session_id`; do **not** pass `continuity_token` to `process_agent_update`. Tokens are reserved for explicit PATH 0 ownership rebinds such as `identity(agent_uuid=..., continuity_token=..., resume=true)`.

If you include `ethical_drift`, current runtimes return `input_glossary.ethical_drift` naming the three positional components. Use that response metadata instead of guessing what each slot means.

When to check in:
- After completing a meaningful unit of work
- Before and after high-complexity tasks
- When you feel uncertain or notice drift
- **Not** after every single tool call — use judgment

Returns a verdict plus current EISV metrics. The response also includes an `identity_assurance` block (`tier`, `score`, `session_source`, `trajectory_confidence`, `reason`) — read it after check-in to confirm strong continuity, especially if calling with `require_strong_identity=true`.

## Reading Verdicts

| Verdict | What to Do |
|---------|-----------|
| **proceed** | Continue normally |
| **guide** + guidance text | Read the guidance, adjust your approach, keep going |
| **pause** | Stop your current task. Reflect on what is flagged. See `references/recovery.md` |
| **reject** | Significant concern. See `references/recovery.md` for recovery options |
| **margin: tight** | Near a basin edge. Be more careful with next steps |

A `guide` verdict is an early warning. Ignoring it makes `pause` more likely.

## Essential Tools

Use in every session:

- `start_session(force_new=true, parent_agent_id=...)` / `onboard(...)` — register a fresh process identity, optionally declaring lineage. Never call bare `onboard()`.
- `sync_state()` / `process_agent_update()` — check in with work summary, complexity, confidence
- `check_working_state()` / `get_governance_metrics()` — read current EISV state; read-only, and for an unbound caller it returns an `unbound` diagnostic plus `next_action` instead of creating a ghost identity
- `identity()` — confirm who the runtime thinks you are within this process; rare same-live-process PATH 0 rebind via `(agent_uuid=..., continuity_token=..., resume=true)` (see `references/resume-semantics.md`)
- `bind_session()` — explicit session rebind for a known `agent_uuid + client_session_id`; use only when bridging transports (e.g., REST hook → MCP session)
- `health_check()` — operator-facing server health when behavior seems odd
- `search_shared_memory(query=...)` / `knowledge(action="search")` — find existing knowledge before creating new entries
- `knowledge(action="note")` — quick contribution to the knowledge graph; `leave_note()` is legacy compatibility only

## Going Deeper

- `references/recovery.md` — what to do after a `pause` or `reject` verdict
- `references/resume-semantics.md` — PATH semantics, S13 gate detail, canonical hijack pattern, and why the "save the token, pass it everywhere" pattern is now an anti-pattern
- `governance-fundamentals` skill — what the EISV numbers mean
- `dialectic-reasoning` skill — how to participate in a structured review when paused
