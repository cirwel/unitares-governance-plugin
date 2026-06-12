# Start in Codex

Use this path if you are working from Codex or ChatGPT and want the cleanest UNITARES workflow without depending on Claude-only hooks.

## Goal

Connect to a running UNITARES governance server, preserve continuity cleanly, and check in at meaningful milestones instead of every trivial edit.

## Recommended Default

Use `explicit` mode unless you are deliberately dogfooding tighter automation.

### Modes

- `explicit`: manual onboarding/check-in/diagnosis; best default
- `dogfood-light`: explicit check-ins plus stronger milestone reminders
- `dogfood-heavy`: research mode for tighter automation and deterministic outcome capture

This plugin currently optimizes for `explicit`.

## Recommended Flow

1. Run `/governance-start`
2. Keep continuity in slot-scoped `.unitares/session-<slot>.json` caches
3. Do real work
4. Run `/checkin` after a meaningful milestone
5. Run `/diagnose` when continuity or governance state looks wrong
6. Use `/dialectic` when you need structured review

If you are not using commands directly, the equivalent raw tool flow is:

1. First run or fresh process: `start_session(force_new=true)` (`onboard(...)` is the canonical equivalent)
2. Fresh process continuing prior work: `start_session(force_new=true, parent_agent_id=<saved uuid>, spawn_reason="new_session")`
3. `sync_state()` after meaningful work (`process_agent_update(...)` is the canonical equivalent)
4. Same live owner / proof-owned rebind only: `identity(agent_uuid=..., continuity_token=..., resume=true)`
5. `check_working_state()` for read-only state checks (`get_governance_metrics(...)` is the canonical equivalent)
6. `identity()` if continuity looks wrong
7. `health_check()` if the system itself may be part of the problem

On servers with the agent-experience envelope enabled, friendly aliases lift
`next_action`, `state_summary`, `risk_summary`, `memory_suggestions`, and
`recovery_hint` when present, plus the full canonical payload under
`raw_governance`. Treat `memory_suggestions` as optional retrieval prompts and
`recovery_hint` as the first recovery route when a response reports degraded or
paused state. Older compatibility surfaces may return the canonical payload
directly; in that case read the same fields where they already appear. If a
server does not know these aliases yet, use the canonical tool names shown in
parentheses.

## Local Continuity Cache

Codex should treat continuity as local workspace state, not Claude-only adapter state.

Preferred cache path:

- `.unitares/session-<slot>.json`

Flat `.unitares/session.json` is a legacy/shared artifact. Use `scripts/session_cache.py list --workspace "$PWD"` to discover recent slots, then read a specific cache with `scripts/session_cache.py get session --slot=<slot>`.

Shared helper:

- `scripts/session_cache.py`

Treat this as local runtime state. It should not be used as a source of truth over the server, but it is the first place to look for:

- `continuity_token` when present for in-process proof-owned calls, not startup resume
- `client_session_id`
- `uuid`
- `agent_id`
- `display_name`
- `session_resolution_source`

## Minimal Session Pattern

Typical session:

- start or declare lineage with `/governance-start`
- do meaningful work
- check in after a milestone, completed step, or decision point
- diagnose only when needed

Do not treat every file edit as a governance event. High-signal check-ins are more useful than noisy ones.

## What to Watch

- `uuid`: identity anchor, not ownership proof
- `continuity_token`: short-lived ownership proof for same-owner rebinding, not indefinite cross-process resume
- `client_session_id`: in-session transport continuity metadata
- `parent_agent_id`: lineage declaration for a fresh process continuing prior work
- `session_resolution_source`: if this falls back to a weak source, rerun `/governance-start`
- `identity_assurance`: strong is better than implicit

## Commands

- `/governance-start` to create or declare lineage and refresh local continuity state
- `/checkin` for a governance update after meaningful work
- `/diagnose` for identity, state, and operator diagnostics
- `/dialectic` for structured review

## Claude Note

Claude hooks remain supported in this repo, but they are an adapter convenience, not the canonical UNITARES workflow. The server is the source of truth; the client should stay thin.
