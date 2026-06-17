# Adapter Notes

How each client connects to a running UNITARES governance server. Adapters are a
convenience layer — the server is the source of truth and the client stays thin.
For env-var configuration see the [Configuration section of the README](../README.md#configuration).

## Claude

The current Claude adapter includes session-start, pre-edit, post-edit, and session-end hooks. Those hooks should be treated as an adapter convenience, not the canonical governance policy. In particular, frequent file writes should not automatically be interpreted as meaningful governance events.

The pre-edit hook acquires a BEAM file lease before Edit/Write/MultiEdit. Missing lease-plane configuration fails open by default, while real `held_by_other` contention blocks the edit with a visible explanation. Post-edit releases the just-edited file lease immediately; session-end remains a best-effort cleanup path for any lease that survived an interrupted edit.

The `session-start` hook remains read-only: it tells the agent to call `start_session(force_new=true)` before substantive work. If the agent has not onboarded by the end of the turn, `post-stop` uses `scripts/onboard_helper.py` to lazily mint a fresh, slot-scoped identity and then emits the normal `turn_stop` summary under that identity. Set `UNITARES_AUTO_ONBOARD=off` or legacy `UNITARES_DISABLE_AUTO_ONBOARD=1` to fall back to identity-free floor observations for un-onboarded sessions.

For the full Claude check-in trigger contract, see [check-ins.md](./check-ins.md).

## Codex

Codex and ChatGPT support should stay minimal and explicit:

- package shared skills through `.codex-plugin/plugin.json`
- document manual command flows for agents that can use them
- treat `.unitares/session-<slot>.json` as the neutral local continuity cache; flat `.unitares/session.json` is legacy/read-only
- use `scripts/session_cache.py` as the shared cache helper across adapters
- avoid client-specific auto-checkin behavior until there is a Codex-native reason to add it

If you want adapter-like onboarding/check-in behavior from Codex, run the sidecar
(below). The full Codex/ChatGPT quickstart lives in [CODEX_START.md](../CODEX_START.md).

## Sidecar

For clients without lifecycle hooks, run the local identity sidecar and send
governance REST tool calls through it:

```bash
python3 scripts/identity_sidecar.py \
  --server-url http://localhost:8767 \
  --workspace "$PWD" \
  --slot codex-local \
  --port 8768
```

Phase 1 is a dependency-free sidecar, not a full streamable-MCP/SSE
implementation. It wraps REST `/v1/tools/call` and minimal JSON-RPC MCP
`/mcp/` requests, lazily onboards a slot when needed, injects
`client_session_id` into attribution-relevant governance calls, forces
`force_new=true` for bare `onboard` / `start_session`, stamps the slot cache
after check-ins, and exposes `GET /audit`. Useful endpoints:

- `GET http://127.0.0.1:8768/client-config?slot=codex-local` for a generated MCP/client snippet
- `POST http://127.0.0.1:8768/v1/tools/call` with `{"name": "...", "arguments": {...}}`
- `POST http://127.0.0.1:8768/mcp/` for JSON-RPC MCP requests; `tools/call` is intercepted and other JSON requests pass through
- `POST http://127.0.0.1:8768/turn/checkin` with `response_text`, `complexity`, and `confidence`
- `POST http://127.0.0.1:8768/turn/stop` for an end-of-turn check-in
- `GET http://127.0.0.1:8768/audit?log_tail=200` for bounded local cache/log contract findings

Use `X-UNITARES-Slot` or top-level `{"slot": "..."}` when one sidecar serves
multiple clients. Without an explicit slot, the sidecar uses a workspace-derived
default slot.

For clients that accept a URL MCP server, point them at
`http://127.0.0.1:8768/mcp/` only when they use JSON request/response MCP. The
generated `GET /client-config` response includes the URL, `X-UNITARES-Slot`
header, and a minimal `mcpServers` snippet. If a client requires streamable
HTTP/SSE semantics, use the upstream governance MCP endpoint until the sidecar
grows that transport path.
