---
description: "Create or declare lineage for this UNITARES session (fresh process-instance posture)"
---

Under the UNITARES identity ontology v2, a fresh process-instance mints fresh governance-identity. Lineage is declared via `parent_agent_id`, not resumed via token. This command starts a session in that posture.

Start by surfacing prior workspace state as a **lineage candidate** (not a resume credential).

Use the shared helper:

- `${CLAUDE_PLUGIN_ROOT}/scripts/session_cache.py list --workspace "$PWD"` — slot inventory sorted by recency (S20.1)

If the helper is unavailable, start fresh without a candidate. Recency of a
workspace file is not lineage, and a cached `client_session_id` is never yours
to reuse.

If a candidate is found:

- read it
- treat `uuid` as a lineage candidate (the predecessor this fresh process declares it inherits from)
- ignore any `continuity_token` field on disk — pre-S11 caches may carry one; v2 caches do not, and either way the helper does not use it for resume

First check whether **this process** is already bound. It is when this
conversation already shows it: SessionStart context saying "this claude process
is already bound to governance" with an `agent_uuid`, an earlier
`start_session`/`onboard` call in this conversation that returned one, or a
pre-governance-call refusal saying "This process is already bound". A slot
cache in the workspace is not that evidence: it may be another process's.

If this process is already bound, do not mint again. A second
`force_new=true` mint gives one process two identities that lineage can never
join, and the hook refuses it unless the call declares lineage or a deliberate
`spawn_reason`. Call `check_working_state()` with the bound
`client_session_id` (the hook adds it when installed) and
report that identity as the result of this command. Mint again only when the
user explicitly asks for a new identity in this process, and then pass
`spawn_reason="explicit"` as the refusal says, never a bare `force_new=true`.

Otherwise call UNITARES using the strongest honest posture for the situation:

- fresh process → `onboard(force_new=true)`, **whether or not a prior slot cache exists**. A cache left in the workspace shows co-location, not lineage: the process that wrote it may still be running, and you are not its continuation just because you share a directory.
- deliberate handoff: declare the cached uuid only when that process has **exited** and the user (or the task) says you are continuing its work → `onboard(force_new=true, parent_agent_id=<cached uuid>, spawn_reason="explicit")`. Declaring a still-live process is rejected (`lineage_coincidental_rejected`), unless `spawn_reason` marks a dispatched child (`subagent`, `dispatch`, `dialectic_reviewer`) or a `compaction` continuation: those relationships expect the parent to still be live, and the dispatcher usually sets them rather than this command. Do not use `spawn_reason="new_session"`: the server treats it as the legacy co-location reason, not intentional lineage.
- include `model_type` when the current runtime is clear from context
- do not invent a display name unless the user asked for one

`start_session(...)` is the friendly equivalent of `onboard(...)` on servers
that expose the agent-experience aliases, with the same identity rules. On
servers with the envelope enabled it lifts `agent_uuid`, `client_session_id`,
`agent_id`, `display_name`, `is_new`, `identity_resolution_outcome`,
`session_resolution_source` when the payload carries it, a compact
`identity_assurance` (tier, session_source, caller_proven, baseline),
`next_action` and `state_summary`. **Read the uuid from `agent_uuid`.** A plain
fresh mint (`response_shape: "routine"`) does not repeat the canonical payload,
and `continuity_token` rides under `rebind` with its use-only-for caveat; any
other mint (a resume miss, a reactivated identity, a declared lineage, a label
rename, an unrecognised notice), or `response_mode="full"`, also carries the
canonical payload under `raw_governance`. Older compatibility surfaces may
return the canonical payload directly. Treat `memory_suggestions` as retrieval
prompts and `recovery_hint` as the first recovery route when a response reports
degraded state. If the alias is unavailable, call `onboard(...)` directly.

Do not use `identity(agent_uuid=<uuid>, ...)` from a fresh process. Bare-UUID resume is an unsigned claim and is hijack-shaped under strict identity mode. Cross-process-instance token resume is also out of posture; the S1-a deprecation path only exists for testing.

After a successful response:

- do not write the session cache yourself. The `post-identity` hook records
  this process's session under its **host session** slot, the key every other
  hook reads, and stores `client_session_id` inside that record. A write keyed
  by `client_session_id` would add a second entry and leave the slot the hooks
  read unchanged.
- if the hook is not installed, leave the cache alone: you do not know the
  host session id the hooks key by, and a slot written under any other key
  only shows up later as a misleading lineage candidate.
- if a later slot read disagrees with the live identity, report it (`/diagnose`).
  Never edit a cache by hand.
- never persist `continuity_token`, and read `continuity_token_supported` from
  the live response: the v2 cache is lineage context only and holds neither.

When reporting back:

- say whether the identity was freshly created, created with declared lineage,
  or already bound to this process before the command ran
- if lineage was declared, name the parent UUID prefix
- show the resolved display name or agent id
- mention the next useful command:
  - `/checkin` at a meaningful milestone when you also hold a judgment the
    substrate cannot infer; starting a session alone does not call for one
  - `/diagnose` if something looks wrong

Do not dump raw JSON unless the user asks for it.
Prefer a short interpreted summary.
