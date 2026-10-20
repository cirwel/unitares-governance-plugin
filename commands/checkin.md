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

If current binding is unclear, call `identity(client_session_id=<your client_session_id>)` to inspect the active binding. Never call `identity()` with no arguments: a call carrying no proof argument is gated to a fresh mint, so it reports on a newly created identity instead of yours. Trust the answer only when `identity_assurance.caller_proven` is true.

If you must rebind to a cached UUID, include a matching live `continuity_token`
from the same process response: `identity(agent_uuid=<uuid>,
continuity_token=<token>, resume=true)`. Never take a `continuity_token` from
any cache file, whether a legacy v1/flat `session.json` or a v2 slot cache:
only a token returned to this live process counts as proof. v2 slot caches do
not persist it, and a token found in a legacy file is not a credential.

If this is a fresh process and no ownership proof is available, use `/governance-start` to mint a fresh identity rather than bare UUID resume. A cached uuid is not a parent by default; `/governance-start` explains when a handoff justifies declaring it.

If no local continuity state exists and the current identity is unclear, use `/governance-start` first.

Call `process_agent_update` once for this `/checkin`. Outside an explicit `/checkin`, check in after meaningful milestones, before/after high-risk work, or when uncertainty/drift shows up, not every turn: the hooks already record per-turn substrate observations, and an agent report manufactured to match them is the failure the evidence classes exist to prevent.

Inputs:

- `response_text`: concise summary of what was actually accomplished
- `complexity`: estimate `0.0-1.0`
- `confidence`: **optional, usually omit it.** Any value mints a tactical prediction that is scored into the fleet calibration curve, so pass one only when you are stating a genuine belief about how your work will hold up, never a habitual number
- use the active session binding or `client_session_id`; do not auto-inject `continuity_token`
- use the compact response shape for routine Codex check-ins: leave `response_mode` at its schema default `auto`, which resolves to compact for steady states and switches to mirror by itself for guide/pause or at-risk states, a degraded identity binding, or a response carrying warnings; or pass `response_mode="compact"` to pin it. Pass `response_mode="mirror"` explicitly for drift or diagnostic review

Guidelines:

- Do not check in after every trivial edit or tool call.
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
maintaining slot-scoped continuity, update the current slot explicitly,
passing the identity fields as the payload (without `--json` the helper has
nothing to write: it refuses a new slot and only bumps `updated_at` on an
existing one):
`scripts/session_cache.py set session --slot=<client_session_id> --merge --stamp --json '{"uuid":"<uuid>","client_session_id":"<client_session_id>","schema_version":2}'`.
Do not write `continuity_token` to the cache; the helper refuses it. Then reset
any local milestone accumulator with
`scripts/session_cache.py reset-milestone`.

Claude adapters may do this through hooks, but Codex should treat it as an
explicit step unless a Codex-native lifecycle hook is added later.
