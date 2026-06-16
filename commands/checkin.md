---
description: "Manual UNITARES governance check-in for the current turn"
---

Before calling tools, check for slot-scoped session caches in the current workspace.

Use the shared helper in this plugin repo:

- `scripts/session_cache.py list --workspace "$PWD"` — slot inventory sorted newest-first; pick the entry whose `slot` matches your current `client_session_id`, or the newest entry if you do not have one yet
- `scripts/session_cache.py get session --slot=<slot>` — read that specific cache

Bare `get session` (no `--slot`) returns the legacy flat `session.json`, which under S20 is read-only-legacy — do not write back to it and do not assume it corresponds to this process-instance.

If a matching cache exists:

- use `uuid` as the expected local identity anchor, not proof by itself
- rely on the active session binding or `client_session_id` for ordinary check-ins
- do not pass `continuity_token` to `process_agent_update`; it is reserved for explicit PATH 0 ownership rebinds

If current binding is unclear, call `identity()` first to inspect the active binding.

If you must rebind to a cached UUID, include the matching `continuity_token`: `identity(agent_uuid=<uuid>, continuity_token=<token>, resume=true)`.

If this is a fresh process and no ownership proof is available, use `/governance-start` to mint a fresh identity with `parent_agent_id=<cached uuid>` rather than bare UUID resume.

If no local continuity state exists and the current identity is unclear, use `/governance-start` first.

Call `process_agent_update` for the current agent once per assistant turn to establish a behavioral baseline. Also call it after meaningful milestones, before/after high-risk work, or when uncertainty/drift shows up.

Inputs:

- `response_text`: concise summary of what was actually accomplished
- `complexity`: estimate `0.0-1.0`
- `confidence`: honest estimate `0.0-1.0`
- use the active session binding or `client_session_id`; do not auto-inject `continuity_token`
- use `response_mode="mirror"` by default for Codex

Guidelines:

- Do not check in after every trivial edit or tool call.
- Prefer one baseline check-in per assistant turn.
- Add a check-in for meaningful milestones, completed steps, or decision points.
- If recent local edit context exists, use it to improve the summary, but do not report raw file churn as if it were real progress.
- If deterministic results already happened in the workflow, mention them concretely instead of speaking in generalities.

After the call:

- report the verdict
- report identity-assurance or continuity warnings when they are surfaced
- report margin or edge warnings when present
- report `memory_suggestions` when present and relevant to the next step
- report `recovery_hint` when present before proposing an invented recovery path
- report any guidance briefly
- report the mirror question when present
- if verdict is `pause` or `reject`, recommend `dialectic(action="request")`
- if verdict is `guide`, summarize the guidance and adjust behavior

For Codex, there is no client hook in this plugin that automatically stamps the
local cache after `process_agent_update`. When a check-in succeeds and you are
maintaining slot-scoped continuity, update the current slot explicitly with
`scripts/session_cache.py set session --slot=<client_session_id> --merge --stamp`
and then reset any local milestone accumulator with
`scripts/session_cache.py reset-milestone`.

Claude adapters may do this through hooks, but Codex should treat it as an
explicit step unless a Codex-native lifecycle hook is added later.
