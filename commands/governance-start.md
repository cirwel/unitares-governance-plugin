---
description: "Create or declare lineage for a UNITARES session in Codex (fresh process-instance posture)"
---

Under the UNITARES identity ontology v2, a fresh process-instance mints fresh governance-identity. Lineage is declared via `parent_agent_id`, not resumed via token. This command starts a session in that posture.

Start by surfacing prior workspace state as a **lineage candidate** (not a resume credential).

Use the shared helper:

- `scripts/session_cache.py list session` — slot inventory sorted by recency (when available; S20.1)
- otherwise glob `.unitares/session-*.json` and pick the most recent by `updated_at`

If a candidate is found:

- read it
- treat `uuid` as a lineage candidate (the predecessor this fresh process declares it inherits from)
- ignore any `continuity_token` field on disk — pre-S11 caches may carry one; v2 caches do not, and either way the helper does not use it for resume

Then call UNITARES using the strongest honest posture for the situation:

- fresh process with no prior workspace state → `onboard(force_new=true)`
- fresh process inheriting prior workspace state → `onboard(force_new=true, parent_agent_id=<cached uuid>, spawn_reason="new_session")`
- include `model_type` when the current runtime is clear from context
- do not invent a display name unless the user asked for one

Do not use `identity(agent_uuid=<uuid>, ...)` from a fresh process. Bare-UUID resume is an unsigned claim and is hijack-shaped under strict identity mode. Cross-process-instance token resume is also out of posture; the S1-a deprecation path only exists for testing.

After a successful response:

- record the session in slot-scoped form using the just-returned `client_session_id` as the slot:
  - `scripts/session_cache.py set session --slot=<client_session_id> --merge --stamp`
- the helper rejects slotless writes at v2 (S20.1) — passing `--slot` is required
- include in the JSON payload:
  - `server_url` when known
  - `uuid`
  - `agent_id`
  - `display_name`
  - `client_session_id`
  - `parent_agent_id` when lineage was declared
  - `session_resolution_source`
  - `continuity_token_supported` (the boolean flag, not the token)
  - `updated_at`
- do **not** persist `continuity_token` itself. The helper rejects payloads containing it at v2 (S20.1). v2 cache schema is lineage-only.

When reporting back:

- say whether the identity was freshly created or created with declared lineage
- if lineage was declared, name the parent UUID prefix
- show the resolved display name or agent id
- mention the next useful command:
  - `/checkin` after meaningful work
  - `/diagnose` if something looks wrong

Do not dump raw JSON unless the user asks for it.
Prefer a short interpreted summary.
