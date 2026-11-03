---
description: "Show current UNITARES governance state and operator-relevant diagnostics"
---

Inventory workspace session caches with this plugin's helpers:

- `scripts/session_cache.py list --workspace "$PWD"` lists slots newest-first.
- `scripts/session_cache.py get session --slot=<slot>` reads one slot.

Bare `get session` reads legacy flat `session.json`: read-only lineage context,
never current identity. Cached UUIDs are lineage anchors, not ownership proof.
Ignore all cached `continuity_token` values, including legacy ones; v2 caches
must not store them. Only a matching token returned to this live process by the
server permits `identity(agent_uuid=<uuid>, continuity_token=<token>, resume=true)`.
Never resume by bare UUID.

If no proof-owned UUID rebind is available, inspect
`identity(client_session_id=<your client_session_id>)`.
Never call bare `identity()`: it mints a fresh identity; check
`identity_assurance.caller_proven` before trusting a result. Without
`client_session_id`, report identity unknown and suggest `/governance-start`,
which declares a cached UUID as a parent only for a deliberate handoff from an
exited process.

Read `get_governance_metrics` using the same continuity data. This does not
advance EISV through a new check-in. Use `verbosity="standard"` or `"full"` if
the compact view lacks needed source or lifecycle detail.

Summarize available fields; label missing provenance unknown:

- Identity: proof-resumed, fresh, or created with lineage; `identity_status`,
  `bound_identity`, `session_resolution_source`, `continuity_token_supported`,
  `identity_assurance`, deprecation warnings, and continuity strength.
- E/I/S/V, `primary_eisv_source`, and timestamp/freshness labels.
- Coherence producer/role: `coherence_source` / `coherence_role` or nested
  `coherence.source` / `coherence.role`.
- Risk with `risk_score_source`; verdict with `verdict_source` /
  `verdict_resolution_source`, when returned.
- `write_gate`: current lifecycle restriction, applied hold, and recovery or
  expiry details. Distinguish these from inferred recommendations.
- `next_action`, `memory_suggestions`, `recovery_hint`, summary, mode/basin.
- Behavioral assessment versus ODE feedback, as identified by the response.

`policy_evaluation` and `enforcement` belong to assessment-bearing check-ins,
not this metrics read or receipt-only retries. A bare verdict does not identify
enforcement posture. For a high-risk reading, inspect its inputs and relevant
task evidence; an applied hold follows its recovery contract even when the
reading's interpretation is disputed. These are defined state indices and
policy classifications. The
[computation reference](https://github.com/cirwel/unitares/blob/master/docs/EISV_COMPUTATION.md)
owns their inputs, interpretations, and evidence limits.

Use `health_check()` only if system health may be involved; report overall
status, degraded checks, and the first operator action.

If live identity differs from the slot cache, refresh that slot with the latest
identity fields:
`scripts/session_cache.py set session --slot=<client_session_id> --merge --stamp --json '{"uuid":"<uuid>","client_session_id":"<client_session_id>","schema_version":2}'`.
Keep `--json`: without it, the helper refuses new slots and only bumps
`updated_at` on existing ones. Never cache `continuity_token`.

For a local audit, run `python3 scripts/audit_identity_contract.py --workspace "$PWD"`.
`error` means a cache contract violation; `warning` flags weak resolution,
legacy flat caches, floor observations, or delivery failure for the operator.

Give a short interpreted summary; dump raw JSON only if explicitly requested.
