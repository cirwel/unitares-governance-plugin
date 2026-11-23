---
description: "Show current UNITARES governance state and operator-relevant diagnostics"
---

Inventory workspace session caches with this plugin's helpers:

- `scripts/session_cache.py list --workspace "$PWD"` lists slots newest-first.
- `scripts/session_cache.py get session --slot=<slot>` reads one slot as lineage context only.

Never copy a `client_session_id` from a slot read or the inventory into a call:
any process can write workspace caches. Only this live process's own
`start_session`/`onboard` response, or a binding a trusted hook injects for its
exact host slot, proves the current session.

Bare `get session` reads legacy flat `session.json`; it and all cached UUIDs
are lineage context, never current identity or ownership proof. Ignore every
cached `continuity_token`. Only a matching token
the server returned to this live process permits
`identity(agent_uuid=<uuid>, continuity_token=<token>, resume=true)`.
Never resume by bare UUID.

Without that rebind, inspect `identity(client_session_id=<your client_session_id>)`.
Never call bare `identity()`: it mints a fresh identity. Check
`identity_assurance.caller_proven` before trusting it. Without
`client_session_id`, report identity unknown and suggest `/governance-start`,
which declares a cached UUID as a parent only for a deliberate handoff from an
exited process.

Read `get_governance_metrics` with this process's own binding, never a cached
one; without it, report identity unknown. It does not advance
EISV. Use `verbosity="standard"` or `"full"` when the compact view lacks source
or lifecycle detail.

Summarize available fields; label missing provenance unknown:

- Identity: proof-resumed, fresh, or created with lineage; `identity_status`,
  `bound_identity`, `session_resolution_source`, `continuity_token_supported`,
  `identity_assurance`, deprecation warnings, and continuity strength.
- E/I/S/V, `primary_eisv_source`, and timestamp/freshness labels.
- Coherence: `coherence_source`/`coherence_role` or nested
  `coherence.source`/`coherence.role`.
- Risk with `risk_score_source`; verdict with `verdict_source` /
  `verdict_resolution_source`, when returned.
- `write_gate`: lifecycle restriction, applied hold, recovery or expiry
  details, kept distinct from inferred recommendations.
- `next_action`, `memory_suggestions`, `recovery_hint`, summary, mode/basin.
- Behavioral assessment versus ODE feedback, as identified.

`policy_evaluation` and `enforcement` belong to assessment-bearing check-ins,
not this read or receipt-only retries; a bare verdict does not identify
enforcement posture. For a high-risk reading, inspect its inputs and task
evidence; an applied hold follows its recovery contract even when the reading
is disputed. The
[computation reference](https://github.com/cirwel/unitares/blob/master/docs/EISV_COMPUTATION.md)
defines these indices, their inputs, and evidence limits.

Use `health_check()` only if system health may be involved; report
status, degraded checks, and the first operator action.

If live identity differs from the slot cache, refresh the slot:
`scripts/session_cache.py set session --slot=<client_session_id> --merge --stamp --json '{"uuid":"<uuid>","client_session_id":"<client_session_id>","schema_version":2}'`.
Keep `--json`: without it, the helper refuses new slots and only bumps
`updated_at` on existing ones. Never cache `continuity_token`.

For a local audit, run `python3 scripts/audit_identity_contract.py --workspace "$PWD"`.
`error` means a cache contract violation; `warning` flags weak resolution,
legacy flat caches, floor observations, or delivery failure.

Give a short interpreted summary; dump raw JSON only if explicitly requested.
