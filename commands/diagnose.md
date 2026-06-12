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
- use `continuity_token` only for proof-owned UUID rebinds

Do not verify by bare UUID resume. If you need to test ownership of a cached UUID, call `identity(agent_uuid=<uuid>, continuity_token=<token>, resume=true)` only when a matching current token is available.

If no proof-owned UUID rebind is available, call `identity()` to inspect current binding. Use `/governance-start` to create a fresh process identity with `parent_agent_id=<cached uuid>` if this process should inherit prior work.

Call `identity()` first when continuity or binding is unclear.

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

If the live identity differs from the slot-scoped cache, refresh that slot's cache with the latest continuity data via `scripts/session_cache.py set session --slot=<client_session_id> --merge --stamp`.

Do not dump raw JSON unless the user explicitly asks for it.
Prefer a short interpreted summary.
