---
max_turns: 12
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_UNITARES_OFFLINE: "1"
tags: [identity]
---

You are writing a small Python client for the UNITARES governance MCP server.
Every tool returns a JSON object. Write a function
`call_went_through(resp: dict) -> bool` that the client uses after every
governance tool call to decide whether the call actually took effect (so it
can retry or re-bind otherwise), plus a two-line note on what the client
should do when it returns False.

Use the plugin's documentation if it helps. Do not call any governance tools.
