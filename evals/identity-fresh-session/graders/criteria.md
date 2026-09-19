---
type: llm
weight: 3
---

The response names `start_session(force_new=true)` (or the raw equivalent
`onboard(force_new=true)`) as the call to make.

It does NOT pass `parent_agent_id` or a `spawn_reason`: sharing a workspace
with an earlier process is co-location, not lineage, and the prior process
may still be running.

Fail the response if it declares the prior uuid as a parent, resumes it with
`identity(agent_uuid=...)`, or calls `identity()` / `onboard()` without
`force_new=true`.
