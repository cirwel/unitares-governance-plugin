# Chat bridges and orchestrators: one identity per conversation

Many tools drive an agent CLI from somewhere else: a Discord, Slack or Telegram
bridge, a job runner, an orchestrator that hands turns to a headless CLI. Most
of them start a new CLI process for every turn. Each new process is a new
UNITARES identity, so a conversation of twenty turns reads as twenty unrelated
agents.

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

The thread id may hold ASCII letters, digits, `.`, `_` and `-`, with no two
dots in a row, up to 128 characters. That is deliberately narrower than what
the server accepts: every id the launcher takes reaches the server unchanged,
so two different ids can never be rewritten into the same one.

Choose the id of the conversation you mean to bind, and make it unique on the
server. A Slack thread timestamp or a Discord thread id is the conversation; a
channel id is not, if the channel holds several threads. Two bridges that
report to one server must not hand out the same id, so prefix it when that can
happen, for example `slack-T024BE7LD-1732062000.004200`.

The launcher replaces itself with the CLI, so standard input, output and the
exit status are the CLI's own. A CLI that is not found exits 127, and one that
exists but cannot be run exits 126.

### What binds the identity

The launcher only says which conversation the process belongs to. It does not
create or bind an identity. The agent does that by calling `start_session()`
with no arguments, which the session-start hook tells it to do before the
first tool that needs an identity. The first such call for a thread id creates
the identity, and the same call in a later turn resumes it.

By default nothing else binds it: a conversation in which the agent never
makes that call has no identity. Set `UNITARES_AUTO_ONBOARD=on` for the CLI to
have the hooks attempt the bind themselves when a turn is still unbound. That
needs check-ins enabled and a session id from the host; see the
[configuration reference](./configuration.md).

## What the launcher sets

The plugin's hooks resume a conversation's identity only when all three of
these are set for the CLI process:

| Variable | Value | Meaning |
|---|---|---|
| `UNITARES_CLIENT_SESSION_ID` | `agent:/thread-<thread-id>` | The conversation. Keep the `agent:/thread-` prefix: the server recognizes an orchestrated first bind by it. |
| `UNITARES_ORCHESTRATED` | `1` | The bridge declares that it provisioned the anchor. |
| `UNITARES_ORCHESTRATED_OWNER_PID` | PID of the CLI | The one process the anchor was provisioned for. |

A bridge that cannot run a shell script can set them itself. The owner PID is
the part that is easy to get wrong: it must be the PID of the process that
runs the hooks. The launcher gets it by setting the variable to its own PID and
then calling `exec`, so the CLI takes over that PID. Setting the variable in
the bridge's own environment, or to the bridge's PID, does not work.

When the owner PID is missing or is not the process running the hooks, the
anchor is dropped and the session-start hook writes this line to standard
error:

```text
[UNITARES] ignored unowned or inherited governance thread anchor
```

The usual cause is a CLI command that is a wrapper: a script that starts the
real program as a child. The child runs the hooks and has a different PID.
Point the launcher at the real program.

One case is silent: an anchor whose owner PID is right but which lacks the
`UNITARES_ORCHESTRATED` marker is ignored without a message. In every case the
turn is then treated as an ordinary session with no anchor.

## Why three variables and not one

An identity names one subject. An anchor that leaked into the wrong process
would put two subjects on one identity, and their work could no longer be told
apart. Each condition closes one way that can happen:

- **The marker.** An anchor counts only when the bridge also declares
  `UNITARES_ORCHESTRATED`. A thread id left in a shell profile or copied into
  an interactive session has neither the marker nor an owner, and is ignored.
- **The owner PID.** Environment variables are inherited. A CLI that the agent
  starts during its turn, such as a reviewer or a sub-agent, inherits the
  anchor but has a different PID, so it is refused and gets its own identity.
- **The server.** With strict identity on, a thread anchor that matches no
  existing identity and arrives without the orchestrated declaration is
  refused (`lineage_declaration_required`) instead of quietly creating one.

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

Those two variables are a suggestion to the child, not a record. The child's
session-start hook shows it the parent and the reason, and the lineage is
recorded only when the child passes them in its own call:
`start_session(force_new=true, parent_agent_id="<uuid>", spawn_reason="subagent")`.

Without the `env` line above, the inherited anchor and the inherited lineage
variables are both dropped, so the spawner's declaration is lost. The child's
session-start hook may still suggest a parent from identities cached in the
same workspace; that is a guess from local history, not the spawner's word.

## Checking it works

Run two turns with the same thread id and have the agent call
`start_session()` with no arguments in each. The hooks attach the anchor to
that call. Compare the UUID in the two responses (`agent_uuid`, or `uuid`
from `onboard`): it should be the same, with `is_new` false on the second, and
it should differ for a second thread id. The agent must not pass
`force_new=true` in an anchored turn: that creates a new identity and breaks
the continuity.

To check the server side without a CLI, run the canary. The first form checks
only the request the plugin's onboarding helper builds; it does not exercise
the launcher or the hooks. The second probes a running server, which must have
strict identity on (the canary first expects a bare anchor to be refused). It
creates a test identity there and does not remove it:

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
