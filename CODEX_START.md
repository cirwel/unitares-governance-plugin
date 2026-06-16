# Start in Codex

Use this path if you are working from Codex or ChatGPT and want the cleanest UNITARES workflow without depending on Claude-only hooks.

## Goal

Connect to a running UNITARES governance server, preserve continuity cleanly, and check in once per assistant turn as a behavioral baseline. Add milestone check-ins for substantial work; avoid per-tool or per-edit noise.

## Recommended Default

Use `explicit` mode unless you are deliberately dogfooding tighter automation.

### Modes

- `explicit`: manual onboarding/check-in/diagnosis; best default
- `dogfood-light`: explicit check-ins plus stronger milestone reminders
- `dogfood-heavy`: research mode for tighter automation and deterministic outcome capture

This plugin currently optimizes for `explicit`. If you want adapter-like
onboarding/check-in behavior from Codex, run the sidecar and send governance
REST tool calls through it.

```bash
python3 scripts/identity_sidecar.py --server-url http://localhost:8767 --workspace "$PWD" --slot codex-local
```

The sidecar wraps `/v1/tools/call`, lazily onboards when the slot has no cached
`client_session_id`, injects that session id into attribution-relevant
governance calls, and provides `/turn/checkin`, `/turn/stop`, and `/audit`.
It is not a full MCP transport proxy yet.

## Recommended Flow

1. Run `/governance-start`
2. Keep continuity in slot-scoped `.unitares/session-<slot>.json` caches
3. Do real work
4. Run `/checkin` once per assistant turn, and after meaningful milestones
5. Run `/diagnose` when continuity or governance state looks wrong
6. Use `/dialectic` when you need structured review

If you are not using commands directly, the equivalent raw tool flow is:

1. First run or fresh process: `start_session(force_new=true)` (`onboard(...)` is the canonical equivalent)
2. Fresh process continuing prior work: `start_session(force_new=true, parent_agent_id=<saved uuid>, spawn_reason="new_session")`
3. `sync_state()` once per assistant turn, and after meaningful work (`process_agent_update(...)` is the canonical equivalent)
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

- `client_session_id`
- `uuid`
- `agent_id`
- `display_name`
- `session_resolution_source`

Do not persist `continuity_token` in this cache. v2 slot caches are lineage and
transport-continuity hints only; a token belongs only to the live response that
returned it and to rare same-live-process proof-owned rebinds.

## Minimal Session Pattern

Typical session:

- start or declare lineage with `/governance-start`
- do meaningful work
- check in once per assistant turn as a baseline
- add a check-in after a milestone, completed step, or decision point
- diagnose only when needed

Do not treat every file edit or tool call as a governance event. Turn-level baseline check-ins are useful; raw file churn is not.

## What to Watch

- `uuid`: identity anchor, not ownership proof
- `continuity_token`: short-lived ownership proof for same-owner rebinding, not indefinite cross-process resume
- `client_session_id`: in-session transport continuity metadata
- `parent_agent_id`: lineage declaration for a fresh process continuing prior work
- `session_resolution_source`: if this falls back to a weak source, rerun `/governance-start`
- `identity_assurance`: strong is better than implicit

Use the local audit when continuity looks suspicious:

```bash
python3 scripts/audit_identity_contract.py --workspace "$PWD"
```

It checks the neutral cache and check-in log for token-at-rest violations, empty
identity stubs, weak resolution sources, and floor/failure log statuses.

## Commands

- `/governance-start` to create or declare lineage and refresh local continuity state
- `/checkin` for the turn baseline and meaningful milestones
- `/diagnose` for identity, state, and operator diagnostics
- `/dialectic` for structured review

## Claude Note

Claude hooks remain supported in this repo, but they are an adapter convenience, not the canonical UNITARES workflow. The server is the source of truth; the client should stay thin.
