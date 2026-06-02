# The LLM wiki is a layer, not a competitor

In April 2026 Andrej Karpathy published a gist describing an *LLM wiki*: a pattern where an agent incrementally compiles raw sources into a persistent, interlinked set of markdown pages instead of re-retrieving fragments on every query. It went viral, and the framing that traveled with it — "RAG is dead" — invites an obvious question for anyone running a knowledge graph: is the LLM wiki, or one of the other agent-memory systems, more capable than the UNITARES knowledge graph?

The short answer is no, not in aggregate. They solve a different problem, and the comparison is more useful as a map of what to borrow than as a contest to win.

## What the LLM wiki actually is

It is a pattern, not infrastructure. Three layers:

- **Raw sources** — immutable documents. The agent reads them but never edits them.
- **The wiki** — an agent-owned collection of interlinked markdown files. The agent creates pages, updates them as new sources arrive, and maintains cross-references.
- **The schema** — a configuration document (`CLAUDE.md`, `AGENTS.md`) that specifies structure, conventions, and workflow, turning a generic chatbot into a disciplined maintainer.

Three operations run against it: **ingest** (read a source, integrate it into 10–15 existing pages), **query** (synthesize an answer with citations), and **lint** (flag contradictions, stale claims, orphan pages, gaps). Storage is flat markdown plus an `index.md` catalog and an append-only `log.md`. Karpathy is explicit about scope: the pattern is intentionally abstract, and the sweet spot is roughly 100 sources and hundreds of pages "before requiring specialized search infrastructure like embedding systems."

## What the UNITARES KG is

The UNITARES knowledge graph is that specialized infrastructure. It is backed by PostgreSQL with Apache AGE for graph queries and pgvector for embeddings, and it is **fleet-wide and multi-agent** rather than single-user. It exposes full CRUD through `knowledge(action=...)` — `store`, `search`, `get`, `list`, `update`, `details`, `note`, `supersede`, `cleanup`, `stats`, `audit` — over typed discoveries (`insight`, `bug_found`, `pattern`, `architectural_decision`, ...) with a status lifecycle (`open → resolved → archived → cold`, plus `superseded`, `disputed`, `wont_fix`). Reads emit best-effort `knowledge_read` audit events with reader context. Conflicts are resolved through structured dialectic with quorum escalation, not left as a TODO. The AGE graph reconciles against the durable Postgres tables on startup via drift detection and selective rehydration.

## Where each one wins

| Axis | LLM wiki | UNITARES KG |
|---|---|---|
| Multi-agent / concurrent writers | single user/agent | fleet-wide, leases, lineage |
| Provenance & audit | `log.md` only | non-cooperative audit, read events |
| Conflict handling | lint *suggests* | dialectic + verdicts, supersede edges |
| Scale | ~100 sources | Postgres + pgvector + AGE |
| Lifecycle / governance | none | status model, cleanup, calibration |
| Synthesis into a compounding artifact | **core strength** | **weak spot** |
| Zero-infra, human-readable | yes | needs a running server |

On every axis it was designed for — concurrency, audit, governance, scale — the KG is more capable. The LLM wiki wins on exactly one thing, and it is a real thing.

## The gap worth taking seriously

The wiki's **ingest** step integrates a new source *into existing pages*: it rewrites the synthesis so the narrative compounds and the cross-references are already materialized before any query arrives. The UNITARES KG stores discrete discovery rows and `related_to` edges; synthesis happens only on *read* (`synthesize=true` on search). The knowledge-graph skill says so plainly — "the graph accumulates knowledge well but does not close loops automatically."

That is precisely the gap the wiki pattern closes. The LLM wiki is therefore best read not as a competitor to replace the KG but as a **missing layer** the KG could adopt: a pass that maintains rolled-up entity/topic pages over the raw discovery rows, so the compounding artifact exists before a query forces it into being.

One caveat carries real weight, because bloat is the genuine risk here. The wiki's *per-source* ingest fits a single-user, ~100-source, human-curated flow; a multi-agent fleet writing constantly is a different volume profile, and running an LLM synthesis pass on every `store`/`note` would be exactly the "auto-checkin every trivial write" behavior this project lists as a non-goal — added latency, cost, and a fresh source of stale auto-generated rows. The transferable shape is therefore **periodic or on-demand synthesis as a lifecycle action** (a `knowledge(action="synthesize")` alongside the existing `cleanup`, `audit`, and `stats`), not a write-time hook. Reframed that way it reuses machinery that already exists, adds no per-write cost, and changes no schema. That is the version worth building.

## "Or any others" — the credible neighbors

The honest competitive set is not RAG but agent-memory knowledge graphs, and two of them are ahead of us on a specific dimension:

- **Zep / Graphiti** — a bi-temporal knowledge graph for agent memory. On time-aware reasoning (when a fact became true or false, and invalidation of superseded facts) it is more capable than the UNITARES KG today. But matching it is a substrate-level change — migration, AGE query rewrites, and a time dimension threaded through every read path — for a payoff (point-in-time reconstruction, automatic invalidation) that is speculative for the current use case, given that `superseded` status plus `created_at` already covers most of it. This is a YAGNI candidate: a documented idea, not a roadmap item, until a concrete temporal-reasoning failure is actually observed.
- **Microsoft GraphRAG** — community detection plus hierarchical summarization. This is the wiki's compounding synthesis done at graph scale, and it is the closest existing implementation of the layer the KG is missing.
- **Letta/MemGPT, Mem0, Cognee** — single-agent memory systems; weaker than the KG on fleet coordination, audit, and governance.

## Bottom line

Neither the LLM wiki nor the other systems are more capable than the UNITARES KG across the board; the KG dominates on the multi-agent, auditable, governed-fleet problem it was built for. The LLM wiki is more capable on compounding synthesis, and Graphiti on temporal reasoning. Naming both is cheap and worth doing, but only one is worth building soon: a **periodic synthesis lifecycle action** (lean, reuses existing machinery). **Temporal edges stay a documented idea** — deferred until a real temporal failure justifies the substrate cost. The point of the comparison is to borrow deliberately, not to switch substrates or build everything the neighbors have.

## Sources

- [Karpathy `llm-wiki` gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
- [Microsoft GraphRAG](https://github.com/microsoft/graphrag)
- [Graphiti (Zep) temporal knowledge graph](https://github.com/getzep/graphiti)
