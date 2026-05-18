---
name: governance-fundamentals
description: >
  Use when an agent needs to understand UNITARES governance concepts — EISV state vectors,
  basins, verdicts, coherence, calibration. Reference material for interpreting governance
  metrics and understanding the thermodynamic model.
license: Apache-2.0
compatibility: Requires UNITARES governance MCP server (gov.cirwel.org or local http://127.0.0.1:8767/mcp/)
metadata:
  unitares.last_verified: "2026-04-25"
  unitares.freshness_days: "14"
---

# Governance Fundamentals

UNITARES gives AI agents digital proprioception — awareness of their own state, their relationship to the system, and whether they are drifting. Agent work is tracked through a thermodynamic model (energy, entropy, coherence) and a shared knowledge graph across all agents.

## EISV State Vector

Four dimensions, updated through check-ins:

| Dim | Range | Meaning |
|-----|-------|---------|
| **E** (Energy) | [0, 1] | Productive capacity |
| **I** (Information Integrity) | [0, 1] | Signal fidelity |
| **S** (Entropy) | [0, 1] | Semantic uncertainty (lower is better) |
| **V** (Void) | [-1, 1] | Accumulated E-I imbalance |

The dimensions couple — E pulls toward I, S responds to complexity, V accumulates imbalance, **coherence** falls out of all four. Coherence is *structural health* (how well E/I/S/V hold together as a vector), **not a quality score for your work** — this is what makes the "do not game coherence" rule below meaningful. Full range is [0, 1]; the typical governed range is ~0.45–0.55, but coherence can travel anywhere in [0, 1] under stress — use `get_governance_metrics()` for current values. For the coupling math, see `references/eisv-deep.md`.

### Two V channels (and what each one drives)

`get_governance_metrics(lite=false)` exposes V in three places — `ode_eisv.V`, `behavioral_eisv.V`, and `primary_eisv.V` — and they can diverge significantly. Do not assume a single V.

- **`ode_eisv.V`** — thermodynamic integrator, heavily damped, typically ~[−0.1, 0.1]. **Coherence is computed from this V.** That is why coherence often sits near 0.5 (tanh(0)=0 midpoint) regardless of what behavioral V is doing.
- **`behavioral_eisv.V`** — observation-first EMA of actual agent behavior. Can swing to ±0.4. **Verdicts** use this channel once `behavioral_eisv.confidence ≥ 0.3`; before that, verdicts fall back to ODE.
- **`primary_eisv`** — whichever channel is currently authoritative for verdicts. Check `primary_eisv_source` to see which.

If you see flat coherence next to a swinging V, you are almost certainly comparing across channels. To move coherence you must move *ODE* V via sustained E-I imbalance, not via short-term behavioral swings. The `state_semantics` block in the metrics response is the runtime-authoritative version of this.

## Verdicts — What to Do

Governance issues a verdict after each check-in. This is the operational signal:

| Verdict | Meaning | Action |
|---------|---------|--------|
| **proceed** | State is healthy | Continue working |
| **guide** | Something is slightly off | Read the guidance text, adjust approach |
| **pause** | Needs attention | Stop current work, reflect, consider dialectic review |
| **reject** | Significant concern | Requires dialectic review or human input |

A `margin: tight` flag means you are near a basin edge. Be more careful with next steps.

## Basins

Your state sits in a basin — a region of EISV space:

- **High basin**: Healthy. E and I high, S and V low. Normal operating range.
- **Low basin**: Degraded. May need recovery or intervention.
- **Boundary**: Transitioning. Verdicts may carry `margin: tight`.

Use `get_governance_metrics()` for the current basin/mode labels — do not assume they are constant across runtime versions.

## Calibration

The system tracks whether your stated confidence matches outcomes. Over time this builds a calibration curve.

- Ground truth comes from objective signals: test pass/fail, command exit codes, lint results, file operations. These feed calibration automatically via `auto_ground_truth.py` and the `outcome_event` hook. Human validation is not required for deterministic outcomes.
- Overconfidence is tracked and penalizes Information Integrity through entropy coupling.

## Diagnostics — When the Numbers Look Wrong

Do not guess first. Use:

- `identity()` — verify who the runtime thinks you are
- `health_check()` — verify the server and knowledge graph are healthy
- `get_governance_metrics()` — current live thresholds and interpreted state

## What NOT to Do

- **Do not game coherence** by reporting low complexity / high confidence on everything
- **Do not ignore guide verdicts** — they are early warnings before pause/reject
- **Do not create duplicate discoveries** — search the knowledge graph first
- **Do not check in after every trivial action** — it is noise, not signal
- **Do not leave high-severity findings open forever** — resolve or archive them

## Going Deeper

- `references/eisv-deep.md` — coupling math, coherence definition C(V, Theta), calibration internals
- `governance-lifecycle` skill — onboard, check-in, recovery flow
- `dialectic-reasoning` skill — what happens when a verdict pauses you
