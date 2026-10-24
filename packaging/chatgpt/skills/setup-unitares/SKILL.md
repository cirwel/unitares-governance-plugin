---
name: setup-unitares
description: Verify a newly installed UNITARES connection and establish the current ChatGPT conversation's explicit governance workflow. Use immediately after installing the UNITARES Governance plugin; do not use for routine check-ins.
---

# Set up UNITARES

Verify the connection with `health_check`. If it fails, report the returned
connection or authentication error and stop; never ask the user to paste a
credential into the conversation.

For a genuinely fresh conversation process, call
`start_session(force_new=true)` once and retain the returned
`client_session_id` for later UNITARES calls in this conversation. Do not
declare a parent unless the user is deliberately handing off work from an
exited predecessor.

Report the new agent UUID and whether the server requested any recovery action.
Explain that this portable web package has no local lifecycle hooks:

- call `sync_state` only at a meaningful milestone or material change;
- use `search_shared_memory` before recording a new finding;
- use `record_result` when a real outcome lands;
- begin a later fresh conversation with its own `start_session` call.

Do not create a check-in merely to test that setup succeeded. A successful
health read and session start are sufficient.
