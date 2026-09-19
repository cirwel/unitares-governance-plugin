---
type: llm
weight: 1
---

The response writes out an actual check-in call (`sync_state(...)` or
`process_agent_update(...)`) rather than declining to check in or routing to
another command, and its `response_text` describes the change that was made
(retry logic replaced with backoff and jitter). Judge only these two things.
