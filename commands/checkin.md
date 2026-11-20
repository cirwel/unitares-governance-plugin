---
description: "Manual UNITARES governance check-in with an agent-authored judgment"
---

Use only the binding already attached to this live process. Do not scan
workspace cache inventory, pick the newest slot, or copy a
`client_session_id` from another cache: workspace files are lineage hints, not
identity proof. A host hook may read the protected HOME binding for its exact
current session slot; an interactive command must not guess that slot.

If the current process already has a binding:

- use `uuid` as the expected local identity anchor, not proof by itself
- rely on the active session binding or `client_session_id` for ordinary check-ins
- do not pass `continuity_token` to `process_agent_update`; it is reserved for explicit PATH 0 ownership rebinds

If the current binding is unclear or no exact current-process
`client_session_id` is available, use `/governance-start`; do not adopt one
from cache inventory. When a live proof is available, call
`identity(client_session_id=<your client_session_id>)` to inspect the active
binding. Never call `identity()` with no arguments: a call carrying no proof
argument is gated to a fresh mint, so it reports on a newly created identity
instead of yours. Trust the answer only when
`identity_assurance.caller_proven` is true.

If you must rebind to a cached UUID, include a matching live `continuity_token`
from the same process response: `identity(agent_uuid=<uuid>,
continuity_token=<token>, resume=true)`. Never take a `continuity_token` from
any cache file, whether a legacy v1/flat `session.json` or a v2 slot cache:
only a token returned to this live process counts as proof. v2 slot caches do
not persist it, and a token found in a legacy file is not a credential.

If this is a fresh process and no ownership proof is available, use `/governance-start` to mint a fresh identity rather than bare UUID resume. A cached uuid is not a parent by default; `/governance-start` explains when a handoff justifies declaring it.

If no local continuity state exists and the current identity is unclear, use `/governance-start` first.

Call `process_agent_update` once for this explicit `/checkin`. Outside an
explicit request, check in only at a meaningful milestone when you also hold a
judgment the substrate cannot infer: a self-correction, revised belief, material
change in uncertainty or complexity, or handoff assessment after substantive
work. A boundary before or after high-risk work qualifies only when it meets
both conditions. The hooks already record per-turn substrate observations; do
not manufacture an agent report to echo them.

Inputs:

- `response_text`: concise, secret-free statement of your judgment and the relevant work
- `complexity`: estimate `0.0-1.0`
- `confidence`: **optional, usually omit it.** Any value mints a tactical prediction that is scored into the fleet calibration curve, so pass one only when you are stating a genuine belief about how your work will hold up, never a habitual number
- use the active session binding or `client_session_id`; do not auto-inject `continuity_token`
- use the compact response shape for routine Codex check-ins: leave `response_mode` at its schema default `auto`, which resolves to compact for steady states and switches to mirror by itself for guide/pause or at-risk states, a degraded identity binding, or a response carrying warnings; or pass `response_mode="compact"` to pin it. Pass `response_mode="mirror"` explicitly for drift or diagnostic review

Guidelines:

- There is no fixed time-, turn-, or tool-count cadence. Completion, generated
  output, a reminder, or task exit alone does not call for a check-in.
- One check-in may cover a coherent unit of work; a milestone or decision point
  also needs an agent-held judgment the substrate cannot infer.
- If recent local edit context exists, use it to improve the summary, but do not report raw file churn as if it were real progress.
- If deterministic results already happened in the workflow, mention them concretely instead of speaking in generalities.

After the call:

- report checkpoint acceptance, assessment status, and any errors or degradation
  separately. A keyed replay acknowledges the original checkpoint without a new assessment;
  it supplies no current verdict. Use `check_working_state` if a current state
  read is needed rather than submitting another check-in to obtain one. For
  partial failures or unconfirmed acceptance, follow the `governance-lifecycle`
  skill's receipt and retry guidance
- when an assessment is returned, distinguish the raw recommendation in
  `policy_evaluation`, the effective action, and `enforcement.applied` when
  those fields are present. The default `observe` posture leaves inferred
  pauses as guidance; it does not apply a lifecycle hold
- report identity-assurance or continuity warnings when they are surfaced
- report margin or edge warnings when present
- report `memory_suggestions` when present and relevant to the next step
- report `recovery_hint` when present before proposing an invented recovery path
- report any guidance briefly
- report `mirror` signals and any `reflection` when present
- if an actual lifecycle hold is reported, stop gated work and follow its
  surfaced recovery path. If no path is supplied, inspect `check_working_state`
  and the `governance-lifecycle` skill; seek structured review or operator input
  for an unresolved hold. It remains authoritative until a supported recovery,
  review, operator, or expiry path lifts it
- if the effective action is `pause` or `reject` but an applied hold is unclear,
  inspect enforcement and the current `write_gate` before choosing the next
  step. An inferred pause projected into guidance under `observe` does not
  require dialectic recovery; review can still help resolve a disagreement
- for `guide` or a surfaced warning, summarize relevant input changes when
  provided, relate them to task evidence, and explain any justified adjustment.
  Label unavailable inputs as unknown. A favorable metric change alone does
  not establish better work or authorize bypassing an existing hold

EISV currently runs synchronously on successful fresh check-ins. Its planned
module boundary does not make assessment optional at runtime; `observe`
controls actuation, not estimation. The server's
[computation reference](https://github.com/cirwel/unitares/blob/master/docs/EISV_COMPUTATION.md)
owns the instrument's inputs, interpretations, and evidence limits.

Do not stamp the cache or reset a milestone by hand after the call. Claude and
Codex both register the `pre-governance-call` hook, which snapshots the current
host slot's cache and milestone before the check-in, and the `post-checkin`
hook, which acts on that snapshot afterwards. `post-checkin` stamps the slot
only when all of these hold:

- the server reported the check-in as successful;
- the slot holds a protected binding for this process, and the call named that
  binding's `client_session_id` or none (a call that named none is credited to
  the slot's binding);
- the snapshot for this same call exists, and the slot's cache was not written
  again while the call was in flight.

After stamping, it resets the slot's milestone only if the milestone's revision
still equals the one in the snapshot. Every recorded edit advances that
revision, so an edit that lands while the call is in flight keeps the milestone
pending for the next check-in. Without lifecycle hooks there is no milestone
accumulator to reset.
