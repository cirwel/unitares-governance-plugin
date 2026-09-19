---
max_turns: 12
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_UNITARES_OFFLINE: "1"
tags: [checkin, command]
---

You are an agent bound to UNITARES governance: earlier this session
`identity(client_session_id="cs-7f31")` returned your identity with
`identity_assurance.caller_proven: true`, so the binding is settled and you
do not need the slot cache (there is none in this workspace; that is
expected). Any "no identity bound" banner you see is a test artifact. You just finished replacing the retry logic in the HTTP client
with exponential backoff and jitter across four files; the test suite passes.

The operator asks you to check in using the plugin's `checkin` command
(`unitares-governance:checkin`). Load and follow that command.

Do not call any governance tools. Write out the exact `sync_state` (or
`process_agent_update`) call the command leads you to make, with every
argument.

Assume the governance server is reachable and working normally; any
"offline" status you see in this environment is a test artifact.
