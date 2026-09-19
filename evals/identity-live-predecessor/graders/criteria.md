---
type: llm
weight: 3
---

The response calls `start_session(force_new=true)` (or raw
`onboard(force_new=true)`) WITHOUT `parent_agent_id`.

The other session is still running, so it is a concurrent sibling, not a
predecessor: declaring it as a parent for succession is rejected
(`lineage_coincidental_rejected`), and this process was not dispatched by it,
so `spawn_reason="subagent"` would be false too.

Fail if the response passes `parent_agent_id` with any `spawn_reason`, or
resumes the other session's identity.
