---
description: "Show current UNITARES governance state and operator-relevant diagnostics"
---

Start by inventorying slot-scoped session caches in the current workspace.

Use the shared helper in this plugin repo:

- `scripts/session_cache.py list --workspace "$PWD"` — slot inventory sorted newest-first
- `scripts/session_cache.py get session --slot=<slot>` — read a specific cache

Bare `get session` (no `--slot`) returns the legacy flat `session.json`, which under S20 is read-only-legacy — surface it only as a lineage candidate, never as the current process's identity.

If continuity state exists:

- treat `uuid` as a local identity anchor and lineage candidate
- ignore any `continuity_token` field in the cache; it is never proof

Never take a `continuity_token` from any cache file, whether a legacy v1/flat
`session.json` or a v2 slot cache: only a token returned to this live process
counts as proof. v2 slot caches do not persist it, and a token found in a legacy
file is not a credential.

Do not verify by bare UUID resume. If you need to test ownership of a cached UUID, call `identity(agent_uuid=<uuid>, continuity_token=<token>, resume=true)` only when this live process holds a matching token returned to it by the server.

If no proof-owned UUID rebind is available, call `identity(client_session_id=<your client_session_id>)` to inspect current binding. Never call `identity()` with no arguments: with no proof argument the call is gated to a fresh mint and reports on an identity it just created, so read `identity_assurance.caller_proven` before trusting the result. If you have no `client_session_id`, report the identity as unknown and suggest `/governance-start`, which declares the cached uuid as a parent only for a deliberate handoff from an exited process.

Then call `get_governance_metrics` for the current agent using the same continuity data.

Call `health_check()` only when system health, not agent state, may be part of the issue.

Display:

- whether identity was proof-resumed, freshly created, or created with lineage
- `identity_status`
- `bound_identity`
- `session_resolution_source`
- `continuity_token_supported`
- `identity_assurance`
- deprecation warnings
- whether continuity looks strong or weak
- E, I, S, V
- coherence
- risk score
- verdict
- `next_action`, `memory_suggestions`, and `recovery_hint` when present
- summary or mode/basin if available
- behavioral vs ODE authority when it is obvious in the response

If `health_check()` is used, also show:

- overall system status
- degraded checks
- first operator action

If the live identity differs from the slot-scoped cache, refresh that slot's cache with the latest identity fields as the payload (without `--json` the helper has nothing to write: it refuses a new slot and only bumps `updated_at` on an existing one):
`scripts/session_cache.py set session --slot=<client_session_id> --merge --stamp --json '{"uuid":"<uuid>","client_session_id":"<client_session_id>","schema_version":2}'`.
Do not write `continuity_token` to the cache.

For a local contract audit, run
`python3 scripts/audit_identity_contract.py --workspace "$PWD"`. Treat `error`
findings as cache contract violations. Treat `warning` findings as operator
signals: weak resolution source, flat legacy cache, floor observation, or
delivery failure.

Do not dump raw JSON unless the user explicitly asks for it.
Prefer a short interpreted summary.
