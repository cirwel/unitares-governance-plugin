# Chat bridges and orchestrators: one identity per conversation

Many tools drive an agent CLI from somewhere else: a Discord, Slack or Telegram
bridge, a job runner, an orchestrator that hands turns to a headless CLI. Most
of them start a new CLI process for every turn. Each new process is a new
UNITARES identity, so a conversation of twenty turns reads as twenty unrelated
agents with one check-in each.

A bridge can give a conversation one identity instead. It does not need a
UNITARES client, a token or any code from this repository beyond one launcher.

## Use the launcher

Install the plugin in the CLI as usual. Then start each turn through
[`scripts/thread-anchor-exec`](../scripts/thread-anchor-exec), passing an id
that is stable for the conversation:

```bash
scripts/thread-anchor-exec <thread-id> -- <cli> [args...]
```

For example, a bridge that runs Claude Code headless for a Discord thread:

```bash
scripts/thread-anchor-exec 1573223283237847113 -- claude -p --resume "$SESSION" "$PROMPT"
```

The thread id may hold letters, digits, `.`, `_` and `-`, up to 128
characters. Use the id your chat platform already gives the thread or channel.
The launcher replaces itself with the CLI, so standard input, output and the
exit status are the CLI's own.

The first turn creates the identity. Every later turn started with the same
thread id resumes it. The agent still onboards and checks in for itself; the
bridge only says which conversation the process belongs to.

## What the launcher sets

The plugin's hooks resume a conversation's identity only when all three of
these are set for the CLI process:

| Variable | Value | Meaning |
|---|---|---|
| `UNITARES_CLIENT_SESSION_ID` | `agent:/thread-<thread-id>` | The conversation. The `agent:/thread-` prefix is required. |
| `UNITARES_ORCHESTRATED` | `1` | The bridge declares that it provisioned the anchor. |
| `UNITARES_ORCHESTRATED_OWNER_PID` | PID of the CLI | The one process the anchor was provisioned for. |

A bridge that cannot run a shell script can set them itself. The owner PID is
the part that is easy to get wrong: it must be the PID of the process that
runs the hooks. The launcher gets it by setting the variable to its own PID and
then calling `exec`, so the CLI takes over that PID. Setting the variable in
the bridge's own environment, or to the bridge's PID, does not work.

If the anchor is not honored, the session-start hook writes this line to
standard error and the turn gets a fresh identity as before:

```text
[UNITARES] ignored unowned or inherited governance thread anchor
```

The usual cause is a CLI command that is a wrapper: a script that starts the
real program as a child. The child runs the hooks and has a different PID.
Point the launcher at the real program.

## Why three variables and not one

An identity names one subject. An anchor that leaked into the wrong process
would put two subjects on one identity, and their work could no longer be told
apart. Each condition closes one way that can happen:

- **The marker.** A thread id left in a shell profile or copied into an
  interactive session is ignored without `UNITARES_ORCHESTRATED`.
- **The owner PID.** Environment variables are inherited. A CLI that the agent
  starts during its turn, such as a reviewer or a sub-agent, inherits the
  anchor but has a different PID, so it is refused and gets its own identity.
- **The server.** With strict identity on, a server refuses a thread anchor
  that arrives without the orchestrated declaration
  (`lineage_declaration_required`) instead of quietly creating an identity.

## Processes the agent starts

A reviewer or sub-agent started from inside an anchored turn is a different
subject. It must not use the conversation's anchor. If it should be recorded
as descended from the turn that started it, clear the anchor and declare the
parent in its environment:

```bash
env -u UNITARES_CLIENT_SESSION_ID -u UNITARES_ORCHESTRATED \
    -u UNITARES_ORCHESTRATED_OWNER_PID \
    UNITARES_PARENT_AGENT_ID="<uuid of the turn's identity>" \
    UNITARES_SPAWN_REASON=subagent \
    <cli> [args...]
```

Without that, the inherited anchor and any inherited lineage are both dropped,
and the child starts as an identity with no declared parent.

## Checking it works

Run two turns with the same thread id and have the agent call
`start_session()` with no arguments in each. The hooks attach the anchor to
that call. `agent_uuid` should be the same in both turns, with
`identity_resolution_outcome` reading `resumed` on the second, and it should
differ for a second thread id. The agent must not pass `force_new=true` in an
anchored turn: that creates a new identity and breaks the continuity.

To check the contract without a CLI, run the canary. The first form checks
only what the plugin sends. The second probes a running server and writes a
short-lived test identity to it:

```bash
python3 scripts/dev/strict_thread_anchor_contract.py --json
python3 scripts/dev/strict_thread_anchor_contract.py --live \
  --server-url "http://127.0.0.1:8767" --json
```

## What this does not do

- It does not share memory or context between turns. That is the CLI's own
  resume feature. This only makes the governance record continuous.
- It does not onboard, check in or report anything on the agent's behalf.
- It does not cover a CLI with no plugin hooks. For those, see the
  [sidecar and `u-run`](./adapters.md#sidecar).
- It does not make two conversations one identity, or carry an identity from
  one thread id to another. A new thread is a new subject.
