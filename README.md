# UNITARES for Claude Code, Codex, and ChatGPT

[![CI](https://github.com/cirwel/unitares-governance-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/cirwel/unitares-governance-plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-0.4.22-blue.svg)](.claude-plugin/plugin.json)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Connect **Claude Code, Codex, and ChatGPT** to
[UNITARES](https://github.com/cirwel/unitares), accountability infrastructure
for long-running AI agents. Keep identity, findings, reviews, and outcomes in
an attributed record that the operator and later agents can recover after a
session ends.

This plugin supplies the MCP connection, lifecycle hooks, skills, and client
helpers. It connects to a **self-hosted, single-operator UNITARES server**;
the server owns storage, review, and governance policy.

## What it adds

- **Identity and continuity:** bind work to a process, retain its session
  context locally, and declare lineage for an actual dispatch or handoff.
- **Shared findings:** search earlier work and preserve evidence, corrections,
  and uncertainty outside the current conversation.
- **Review and outcomes:** request structured review and record what actually
  happened, so later work can recover both the claim and its result.
- **Host integration:** lifecycle guidance, edit leases, check-in helpers, and
  diagnostics for Claude Code and Codex. A sidecar supports other runners.

## Install

### Prerequisites

You need a running UNITARES server and Python 3.12+ available to hook commands
as `python3`. Windows also requires Git Bash from Git for Windows.

For a new server, follow the [verified-release Docker quickstart](https://github.com/cirwel/unitares/blob/master/docs/manual/02-install.md#21-docker-quickstart).
The local stack exposes MCP at `http://localhost:8767/mcp/` and the dashboard
at `http://localhost:8767/dashboard`. Server deployment and updates belong to
that guide; installing this plugin does not start a server.

The host contracts in this repository were tested with Claude Code 2.1.220
and Codex CLI 0.146.0. See the [adapter reference](docs/adapters.md#host-hook-contract)
for the tested payloads and host-specific behavior.

### Claude Code

Run inside Claude Code:

```text
/plugin marketplace add cirwel/unitares-governance-plugin
/plugin install unitares-governance@unitares-governance
```

Start a new session. The plugin registers its MCP client through
[`.mcp.json`](.mcp.json) and loads [Claude hooks](hooks/claude-hooks.json).
The bundled transport is unauthenticated and defaults to
`http://localhost:8767`. For a remote or authenticated deployment, disable it
and register an HTTPS MCP server as described in
[Server connections](docs/configuration.md#server-connections).

### Codex

Add the marketplace from a terminal:

```bash
codex plugin marketplace add cirwel/unitares-governance-plugin
```

Open `/plugins` in Codex, install and enable **UNITARES Governance**, then
start a new session. Open `/hooks` to review and trust the bundled command
hooks; installing the plugin alone does not authorize them. See the
[official plugin setup and hook trust documentation](https://developers.openai.com/plugins/build/plugins).

The bundled [Codex MCP transport](.codex-mcp.json) points to unauthenticated
`http://localhost:8767/mcp/`. It does **not** read the Claude transport's URL or
bearer-token settings. For a hosted or authenticated server, follow
[Server connections](docs/configuration.md#server-connections) to disable the
bundled transport and register an authenticated `unitares-governance` server.

[CODEX_START.md](CODEX_START.md) covers explicit use, hook behavior, and the
local continuity cache. Hook scripts must exist in the client's execution
environment; installing a plugin on the web does not deploy local scripts.

### ChatGPT

Build a hook-free portable package after registering the UNITARES MCP endpoint
in ChatGPT, or build a direct OAuth-DCR package for a hosted HTTPS endpoint:

```bash
python3 scripts/dev/build-chatgpt-plugin.py \
  --app-id plugin_asdk_app_REPLACE_ME \
  --output /absolute/path/to/unitares-chatgpt \
  --archive /absolute/path/to/unitares-chatgpt.zip
```

The generated package copies the canonical skills but never includes local
hooks, scripts, credentials, or skill attestations. ChatGPT therefore uses the
explicit lifecycle described by the server and skills. See the
[ChatGPT package guide](docs/chatgpt.md) for both connection modes and server
requirements.

## Core workflow

Use the bundled [lifecycle skill](skills/governance-lifecycle/SKILL.md) for the
current server contract. A normal session has a few meaningful checkpoints:

| Need | Tool or action |
|---|---|
| Bind a genuinely fresh, unbound process | `start_session(force_new=true)` once; retain the returned `client_session_id` |
| Continue a bound process | Reuse its `client_session_id`; trusted hooks can inject it |
| Recover earlier knowledge | `search_shared_memory`, then inspect the relevant findings |
| Preserve a finding or correction | `store_finding` / `update_finding`; search before adding duplicates |
| Report meaningful agent state | `sync_state` at a milestone or material change |
| Inspect current state | `check_working_state` |
| Request structured review | `request_review` |
| Record an observed outcome | `record_result` |

A new user message is not a new process. If your host has already bound this
session or supplied a governance resume anchor, follow that binding rather
than minting another identity. A genuinely new process gets its own identity;
declare `parent_agent_id` only for a real dispatch or handoff. A saved UUID or
shared workspace alone proves neither ownership nor lineage.

Check in when there is meaningful state to report. Findings, reviews, and
outcomes have their own records; a turn ending is not evidence that a task is
complete. Supply confidence only for a genuine forecast. If a response asks
for recovery, follow its stated next step. For continuity problems, use
`identity(client_session_id=...)`; for server problems, use `health_check`.

## What runs automatically

With the host hooks enabled and, in Codex, trusted:

| Event | Plugin behavior |
|---|---|
| Session start | Surface lifecycle guidance and local continuity context |
| File edit | Acquire and release file leases through the separate lease plane; record local milestones |
| Completed tool call | Maintain host-specific continuity and observation records; Codex receipts are not check-ins |
| Turn stop | May emit an automatic `substrate_interpretation`, separately labeled from an agent-authored check-in |
| Shell push | Guard against pushing more work onto a branch whose PR is already merged or closed |

Claude also supports asynchronous edit check-ins. Codex does not make
synchronous per-edit governance check-ins. The [check-in reference](docs/check-ins.md)
documents triggers, provenance, diagnostics, and the `UNITARES_CHECKINS=off`
kill switch.

## Configuration

Automatic onboarding defaults to **off**. Set `UNITARES_AUTO_ONBOARD=on` if
you want the Stop hook to create a slot-scoped identity when none is bound.
Otherwise an unbound session can produce identity-free observations.

Edit leases default to enabled. Missing or unreachable lease infrastructure
fails open unless `UNITARES_FILE_LEASES_REQUIRED=1`; an actual conflicting
lease can deny an edit. Optional Watcher integration is off by default and
requires explicit trusted paths. These settings and the full environment
table live in [Configuration](docs/configuration.md).

## Other clients

For a runner without native lifecycle hooks, the bundled
[sidecar](docs/adapters.md#sidecar) retains slot identity and injects it into
governance calls. It supports REST and a minimal JSON-RPC MCP interface; it is
not a full streamable-MCP/SSE proxy.

For a plain CLI, run the process wrapper from this checkout:

```bash
scripts/u-run --class goose -- goose run
```

The wrapper manages a sidecar for the child and records an exit observation
from wall time and exit status. See [the wrapper guide](docs/adapters.md#plain-cli-wrapper-u-run).

Hermes-native lifecycle bindings live in
[`unitares-host-adapter`](https://github.com/cirwel/unitares-host-adapter).
Adding a direct MCP connection to Hermes exposes tools but does not by itself
install lifecycle automation.

## Documentation

| Guide | Read it for |
|---|---|
| [Codex quickstart](CODEX_START.md) | Setup, explicit workflow, and continuity caches |
| [ChatGPT package](docs/chatgpt.md) | Build a hook-free portable plugin for a registered app or hosted OAuth MCP server |
| [Configuration](docs/configuration.md) | Authentication, remote servers, environment variables, and kill switches |
| [Adapter reference](docs/adapters.md) | Host contracts, Claude/Codex differences, sidecar, and wrapper |
| [Check-ins](docs/check-ins.md) | Automatic versus agent-authored records, triggers, logs, and upgrades |
| [Skills](skills/) | Lifecycle, state estimation, dialectic review, shared knowledge, and integration guidance |
| [Claude commands](commands/) | `/governance-start`, `/checkin`, `/diagnose`, `/dialectic`, and `/dialectic-respond`; these commands are not installed by Codex |
| [Documentation index](docs/README.md) | Design rationale and the full documentation map |
| [Contributing](CONTRIBUTING.md) | Validation, branch/PR workflow, and verified canonical skill mirrors |

## Scope and limits

Governance readings estimate state; they do not establish task correctness or
prove that a pause improves outcomes. See the server's
[evidence and limits](https://github.com/cirwel/unitares/blob/master/docs/EVIDENCE_AND_LIMITS.md).
The plugin keeps agent-authored reports, automatic observations, and actual
outcomes distinguishable. Runtime policy, thresholds, and persistence remain
in [`cirwel/unitares`](https://github.com/cirwel/unitares).

## Development

Changes, including documentation, use a fresh branch and PR. Run the required
checks from this checkout:

```bash
./scripts/dev/lint-command-cache-contract.sh
./scripts/dev/lint-doc-drift.sh
python3 scripts/dev/lint-doc-command-examples.py
./scripts/check-skill-freshness.sh
python3 -m pytest
```

Shared skills are mirrored from canonical UNITARES content; use the verified
mirror workflow described in [Contributing](CONTRIBUTING.md) for those changes.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
