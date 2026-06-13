# Do we need an ontology? No — we need a normalizer

The question keeps coming back in different costumes: should the knowledge graph
grow an *ontology* — a formal schema of entity and relationship types — and maybe
a standing *ontology agent* to maintain it? The honest answer, measured against
what this fleet actually does, is no. The real problem hiding behind "ontology"
is smaller, duller, and almost entirely a **formatting** problem. Name it
correctly and it shrinks from a system to a function.

## The implicit ontology already exists

Most of what an ontology would give us, the KG already has, and it earned it
without calling it an ontology:

- **Node types** — a fixed `discovery_type` enum (`insight`, `bug_found`,
  `pattern`, `architectural_decision`, ...). That *is* a node ontology, and a
  disciplined one; the knowledge-graph skill says outright, "Do not invent
  discovery types casually."
- **A lifecycle** — a real state machine (`open → resolved → archived → cold`,
  plus `superseded`, `disputed`, `wont_fix`).
- **Edges** — `related_to` and `supersedes`.
- **Synthesis** — `knowledge(action="synthesize")` already writes deterministic
  `topic_rollup` rows on demand or on a scheduled pass. That closed most of the
  GraphRAG-style "compounding entity page" gap that `llm-wiki-vs-kg.md` flagged,
  and it did so as a *lifecycle action, not an agent* — the right shape.

A heavyweight ontology (RDF/OWL, a reasoner, a process that watches every write)
would also directly violate this project's stated philosophy: high-signal over
high-frequency, with "auto-checkin on every trivial write" listed as a non-goal.
An ontology agent firing on each `store`/`note` is exactly that anti-pattern,
re-dressed.

## The actual problem is the tag space

The one place the model genuinely leaks is the **free-text tag space**. The skill
has to plead with agents — "Check existing tags before inventing new ones... Be
consistent." That is hope, not a mechanism, and it is where the UX friction the
fleet feels actually comes from: an agent searches `postgres`, the discovery it
needed was filed under `PostgreSQL`, the search comes back thin, and the agent
duplicates work that was already done. Every fragmented tag is a future
cache miss.

Break that fragmentation into its real components and most of it is not semantics
at all:

### It's mostly formatting (the cheap 70–80%)

- casing — `Postgres` / `postgres` / `PostgreSQL`
- separators — `pool-connection-leak` / `pool_connection_leak` / `poolConnectionLeak`
- whitespace, trailing punctuation, simple plurals

This is fixable with a deterministic **normalizer**: lowercase → trim →
hyphenate-separators → a tiny hardcoded spelling map (`postgresql → postgres`).
No ontology, no LLM, no agent. It can run at write time *and* as a one-time
backfill, and it kills the large majority of the fragmentation for near-zero
cost.

### A small semantic residue (curated, not inferred)

What survives normalization is true synonymy: `db` vs `database`, `auth` vs
`identity`, `perf` vs `latency`. The fix is a **short, hand-curated alias map** —
a human-decided table, kept deliberately small. Not an inference engine.

### Entity resolution (documented, unbuilt)

"Is *this* bug the same as *that* one?" is the only genuinely hard part, and it
is the part to *not* build until a concrete query failure justifies it — the same
YAGNI verdict `llm-wiki-vs-kg.md` reached for temporal edges. Naming it is free;
building it speculatively is not.

## Edges, for completeness

Everything relational currently collapses into `related_to`. "A contradicts B,"
"A caused B," "A depends on B" are indistinguishable to the graph. Richer edge
types are a real capability — and also a YAGNI candidate. Defer them until a
query actually needs to traverse one of those relations and can't. Document the
idea; do not pre-build the vocabulary.

## What to do

Split by where the work lives. This repo is the **client adapter**; the
normalization machinery itself belongs in the `cirwel/unitares` server.

| Fix | Where | When |
|---|---|---|
| Canonical-tag *guidance* — normalize at write time, agreed spellings | this repo, the `knowledge-graph` skill | now |
| Deterministic `normalize()` on write + a one-time backfill | `cirwel/unitares` server | next |
| Short hand-curated synonym/alias map folded into the lifecycle pass | `cirwel/unitares` server | with the above |
| Richer edge types | anywhere | YAGNI — on demonstrated need |
| Entity resolution; ontology system; ontology agent | nowhere | not building |

## Bottom line

There is no need for an ontology system and no need for an ontology agent. The
need that masquerades as "ontology" is tag normalization, and that need is mostly
**formatting** — a `normalize()` function and a ~20-line synonym table. Build the
function, write the table, and leave everything grander documented and unbuilt.
The point, as with the LLM-wiki comparison, is to borrow the narrow useful bit
and decline the machine.
