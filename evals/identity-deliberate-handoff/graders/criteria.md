---
type: llm
weight: 3
---

The response calls `start_session(force_new=true, ...)` (or raw
`onboard(force_new=true, ...)`) with `parent_agent_id` set to
`9b1f0c2e-5d4a-4e61-8c3b-2a7f6e9d1c05` and `spawn_reason="explicit"`.

Fail if `spawn_reason` is `"new_session"`, `"subagent"`, missing, or anything
other than `"explicit"`; if `force_new` is absent or false; or if it tries to
resume the old identity with `identity(agent_uuid=..., resume=true)` or a
continuity token instead of minting a new identity with declared lineage.
