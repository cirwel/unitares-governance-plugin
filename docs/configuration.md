# Configuration

Start with the [installation guide](../README.md#install). This reference covers
remote server connections and the plugin environment settings.

## Server connections

For Claude Code on a different host or port, set `UNITARES_SERVER_URL` to the
**base** URL (no `/mcp/` suffix; the plugin appends it):

```bash
export UNITARES_SERVER_URL=https://gov.example.org   # plugin uses .../mcp/
export UNITARES_HTTP_API_TOKEN='replace-with-client-token'
```

Claude's bundled transport is deliberately unauthenticated so a bearer can
never be attached to an operator-supplied cleartext URL. For an authenticated
deployment, disable the bundled transport and register the HTTPS endpoint with
Claude using `UNITARES_HTTP_API_TOKEN`. Governance REST helpers reject a bearer
on non-loopback HTTP before opening a socket.

Use `https://` or a loopback address. When `UNITARES_SERVER_URL` is cleartext
HTTP to any other host, the hooks send no governance data to it, and the
PreToolUse hook denies any call to the bundled Claude server whose input names
`client_session_id`, `continuity_token`, `agent_uuid` or `agent_id`, whether the
hook would add the field or the agent supplied it. The hook walks the whole
input: every key, value and list item, and every string that starts with
`{`, `[` or `"`, decoded with duplicate keys kept and walked again, so an
escaped key name is compared after decoding. A name in plain text counts too,
unless a letter, digit or underscore directly precedes it, as in
`parent_agent_id`: a lineage reference alone is not proof. The Claude hook manifest routes every tool on
the bundled server to that check, not only the named governance tools, and the
hook recognizes the server by its tool-name prefix. An input too large for the
scan budget, a string starting with `{`, `[` or `"` that the JSON decoder
rejects for any reason, and a payload the hook cannot read are denied as if
they carried proof.

Loopback means what Claude's URL parser (the WHATWG URL Standard) resolves to
a loopback address, so IPv4 shorthand such as `http://127.1:8767` counts.
Hosts with percent-escapes or non-ASCII characters do not count, and a URL
with a backslash, userinfo, whitespace or control characters counts as
insecure, because parsers can disagree about its host. The Python REST helpers
accept only `localhost` and literal loopback addresses. Calls that never name
those fields still pass. The check covers those field names, not a proof
value pasted into free text without one, and like the other hooks it fails
open if the hook itself cannot run.

The bundled Codex `.codex-mcp.json` is deliberately fixed to unauthenticated
`http://localhost:8767/mcp/` and does not declare a bearer-token environment
variable. For a hosted or authenticated Codex server, disable that bundled
transport and register a separate server under the exact
`unitares-governance` alias:

```toml
# ~/.codex/config.toml
[plugins."unitares-governance@unitares-governance".mcp_servers.unitares-governance]
enabled = false
```

```bash
export UNITARES_SERVER_URL=https://gov.example.org
export UNITARES_HTTP_API_TOKEN='replace-with-client-token'
codex mcp add unitares-governance \
  --url "${UNITARES_SERVER_URL%/}/mcp/" \
  --bearer-token-env-var UNITARES_HTTP_API_TOKEN
```

Codex hooks trust only the bare `unitares-governance` server alias. Claude's
plugin-scoped alias is host-specific and is not accepted at the Codex hook
boundary.

## Environment variables

Explicit environment values take precedence over plugin defaults,
including `0` and `off` kill switches:

| Variable | Default | Description |
|----------|---------|-------------|
| `UNITARES_SERVER_URL` | `http://localhost:8767` | Governance server base URL |
| `UNITARES_HTTP_API_TOKEN` | unset | Client bearer token for governance REST calls and separately registered authenticated MCP transports; hosted deployments must use HTTPS and one token accepted by the server |
| `UNITARES_AGENT_PREFIX` | host-specific | Prefix for generated client-side names (`claude` or `codex` unless overridden) |
| `UNITARES_CHECKINS` | `on` | `off` suppresses plugin-emitted check-ins and the host-observation bridge; manual MCP calls remain available |
| `UNITARES_AUTO_ONBOARD` | `auto` | Whether the hooks ask the server for a slot-scoped identity when the agent has not onboarded. `auto` asks a server on this machine or one reached over HTTPS; `on` asks whatever server is configured; `anchored` asks only in the process that owns an orchestrated thread anchor ([orchestrators](./orchestrators.md)); `off` never asks and leaves un-onboarded sessions as identity-free floor observations. The request is marked `harness_request`, and a server whose operator set `UNITARES_HARNESS_ONBOARDING=agent_only` declines it. A server older than interface contract 2.5.0 has no such policy and accepts |
| `UNITARES_BINDING_IDLE_HOURS` | `24` | A slot binding with no bound turn end for longer than this is treated as an earlier process's: the host resumed the session without SessionStart (Codex often does), so the pre-governance-call guard no longer refuses this process's `force_new` mint, and the next Stop marks the cache `prior_process`. Match it to the server's `SESSION_TTL_HOURS`, after which an idle binding has expired anyway |
| `UNITARES_CONTINUITY_PROBE` | `off` | `on` asks the agent, after each recorded context compaction, whether it is the same running process, a new one, or cannot tell. The question goes ahead of the compaction guidance; the agent answers with one `continuity-probe:` line at the end of that turn, which adds a visible line to its reply. The Stop hook records only the answer's value (or `no_answer`) beside the compaction, on the identity the cache then holds. `UNITARES_CHECKINS=off` also turns it off |
| `UNITARES_EAGER_BIND` | `on` | When the hooks ask for an identity (see `UNITARES_AUTO_ONBOARD`), SessionStart mints that identity at startup instead of at the first Stop and names its `agent_uuid` and `client_session_id` in the session context, so the agent does not mint a second one. Skipped for orchestrated anchors, spawned children with a declared parent candidate, and slots that already have a cache. `off` restores the lazy Stop-only path |
| `UNITARES_EAGER_BIND_TIMEOUT` | `5` | Seconds the SessionStart mint may take before it gives up silently (the Stop hook's lazy path stays as the fallback) |
| `UNITARES_SESSION_CACHE` | set by SessionStart (Claude) | Exported through `CLAUDE_ENV_FILE`: the absolute path of this session's slot cache. `unitares-github` reads the `uuid` there when it stamps the PR ownership footer (`UNITARES agent: <uuid>`), so a later mint is followed and a child session does not stamp its parent. Not a credential |
| `UNITARES_HOOK_FUNDAMENTALS_EXCERPT` | `off` | SessionStart points both hosts at the `governance-fundamentals` and `governance-lifecycle` skills; `on` inlines the ~4 KB Fundamentals excerpt instead, for a host build that does not load plugin skills |
| `UNITARES_FILE_LEASES_ENABLED` | `1` | Enable host edit leases (Claude Edit/Write/MultiEdit; Codex apply_patch) |
| `UNITARES_FILE_LEASES_REQUIRED` | `0` | Block edits when lease infrastructure is missing/unreachable, or when an edit target's git repository/worktree namespace cannot be verified (a target outside every git repository still proceeds); truthy values take precedence over `UNITARES_FILE_LEASES_ENABLED=0` |
| `UNITARES_ALLOW_INSECURE_LOOPBACK_LEASE_HTTP` | `0` | Development-only opt-in to send a configured lease bearer over cleartext loopback HTTP; use HTTPS or a verified Unix domain socket (`UNITARES_LEASE_PLANE_UNIX_SOCKET` / `unix:///path`) for enforced deployments |
| `UNITARES_LEASE_PLANE_UNIX_SOCKET` | unset | Absolute path to an owner-only (`0600`) Unix domain socket for the BEAM lease plane (takes precedence over `LEASE_PLANE_BASE_URL` / `UNITARES_LEASE_PLANE_URL`). Validated fail-closed (`os.lstat`: absolute non-symlink `S_ISSOCK`, current UID, `st_mode & 0o077 == 0`, parent directory not group/world-writable without sticky bit, and post-connect peer UID verification via `LOCAL_PEERCRED` / `SO_PEERCRED`); treated as secure transport like HTTPS so configured bearers work without `UNITARES_ALLOW_INSECURE_LOOPBACK_LEASE_HTTP=1` |
| `UNITARES_FILE_LEASE_TTL_S` | `30` | Crash/failure backstop for a lease not released by PostToolUse |
| `UNITARES_FILE_LEASE_BATCH_TIMEOUT_S` | `3.5` | Total bounded lease work per edit hook (clamped to four seconds) |
| `UNITARES_MILESTONE_LOCK_TIMEOUT_S` | `2.0` | Maximum wait for the cross-process milestone lock (clamped to four seconds) |
| `UNITARES_SESSION_CACHE_LOCK_TIMEOUT_S` | `2.0` | Maximum wait for a slot-scoped session-cache transaction (clamped to four seconds) |
| `UNITARES_AUTO_CHECKIN_CLAIM_TTL_S` | `30` | Crash-recovery expiry for Claude's single in-flight edit check-in claim (clamped to 30-120 seconds) |
| `UNITARES_WATCHER_ENABLED` | `0` | Opt in to Watcher edit scanning and lifecycle surfacing; workspace-local executables are never auto-run |
| `UNITARES_WATCHER_AGENT` | unset | Explicit trusted path to Watcher's `agent.py`, used for SessionStart backlog and audience-scoped prompt chimes |
| `UNITARES_WATCHER_HOOK` | unset | Explicit trusted executable receiving one normalized single-file Edit event per changed path |
| `UNITARES_CODEX_LIVENESS` | `on` | Record local, slot-scoped completed-tool receipts; these are not check-ins or agent runtime |
| `UNITARES_CODEX_RUNTIME_OBSERVATIONS` | `on` | Emit bounded completed-tool rollups to the legacy runtime-named audit sink |
| `UNITARES_CODEX_HOST_HEARTBEATS` | `off` | Opt in to hook-parent PID heartbeats; a shared host PID never proves per-agent runtime |
| `UNITARES_CODEX_RUNTIME_IDLE_EXIT_S` | `3600` | Stop a detached slot worker after this many seconds without a completed-tool receipt |
| `LEASE_PLANE_BASE_URL` | `http://127.0.0.1:8788` | BEAM lease-plane HTTP base URL (or `unix:///absolute/path` for local UDS; `UNITARES_LEASE_PLANE_URL` is accepted as an alias, and `UNITARES_LEASE_PLANE_UNIX_SOCKET` takes precedence when set) |
| `LEASE_PLANE_BEARER_TOKEN` | unset | Bearer used for lease-plane acquire, heartbeat, and release calls. Read from the environment, then `UNITARES_SECRETS_ENV`, `~/.config/unitares/secrets.env`, or the older `~/.config/cirwel/secrets.env`. When none is set and the lease plane is on loopback, the hook tries the UNITARES Docker Compose default (`unitares-local-lease-plane`), except in required mode. SessionStart says when leases are enabled but would not work |
| `UNITARES_SIDECAR_SOCKET` | unset | Optional absolute path to an owner-only (`0600`) Unix domain socket for `scripts/identity_sidecar.py` (`--unix-socket`), `scripts/u-run` (`--sidecar-socket`), and `scripts/sidecar_client.py`; pre-existing parent directories must already be `0700` owner-only (never silently `chmod`ed), providing full local isolation instead of loopback TCP |
| `UNITARES_SIDECAR_CLIENT` | set by `u-run` | Exported to `u-run` child processes: absolute path to `scripts/sidecar_client.py`, which performs the `/startup-proof` HMAC challenge over TCP (`UNITARES_SIDECAR_STARTUP_SECRET`) or validates `UNITARES_SIDECAR_SOCKET` before presenting `UNITARES_SIDECAR_CAPABILITY` |

See [adapter behavior](./adapters.md) for host-specific hooks and
[check-in diagnostics](./check-ins.md) for triggers, logs, and upgrade steps.
