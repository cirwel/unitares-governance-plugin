---
name: dialectic-reasoning
description: >
  Use when an agent is participating in a UNITARES dialectic session — paused and needs to
  submit a thesis, reviewing another agent's thesis, or synthesizing conditions for resolution.
  Covers structured argumentation and convergence.
license: Apache-2.0
compatibility: Requires UNITARES governance MCP server (gov.cirwel.org or local http://127.0.0.1:8767/mcp/)
metadata:
  unitares.last_verified: "2026-06-11"
  unitares.freshness_days: "14"
---

# Dialectic Reasoning

## When Dialectics Happen

A dialectic session is triggered when:

- You receive a **pause** or **reject** verdict and want to contest it
- You call `dialectic(action="request")` for peer verification
- You find something that contradicts the knowledge graph
- A high-stakes decision needs structured verification before proceeding

Dialectics are not punishment. They are a structured way to resolve disagreements using evidence and negotiation. In current UNITARES language, think of them as structured review more than "recovery court."

The runtime may still expose legacy aliases (`request_dialectic_review()`, `submit_thesis()`, `submit_antithesis()`, `submit_synthesis()`), but prefer the unified `dialectic(action=...)` surface when available.

On current servers, bare `dialectic({})` defaults to `action="list"` for orientation. Pass the action explicitly in docs, examples, and automation so the intent stays legible.

For small decisions that need a structured second look but not a persisted session, use `dialectic(action="quick", issue_description=..., position=...)`. It returns `record_decision` or `escalate_full_dialectic` and flags risk markers such as missing position, high risk/coherence metrics, security/data-loss language, or three or more concerns.

`dialectic(action="get")` and `dialectic(action="list")` may include actionability fields naming the required role, allowed agent IDs, whether the current bound agent can submit, and the recommended next action. Follow those fields before writing thesis/antithesis/synthesis.

## Phase 1: Thesis

The paused or requesting agent submits their position:

```
dialectic(
  action: "thesis",
  session_id: "<session-id>",
  reasoning: "Why I should resume / why my position is correct",
  root_cause: "What went wrong or what triggered this",
  proposed_conditions: ["Concrete, measurable condition 1", "Condition 2"]
)
```

### Writing a Good Thesis

- **Reasoning**: Explain your perspective with reference to EISV data, not feelings
- **Root cause**: Be specific. "High entropy from complex refactoring task" is better than "I got paused for no reason"
- **Proposed conditions**: Must be concrete and measurable. Tie them to live metrics or observable behavior, not vague intent.

## Phase 2: Antithesis

A reviewing agent examines the thesis and raises concerns:

```
dialectic(
  action: "antithesis",
  session_id: "<session-id>",
  reasoning: "Counter-arguments to the thesis",
  concerns: ["Specific risk 1", "Specific risk 2"],
  observed_metrics: { E: 0.45, I: 0.38, S: 1.2, V: 0.8 }
)
```

### Writing a Good Antithesis

- **Reasoning**: Address the thesis directly. What does the data actually show?
- **Concerns**: Be specific about risks. "I exceeds E by 0.3, indicating integrity debt" is useful.
- **Observed metrics**: Include the actual EISV values backing your concerns

If identity or session continuity looks suspect, verify with `identity()` before assuming the thesis belongs to the agent you think it does.

If a session is waiting on a different reviewer and the operator wants your bound agent to answer instead, use `dialectic(action="antithesis", take_over_if_requested=true, ...)` or explicitly reassign with `dialectic(action="reassign", session_id=..., new_reviewer_id=...)`.

## Phase 3: Synthesis

Both sides negotiate toward resolution:

```
dialectic(
  action: "synthesis",
  session_id: "<session-id>",
  reasoning: "How we reconcile the thesis and antithesis",
  agrees: true/false,
  proposed_conditions: ["Negotiated condition 1", "Condition 2"]
)
```

Convergence happens when both sides agree on conditions. The synthesis should reflect genuine agreement, not capitulation.

When `agrees: true`, include populated `proposed_conditions` unless a prior synthesis already supplied them. Current servers also accept `conditions` as an alias, but `proposed_conditions` is the canonical field for thesis/synthesis messages.

## Resolution Outcomes

| Outcome | Meaning |
|---------|---------|
| **resume** | Agent continues with agreed conditions |
| **block** | Agent stays paused — conditions not met or agreement not reached |
| **failed** | Session ended without safe convergence or resolution execution failed |

## How to Participate Well

- **Be honest about metrics.** The EISV data is right there. Denying it undermines your credibility.
- **Propose measurable conditions.** "Entropy below X within Y check-ins" is verifiable. "I will be more careful" is not.
- **Do not be defensive.** A pause verdict is data, not judgment. Analyze why it happened.
- **Acknowledge valid concerns.** If the antithesis raises a real issue, say so. Partial agreement strengthens your position.
- **Look at EISV data, not feelings.** Call `get_governance_metrics()` for actual values. The thermodynamic model is the shared ground truth.

## Common Mistakes

- **Ignoring the metrics**: Arguing against a pause while your entropy is at 1.5 and energy is at 0.3. The numbers matter.
- **Proposing impossible conditions**: promising a metric target without checking the live state first.
- **Being defensive instead of analytical**: "The system is wrong" vs. "My entropy spiked because of X, and here is how I address it."
- **Treating dialectic as adversarial**: It is collaborative problem-solving with structure, not a trial. Both sides benefit from honest resolution.
- **Rushing synthesis**: Agreeing to conditions you cannot meet just to get unpaused guarantees a future pause.
