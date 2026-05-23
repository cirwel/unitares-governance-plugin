# Recovery — When You Are Paused or Stuck

Reference material for recovering from a `pause` or `reject` verdict. Loaded by `governance-lifecycle/SKILL.md` on demand from the verdict table. The day-to-day verdict reading lives in the SKILL body; this file is for when you actually need to *act* on a non-proceed verdict.

For the *other* kind of recovery — resuming a previously-saved identity — see `references/resume-semantics.md` instead. The two are unrelated.

## Recovery Options

| Situation | Tool | Notes |
|-----------|------|-------|
| Stuck or paused, want automatic recovery | `self_recovery()` | Attempts to restore healthy state |
| Disagree with verdict, want structured review | `dialectic(action="request")` | Starts thesis/antithesis/synthesis (see `dialectic-reasoning` skill) |
| Manual override needed | `operator_resume_agent()` | Requires human/operator action |

Recovery is not a shortcut — `self_recovery()` examines your EISV state and determines if resumption is safe. If your metrics are genuinely degraded, it will not force a resume.

## What to Try First

1. **Re-read the verdict text and any guidance** — the server often explains *why* it paused. The fix is usually visible.
2. **`get_governance_metrics()`** — see which dimension(s) are degraded. Low E suggests fatigue / slowdown is appropriate; high S suggests you're in unfamiliar territory and should seek information; negative V (running careful) is rarely a problem; positive V (running hot) suggests the work is shallower than your check-ins claim.
3. **`self_recovery()`** — only after you understand *why* you were paused. A blind retry is the canonical anti-pattern.
4. **`dialectic(action="request")`** — when you disagree with the verdict on substantive grounds. Do not use as a re-roll.
5. **`operator_resume_agent()`** — last resort; requires the human operator to intervene.

## Specialized Tools (rare)

- `call_model()` — delegate to a secondary LLM for analysis
- `detect_stuck_agents()` — find unresponsive agents (operator-facing)
- `dialectic(action="thesis" | "antithesis" | "synthesis")` — dialectic participation (covered in `dialectic-reasoning` skill)
- `export()` — export session history
- `knowledge()` / `agent()` / `calibration()` — full CRUD surfaces; tool descriptions cover the parameters
