# UNITARES Governance

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Claude Code Plugin](https://img.shields.io/badge/Claude%20Code-plugin-d97757.svg)](https://docs.claude.com/en/docs/claude-code/plugins)
[![Codex Plugin](https://img.shields.io/badge/Codex-plugin-10a37f.svg)](./CODEX_START.md)
[![Version](https://img.shields.io/badge/version-0.4.9-blue.svg)](.claude-plugin/plugin.json)

Client/plugin integration layer for **UNITARES** — the runtime telemetry and coordination layer for heterogeneous AI-agent fleets. This repo provides Claude/Codex-facing skills, command guidance, hook scripts, and sidecar tooling for connecting coding agents to a running UNITARES governance server. The runtime itself lives in [`cirwel/unitares`](https://github.com/cirwel/unitares); Hermes-native lifecycle bindings live in [`cirwel/unitares-host-adapter`](https://github.com/cirwel/unitares-host-adapter).

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
- `unitares-host-adapter` contains host lifecycle bindings, including the Hermes-native adapter used through a thin Hermes user plugin
- `unitares-governance-plugin` contains the Claude/Codex-facing plugin package, skills, command guidance, hook scripts, and sidecar tooling
- optional bridges like Discord can remain separate integrations

This repo should not duplicate server business logic or become the source of truth for thresholds that already live in the runtime.

## Current Surfaces in This Repo

- Codex/ChatGPT: plugin packaging plus shared skills and explicit command guidance
- Claude: hooks, session helpers, command docs, and optional file-lease/check-in conveniences
- Sidecar: local proxy for clients without native lifecycle hooks

Hermes Agent is intentionally not listed here as the native path. For Hermes, use `unitares-host-adapter` and install a thin Hermes user plugin that imports `unitares_host_adapter.bindings.hermes`. Direct Hermes MCP config only exposes tools; it does not provide automatic lifecycle check-ins by itself.

The shared value in this repo is the workflow guidance and client integration surface, not a second copy of the governance model.

## Start Here

If you are using ChatGPT or Codex, start with [CODEX_START.md](./CODEX_START.md).

That path is now the preferred default. Claude hook automation remains supported, but it is no longer the canonical mental model for UNITARES usage.

## Documentation

| Document | What it covers |
|---|---|
| [CODEX_START.md](./CODEX_START.md) | Preferred entry path for Codex/ChatGPT — modes, recommended flow, continuity cache |
| [docs/](./docs/) | Documentation index and design-rationale notes (why the plugin is shaped this way) |
| [skills/](./skills/) | Agent-facing capability docs — governance fundamentals, lifecycle, dialectic, knowledge graph, Discord bridge |
| [commands/](./commands/) | Slash-command guidance — `/governance-start`, `/checkin`, `/diagnose`, `/dialectic` |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | Branch/PR convention and review standard |

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

This repo is a **client/plugin integration layer only** — it does not include the governance engine. You need a server running before any of these commands or skills do anything useful.

**Easiest server bring-up — Docker Compose:**

```bash
git clone https://github.com/CIRWEL/unitares.git
cd unitares
docker compose up
# server now at http://localhost:8767/mcp/
```

That single command brings up Postgres+AGE+pgvector, Redis, and the governance server. The Pi/Lumen embodiment side is optional — governance runs standalone. For bare-metal install (Homebrew Postgres, native AGE compile) see [unitares/README.md](https://github.com/CIRWEL/unitares#installation).

Once the server is up, **the plugin registers its MCP client automatically** — it
ships an `.mcp.json` pointing at `http://localhost:8767/mcp/`, so there is nothing
to hand-edit. Installing the plugin and starting Claude Code is enough.

If your server is on a different host or port, set `UNITARES_SERVER_URL` to the
**base** URL (no `/mcp/` suffix — the plugin appends it):

```bash
export UNITARES_SERVER_URL=https://gov.example.org   # plugin uses .../mcp/
```

To override the auto-registered server (e.g. point at a sidecar), a
manually-configured `unitares-governance` server in your own settings takes
precedence over the plugin's.

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

Adapters are a convenience layer over the governance server, not the canonical
policy — the server stays the source of truth and the client stays thin.

- **Claude** — session-start, pre-edit, post-edit, and session-end hooks, plus BEAM file leases.
- **Codex/ChatGPT** — minimal and explicit; shared skills, manual command flows, slot-scoped continuity cache.
- **Sidecar** — a dependency-free local proxy for clients without lifecycle hooks.
- **Hermes Agent** — native lifecycle binding lives in `unitares-host-adapter`; this repo is only relevant to Hermes if you deliberately route through the generic sidecar instead.

Full details, endpoints, and configuration are in [docs/adapters.md](./docs/adapters.md).
The Codex/ChatGPT quickstart is [CODEX_START.md](./CODEX_START.md).

## Non-Goals

This repo should not:

- redefine the governance math
- duplicate server-side threshold logic
- auto-checkin every trivial file write by default
- override runtime verdicts locally

## Check-In Triggers

The Claude adapter emits canonical `process_agent_update` calls at three trigger
points (`turn_stop`, `auto_edit`, `session_end`) through one shared helper
(`scripts/checkin.py`) that redacts secrets, truncates, logs, and is
fire-and-forget. A `UNITARES_CHECKINS=off` kill switch suppresses all of them.

For the trigger table, the diagnostic log format, the protective audit, the
known token-auth limitation, and plugin-cache upgrade steps, see
[docs/check-ins.md](./docs/check-ins.md).

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
