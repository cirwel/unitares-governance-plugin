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

Claude's bundled transport expands the token into its `Authorization` header.
Configure that bundled transport through these variables; do not rely on a
same-named manual Claude server overriding the plugin server.

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
| `UNITARES_HTTP_API_TOKEN` | unset | Client bearer token for governance REST calls, Claude's bundled MCP transport, and a separately registered authenticated Codex transport; hosted deployments must use one token accepted by the server |
| `UNITARES_AGENT_PREFIX` | host-specific | Prefix for generated client-side names (`claude` or `codex` unless overridden) |
| `UNITARES_CHECKINS` | `on` | `off` suppresses plugin-emitted check-ins and the host-observation bridge; manual MCP calls remain available |
| `UNITARES_AUTO_ONBOARD` | `off` | Set to `on` to let the host Stop hook create a slot-scoped identity before its first turn summary when the agent has not onboarded; off leaves un-onboarded sessions as identity-free floor observations |
| `UNITARES_EAGER_BIND` | `on` | With `UNITARES_AUTO_ONBOARD=on`, SessionStart mints that identity at startup instead of at the first Stop and names its `agent_uuid` and `client_session_id` in the session context, so the agent does not mint a second one. Skipped for orchestrated anchors, spawned children with a declared parent candidate, and slots that already have a cache. On Claude Code it also writes `export UNITARES_AGENT_UUID=<uuid>` to `CLAUDE_ENV_FILE`, which `unitares-github` uses to stamp the PR ownership footer. `off` restores the lazy Stop-only path |
| `UNITARES_EAGER_BIND_TIMEOUT` | `5` | Seconds the SessionStart mint may take before it gives up silently (the Stop hook's lazy path stays as the fallback) |
| `UNITARES_HOOK_FUNDAMENTALS_EXCERPT` | `off` | SessionStart points both hosts at the `governance-fundamentals` and `governance-lifecycle` skills; `on` inlines the ~4 KB Fundamentals excerpt instead, for a host build that does not load plugin skills |
| `UNITARES_FILE_LEASES_ENABLED` | `1` | Enable host edit leases (Claude Edit/Write/MultiEdit; Codex apply_patch) |
| `UNITARES_FILE_LEASES_REQUIRED` | `0` | Block edits when lease infrastructure is missing/unreachable; truthy values take precedence over `UNITARES_FILE_LEASES_ENABLED=0` |
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
| `LEASE_PLANE_BASE_URL` | `http://127.0.0.1:8788` | BEAM lease-plane HTTP base URL |
| `LEASE_PLANE_BEARER_TOKEN` | unset | Bearer used for lease-plane acquire, heartbeat, and release calls. Read from the environment, then `UNITARES_SECRETS_ENV`, `~/.config/unitares/secrets.env`, or the older `~/.config/cirwel/secrets.env`. When none is set and the lease plane is on loopback, the hook tries the UNITARES Docker Compose default (`unitares-local-lease-plane`), except in required mode. SessionStart says when leases are enabled but would not work |

See [adapter behavior](./adapters.md) for host-specific hooks and
[check-in diagnostics](./check-ins.md) for triggers, logs, and upgrade steps.
