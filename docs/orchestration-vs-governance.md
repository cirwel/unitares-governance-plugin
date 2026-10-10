# Orchestration is not governance

When agent runtimes start adding multi-agent features, the same questions surface every time. How do specialized agents communicate? Who resolves their conflicts? Where does shared memory live? How do you keep an agent from drifting? These questions do not all live at the same layer. Conflating them is what makes multi-agent systems hard to design.

There are two distinct layers.

## The orchestration layer

Routes work between agents. Task graphs, consensus voting, "Chairman" patterns, A2A wire protocols, Paperclip-style sized routing, profile dispatch. This is the layer that answers *how do agents talk and who runs next*. Most current frameworks sit here — Hermes Agent's `delegate` tool, CrewAI, LangGraph, AutoGen, the [Agent2Agent](https://github.com/a2aproject/A2A) protocol.

## The governance layer

Supervises a fleet of agents whose orchestration topology is already in motion. It answers different questions:

- Is agent A drifting from its task?
- Should the fleet pause?
- Does this discovery contradict an earlier one in shared memory?
- Which intervention is warranted?
- What is the auditable provenance of that intervention?

These do not collapse into each other. An orchestrator routing work between five clones of the same model still has to answer the governance questions, and mixing in a sixth model only makes them sharper. A governance layer with no orchestrator has nothing to govern.

## When the distinction is structural

For single-runtime, single-vendor fleets, governance and orchestration share process, memory, and failure domain — the layer cut is more rhetorical than structural in that case. The cut earns structural weight when fleets mix runtimes. That is the case UNITARES is built for, and it is also the case the multi-agent v1 designs are converging on, even when the framing is single-runtime today.

## Why the distinction matters now

Most of the asks people make of multi-agent v1 — *shared brain across profiles, persistent specialized agents, async talk-back without timeouts, manager agent that resolves conflicts, human approval gates, role-based handoffs* — are governance-layer asks reframed as orchestration features. Building them at the orchestration layer means rebuilding them per-runtime. Building them once at the governance layer lets each orchestrator stay thin and lets fleets that mix runtimes share the same supervisor.

This is also why the question *one Hermes with five profiles vs. five Hermes vs. many specialized Hermes* reframes once a separate governance layer exists. All three topologies can run under the same supervisor. The trilemma stops being a routing question and becomes a topology-cost question — latency, failure-mode diversity, and per-process overhead still differ across topologies, but those are tradeoffs to choose, not coordination problems to solve.

## What UNITARES is

UNITARES is accountability infrastructure for long-running AI agents: a self-hosted, single-operator federation kernel for agent identity, claims and evidence, review, outcomes, and reconstruction. At each check-in it returns a policy action, with auditable provenance. It does not orchestrate; it keeps the record that orchestrated agents share.

- Shared knowledge graph is **fleet-wide**, not runtime-local
- Verdict authority is **cross-runtime** (proceed / guide / pause / reject)
- Conflict resolution uses **structured dialectic** as the primary mechanism, with quorum escalation for unresolvable sessions
- Identity is per-agent, **lineage-observed** (advisory cross-host), and never silently substituted

Host bindings ship today for Hermes Agent and Claude Code via [`unitares-host-adapter`](https://github.com/CIRWEL/unitares-host-adapter); a Goose binding is forthcoming.

## What "supervises" actually does

A reasonable next question: when governance issues `pause` or `reject`, what stops the agent? Verdicts return in-band on the agent's next governance call. Enforcement is **cooperative** — the agent's harness sees the verdict and honors it before the next tool action. UNITARES is not a token-level interceptor or a scheduler veto; it shapes the contract the runtime operates under.

Audit is **non-cooperative**. Verdict history, dialectic transcripts, knowledge-graph contradictions, and lineage observations are queryable independently of any agent's cooperation. An agent that ignores its verdict is visible to operators and to peer agents in the fleet immediately. The layer's load is this asymmetry: cooperative enforcement, non-cooperative audit. That is enough surface to govern coding-agent fleets where the tool-call loop is exposed; it is deliberately less than what a kernel-level supervisor would claim.

## If you are designing multi-agent v1

Before you pick task-routing primitives, separate the asks. Which features are routing problems? Which are fleet-supervision problems? Solving the second category once at the right layer is cheaper than solving them per-orchestrator forever.

## Further reading

- [UNITARES paper v6.9.1](https://github.com/CIRWEL/unitares-paper-v6) — *Information-Theoretic Governance of Heterogeneous Agent Fleets*. Concept DOI: [10.5281/zenodo.19647159](https://doi.org/10.5281/zenodo.19647159)
- [Governance MCP](https://github.com/CIRWEL/unitares) — runtime, MCP server, storage
- [Host adapter](https://github.com/CIRWEL/unitares-host-adapter) — thin client bindings for agent hosts
