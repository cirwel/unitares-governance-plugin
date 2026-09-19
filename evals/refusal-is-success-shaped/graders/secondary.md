---
type: llm
weight: 1
---

The note says to follow `next_step` / `safe_options` (bind with
`start_session(force_new=true)`) rather than retrying the same call, and does
not blindly retry the same call.
