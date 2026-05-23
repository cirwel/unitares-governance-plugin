# Resume Semantics — PATH Reference and the Anti-Pattern

Reference material for the **rare** case of returning to a saved identity from a previous process — and the more common case of correctly *not* doing that. Loaded by `governance-lifecycle/SKILL.md` on demand. Day-to-day onboarding (which uses lineage declaration, not resume) lives in the SKILL body.

For the *other* kind of recovery — recovering from a paused verdict — see `references/recovery.md`. The two are unrelated.

## The v2 Default: Declare Lineage, Don't Resume

A fresh process-instance is a fresh agent. To continue prior work across process boundaries, **declare lineage** via `onboard(force_new=true, parent_agent_id=<prior-uuid>, spawn_reason="new_session")`. This is the v2 default; the rest of this document covers the cases where you really do need to resume an existing identity, and one important anti-pattern that re-introduces a closed hijack vector.

`name=` is cosmetic — passing `name="Same-Agent"` does not re-bind to an existing agent (PATH 2.5 retired 2026-04-17). Note: `name=` is *also* counted as a "proof signal" by the S13 gate (next section), which means it suppresses the auto-mint behavior even though it does not drive identity lookup. Two different mechanisms, easily confused.

## S13 Fresh-Instance Gate — When the Server Auto-Mints for You

The server auto-promotes `force_new=true` and emits `[FRESH_INSTANCE]` only for **truly arg-less** `onboard()` calls. Proof-shaped arguments such as `client_session_id`, `agent_uuid`, `agent_id`, or `name` suppress that auto-mint gate. So `onboard(name="Foo", model_type="claude")` (the legacy pattern) does *not* auto-mint and can hit weak session/PATH 2 IP:UA pin behavior. **Pass `force_new=true` explicitly whenever you mean to mint fresh.**

After S1-c (2026-05-23), `onboard(continuity_token=...)` is no longer a resume path. Token-only `onboard`, token-only `identity`, and token-only `bind_session` return `status=continuity_token_resume_rejected`. The token remains valid only as the explicit PATH 0 ownership proof paired with `agent_uuid`.

## What Each Resume Path Actually Does

These describe paths the **server** runs internally when it sees a proof signal. They are not a strength ladder you should be climbing — most callers should use lineage declaration above and never present a resume signal at all.

- **PATH 1/2 (`client_session_id`)** — transport-bound session continuity *within the same process-instance*. Pass on subsequent calls in this process to maintain identity. Weak across processes by design.

- **PATH 0 (`agent_uuid` + `continuity_token`)** — explicit ownership-proven rebind for the **same live process** that already holds the continuity_token. The Identity Honesty Part C gate (2026-04-18) verifies the token's `aid` claim equals the requested `agent_uuid`. Without the matching token, the request is treated as a hijack (see below). **Not for cross-process resurrection** — that is the anti-pattern, not a feature.

- **Token-only resume (`continuity_token` alone)** — retired by S1-c. The server now rejects this with `status=continuity_token_resume_rejected` instead of extracting the embedded UUID and rebinding. If you were relying on this, mint fresh with `force_new=true` and declare lineage through `parent_agent_id`, or use PATH 0 only when you also have the target `agent_uuid` and are proving ownership of that live UUID.

## The Anti-Pattern: Auto-Injecting continuity_token Between Calls

**Do not** auto-inject `continuity_token` between calls at the client transport layer (SDK, hook, adapter). The token is per-process-instance proof for the PATH 0 anti-hijack gate, **not** a transport-level identity claim. Auto-injection re-introduces the silent-resurrection vector that Identity Honesty Part C closed: *any process knowing UUID X could speak as X*. For cross-process-instance continuity, declare lineage via `parent_agent_id`; do not carry a token forward.

This is the old "save the token from your first onboard, pass it on every subsequent call" pattern. It worked, and on shared hosts it was indistinguishable from hijack. If you're writing a client adapter or a check-in hook and considering this, stop.

## The Canonical Hijack Pattern — Do Not Do This

**Do not** call `identity(agent_uuid=X, resume=true)` (or `onboard(agent_uuid=X)`) with a UUID you learned from elsewhere — a hook listing, another agent's check-in, a log line — without a matching `continuity_token`. The server logs `[IDENTITY_STRICT]` and emits an `identity_hijack_suspected` broadcast event with `path='path2_ipua_pin'` or similar; under `UNITARES_IDENTITY_STRICT=strict` the request is rejected outright. See KG bug `2026-04-20T00:09:51`.

Without any of the proof signals above, you are a new agent. That is the correct semantic, not a bug.

## Identity Diagnostics

When continuity seems unclear, call `identity()` (no args) and inspect:

- `identity_status`
- `bound_identity`
- `session_resolution_source`
- `continuity_token_supported`
- `ownership_proof_version`
- `deprecations` (when present — surfaces if the server flagged a legacy-path call)

`process_agent_update()` responses additionally surface `identity_assurance` (`tier`, `score`, `session_source`, `trajectory_confidence`, `reason`) for confirming strong continuity. `trajectory_confidence` is `None` when not applicable but is always present in the dict.

If the runtime falls back to weak signals such as fingerprinting, the right move is almost always to mint fresh and declare lineage — not to retry resume with weaker proof.
