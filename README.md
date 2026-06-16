# UNITARES Governance

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Claude Code Plugin](https://img.shields.io/badge/Claude%20Code-plugin-d97757.svg)](https://docs.claude.com/en/docs/claude-code/plugins)
[![Codex Plugin](https://img.shields.io/badge/Codex-plugin-10a37f.svg)](./CODEX_START.md)
[![Version](https://img.shields.io/badge/version-0.4.7-blue.svg)](.claude-plugin/plugin.json)

Client and integration layer for **UNITARES** — the runtime telemetry and coordination layer for heterogeneous AI-agent fleets. This repo provides agent-facing skills, command guidance, and adapters for connecting coding agents (Claude Code, Codex/ChatGPT, others) to a running UNITARES governance server. The runtime itself lives in [`cirwel/unitares`](https://github.com/cirwel/unitares).

## Purpose

This repo is not the governance engine itself. It is the client and integration layer.

Use it to:

- onboard agents into UNITARES
- inspect governance state and operator diagnostics
- request dialectic review
- work with the knowledge graph
- adapt UNITARES workflows to Codex, ChatGPT, Claude, and other clients

## What Lives Elsewhere

- `unitares` contains the runtime, MCP server, storage, health checks, and governance logic
- `unitares-governance` contains the agent-facing plugin and integration layer
- optional bridges like Discord can remain separate integrations

This repo should not duplicate server business logic or become the source of truth for thresholds that already live in the runtime.

## Current Adapters

- Codex/ChatGPT adapter: plugin packaging plus shared skills and explicit command guidance
- Claude adapter: hooks, session helpers, and command docs

The shared value in this repo is the workflow guidance and client integration surface, not a second copy of the governance model.

## Start Here

If you are using ChatGPT or Codex, start with [CODEX_START.md](./CODEX_START.md).

That path is now the preferred default. Claude hook automation remains supported, but it is no longer the canonical mental model for UNITARES usage.

## Core Workflow

The intended workflow is:

1. `start_session(force_new=true)` as your first step in the session, to mint a fresh process identity (`onboard(...)` is the canonical equivalent) — until you onboard, your work this session is invisible to governance, which is the main source of uninitialized, 0-update agents
2. if continuing prior work, pass `parent_agent_id=<prior uuid>` and `spawn_reason="new_session"`
3. call `sync_state()` once per assistant turn as a behavioral baseline, and after meaningful work (`process_agent_update(...)` is the canonical equivalent) — an identity that never checks in produces no governance signal
4. call `get_governance_metrics()` for read-only state
5. use `identity(agent_uuid=..., continuity_token=..., resume=true)` only for same-owner proof-owned rebinds
6. call `identity()` and `health_check()` when diagnosis is needed

On servers with the agent-experience envelope enabled, friendly alias responses
lift `next_action`, `state_summary`, `risk_summary`, `memory_suggestions`, and
`recovery_hint` when present while preserving the canonical payload under
`raw_governance`. Older compatibility surfaces may return the canonical payload
directly. Use `memory_suggestions` as retrieval cues, and prefer
`recovery_hint` before inventing a recovery path.

The principle is simple: prefer regular behavioral baselines over raw activity noise. One real check-in per assistant turn is useful; per-tool or per-edit check-ins are usually not.

## Commands

| Command | Description |
|---------|-------------|
| `/governance-start` | Create or declare lineage for a Codex/ChatGPT UNITARES session |
| `/checkin` | Manual turn-baseline check-in, plus meaningful milestones |
| `/diagnose` | Show current governance state plus identity/health diagnostics when needed |
| `/dialectic` | Request a dialectic review |

## Skills

| Skill | When to Use |
|-------|-------------|
| `unitares-governance:governance-fundamentals` | Understanding EISV, coherence, verdicts, and calibration |
| `unitares-governance:governance-lifecycle` | Onboarding, continuity, check-ins, and recovery |
| `unitares-governance:dialectic-reasoning` | Participating in dialectic sessions |
| `unitares-governance:knowledge-graph` | Searching and contributing to shared memory |
| `unitares-governance:discord-bridge` | Operating the Discord integration |

## Prerequisites

1. A running UNITARES governance server
2. The governance MCP endpoint reachable by the client

This repo is a **client adapter only** — it does not include the governance engine. You need a server running before any of these commands or skills do anything useful.

**Easiest server bring-up — Docker Compose:**

```bash
git clone https://github.com/CIRWEL/unitares.git
cd unitares
docker compose up
# server now at http://localhost:8767/mcp/
```

That single command brings up Postgres+AGE+pgvector, Redis, and the governance server. The Pi/Lumen embodiment side is optional — governance runs standalone. For bare-metal install (Homebrew Postgres, native AGE compile) see [unitares/README.md](https://github.com/CIRWEL/unitares#installation).

Once the server is up, point this plugin's MCP client at it:

```json
{
  "mcpServers": {
    "unitares-governance": {
      "type": "url",
      "url": "http://localhost:8767/mcp/"
    }
  }
}
```

If your server is on a different host or port, set `UNITARES_SERVER_URL` (see Configuration below).

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `UNITARES_SERVER_URL` | `http://localhost:8767` | Governance server base URL |
| `UNITARES_AGENT_PREFIX` | `claude` | Prefix for generated client-side names in Claude hooks |
| `UNITARES_AUTO_ONBOARD` | `on` | Let Claude `post-stop` lazily create a slot-scoped identity before its first turn summary if the agent did not manually onboard |
| `UNITARES_FILE_LEASES_ENABLED` | `1` | Enable Claude Edit/Write/MultiEdit file-lease guard |
| `UNITARES_FILE_LEASES_REQUIRED` | `0` | Block edits when lease infrastructure is missing/unreachable |
| `LEASE_PLANE_BASE_URL` | `http://127.0.0.1:8788` | BEAM lease-plane HTTP base URL |
| `LEASE_PLANE_BEARER_TOKEN` | unset | Bearer used for lease-plane acquire, heartbeat, and release calls |

## Adapter Notes

### Claude

The current Claude adapter includes session-start, pre-edit, post-edit, and session-end hooks. Those hooks should be treated as an adapter convenience, not the canonical governance policy. In particular, frequent file writes should not automatically be interpreted as meaningful governance events.

The pre-edit hook acquires a BEAM file lease before Edit/Write/MultiEdit. Missing lease-plane configuration fails open by default, while real `held_by_other` contention blocks the edit with a visible explanation. Post-edit releases the just-edited file lease immediately; session-end remains a best-effort cleanup path for any lease that survived an interrupted edit.

The `session-start` hook remains read-only: it tells the agent to call `start_session(force_new=true)` before substantive work. If the agent has not onboarded by the end of the turn, `post-stop` uses `scripts/onboard_helper.py` to lazily mint a fresh, slot-scoped identity and then emits the normal `turn_stop` summary under that identity. Set `UNITARES_AUTO_ONBOARD=off` or legacy `UNITARES_DISABLE_AUTO_ONBOARD=1` to fall back to identity-free floor observations for un-onboarded sessions.

### Codex

Codex and ChatGPT support should stay minimal and explicit:

- package shared skills through `.codex-plugin/plugin.json`
- document manual command flows for agents that can use them
- treat `.unitares/session-<slot>.json` as the neutral local continuity cache; flat `.unitares/session.json` is legacy/read-only
- use `scripts/session_cache.py` as the shared cache helper across adapters
- avoid client-specific auto-checkin behavior until there is a Codex-native reason to add it

### Sidecar

For clients without lifecycle hooks, run the local identity sidecar and send
governance REST tool calls through it:

```bash
python3 scripts/identity_sidecar.py \
  --server-url http://localhost:8767 \
  --workspace "$PWD" \
  --slot codex-local \
  --port 8768
```

Phase 1 is a dependency-free REST sidecar, not a full streamable-MCP proxy. It
wraps `/v1/tools/call`, lazily onboards a slot when needed, injects
`client_session_id` into attribution-relevant governance calls, forces
`force_new=true` for bare `onboard` / `start_session`, stamps the slot cache
after check-ins, and exposes `GET /audit`. Useful endpoints:

- `POST http://127.0.0.1:8768/v1/tools/call` with `{"name": "...", "arguments": {...}}`
- `POST http://127.0.0.1:8768/turn/checkin` with `response_text`, `complexity`, and `confidence`
- `POST http://127.0.0.1:8768/turn/stop` for an end-of-turn check-in
- `GET http://127.0.0.1:8768/audit` for local cache/log contract findings

Use `X-UNITARES-Slot` or top-level `{"slot": "..."}` when one sidecar serves
multiple clients. Without an explicit slot, the sidecar uses a workspace-derived
default slot.

## Non-Goals

This repo should not:

- redefine the governance math
- duplicate server-side threshold logic
- auto-checkin every trivial file write by default
- override runtime verdicts locally

## Check-In Triggers

The Claude adapter emits canonical `process_agent_update` calls at three trigger points.
`session-start` is deliberately read-only: it checks server reachability,
fetches the governance fundamentals excerpt, and prompts the agent to call
`start_session(force_new=true)` / `onboard(force_new=true)` itself. If the
agent does not do that before the turn ends, `post-stop` lazily onboards a
slot-scoped identity before emitting `turn_stop`; if that fails, it records
an identity-free floor observation instead.

| Trigger | Hook script | Frequency | `metadata.event` |
|---|---|---|---|
| Claude turn ends | `post-stop` | per Claude turn | `turn_stop` |
| Edit threshold crossed | `post-edit` | every N edits or T seconds | `auto_edit` |
| Session closes | `session-end` | once per session | `session_end` |

All emissions share one shared helper (`scripts/checkin.py`) that:
- Applies secret-pattern redaction to `response_text` before POST
- Truncates `response_text` to 512 chars
- Logs one status line per attempt to `~/.unitares/checkins.log`
- Returns fire-and-forget: never raises, never blocks a Claude turn on failure

### Kill switch

`UNITARES_CHECKINS=off` in the environment suppresses every plugin-emitted
check-in. Disable a single trigger by removing its entry from
`hooks/hooks.json`.

### Diagnosing check-in behavior

```bash
tail -f ~/.unitares/checkins.log
```

Expected line format:
```
2026-04-17T02:45:12Z | slot=abc12345 | event=turn_stop | uuid=86ae619f | status=sent | latency_ms=42
```

Statuses: `sent` (accepted by governance), `fail` (POST failed — see `err=...`), `skip_kill_switch` (suppressed by `UNITARES_CHECKINS=off`), `error` (client-side exception; caller passed garbage values).

### Protective audit

Run the local identity-contract audit when check-ins look wrong, before shipping
adapter changes, or from a lightweight monitor:

```bash
python3 scripts/audit_identity_contract.py --workspace "$PWD"
```

The audit checks slot-scoped session caches and the hook diagnostic log without
contacting the governance server. Hard failures include non-empty
`continuity_token` at rest, unreadable session JSON, and session caches with no
`uuid` or `client_session_id`. Warnings include flat legacy `session.json`, weak
`session_resolution_source` values such as `ip_ua_fingerprint`, and log statuses
like `floor_sent`, `floor_fail`, `fail`, or `error`. Use `--json` for monitor
output and `--fail-on-warning` when warnings should break CI.

### Known limitation

The edit-threshold auto-checkin previously supported `UNITARES_HTTP_API_TOKEN`
for Bearer-token auth against remote governance. The refactored helper uses
stdlib urllib and does not pass this header. Local-only deployments (the
supported default) are unaffected.

### Upgrading from plugin 0.2.0

Claude Code caches installed plugins at `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`. A cache at version `0.2.0` predates the check-in trigger hooks shipped in `0.3.0`. To force a refresh:

```bash
rm -rf ~/.claude/plugins/cache/unitares-governance-plugin/unitares-governance/0.2.0/
```

The cache will repopulate on the next Claude Code launch. Verify the refresh landed by checking `hooks/` contains `post-stop` and `session-end` under the new version path.

## Development Workflow

Use a lightweight branch and PR flow for normal changes:

1. create a short-lived branch
2. keep the change focused
3. push the branch
4. open a PR
5. merge after review or self-review

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the repo convention.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
