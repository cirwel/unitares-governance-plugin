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

The UNITARES knowledge graph is shared infrastructure for a fleet of agents. PostgreSQL full-text search is the default backend; Apache AGE is an optional graph backend, and semantic retrieval depends on the configured backend. It exposes typed discoveries (`insight`, `bug_found`, `pattern`, `architectural_decision`, ...) through `knowledge(action=...)`, with search, status updates, lifecycle audit and cleanup, and on-demand topic synthesis. Findings carry provenance and can be revised or superseded. A search hit is a lead to verify against current evidence, not a current-state guarantee. Structured dialectic is a separate way to review contested claims.

## Where each one wins

| Axis | LLM wiki | UNITARES KG |
|---|---|---|
| Multi-agent / concurrent writers | single user/agent | fleet-wide shared findings |
| Provenance & audit | `log.md` only | attributed findings and read audit |
| Conflict handling | lint *suggests* | status updates, supersession, separate dialectic review |
| Scale | ~100 sources | PostgreSQL search; optional AGE graph backend |
| Lifecycle / governance | workflow-defined lint | status model, cleanup, staleness audit |
| Synthesis into a compounding artifact | maintained during ingest | on-demand topic rollups |
| Zero-infra, human-readable | yes | needs a running server |

These are different operating models. The KG supports shared, attributable findings; the wiki pattern puts a maintained narrative at the center of retrieval.

## The gap worth taking seriously

The wiki's **ingest** step integrates a new source *into existing pages*: it rewrites the synthesis and cross-references before any query arrives. The UNITARES KG stores discrete discoveries and can create persistent topic rollups with `knowledge(action="synthesize")`. The action runs on demand, or when explicitly scheduled, rather than on every write. Search can also synthesize at read time when its `synthesize` option is used.

The remaining gap is maintenance: a wiki editor incorporates each new source into its narrative, while a KG topic rollup reflects its members only when synthesis runs again. Neither a stored rollup nor an open discovery proves that its claims still match the current system. The KG has staleness signals, audit, status updates, and supersession, but agents still need to verify consequential claims and close findings when the evidence changes.

Running an LLM synthesis pass on every `store` or `note` would add latency, cost, and more generated rows to maintain. The existing on-demand action keeps that work explicit. A periodic cadence may help for active topics, but it should be justified by observed retrieval needs and paired with a way to detect stale rollups.

## "Or any others" — the credible neighbors

The honest competitive set is not RAG but agent-memory knowledge graphs, and two of them are ahead of us on a specific dimension:

- **Zep / Graphiti** — a bi-temporal knowledge graph for agent memory. On time-aware reasoning (when a fact became true or false, and invalidation of superseded facts) it is more capable than the UNITARES KG today. But matching it is a substrate-level change — migration, AGE query rewrites, and a time dimension threaded through every read path — for a payoff (point-in-time reconstruction, automatic invalidation) that is speculative for the current use case, given that `superseded` status plus `created_at` already covers most of it. This is a YAGNI candidate: a documented idea, not a roadmap item, until a concrete temporal-reasoning failure is actually observed.
- **Microsoft GraphRAG** — community detection plus hierarchical summarization. UNITARES now has topic rollups, but does not claim GraphRAG-style community detection or automatic hierarchical maintenance.
- **Letta/MemGPT, Mem0, Cognee** — single-agent memory systems; weaker than the KG on fleet coordination, audit, and governance.

## Bottom line

The KG remains useful for fleet-wide, attributable discoveries. On-demand topic synthesis is already available; the next question is whether a measured retrieval need warrants running it on a cadence and checking that rollups stay current. Temporal fact validity remains a documented idea, deferred until a concrete failure justifies the substrate cost.

## Sources

- [Karpathy `llm-wiki` gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
- [Microsoft GraphRAG](https://github.com/microsoft/graphrag)
- [Graphiti (Zep) temporal knowledge graph](https://github.com/getzep/graphiti)
