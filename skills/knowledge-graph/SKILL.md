---
name: knowledge-graph
description: >
  Use when an agent needs to search the shared knowledge graph, contribute a discovery,
  or update existing entries. Covers search, tagging, discovery types, and status lifecycle.
license: Apache-2.0
compatibility: Requires UNITARES governance MCP server (gov.cirwel.org or local http://127.0.0.1:8767/mcp/)
metadata:
  unitares.last_verified: "2026-05-26"
  unitares.freshness_days: "14"
---

# Knowledge Graph

## What It Is

The knowledge graph is shared institutional memory across all agents. It is backed by PostgreSQL with Apache AGE for graph queries. Every agent can search, contribute, and update entries. Discoveries persist across sessions and are available to all agents in the system.

## Search Before Creating

Always search before adding new entries:

```
knowledge(
  action: "search",
  query: "description of what you're looking for",
  tags: ["relevant", "tags"],
  limit: 10
)
```

The runtime may still expose older search aliases, but prefer the unified `knowledge(action="search")` path when available. Duplicate entries fragment knowledge and make search less effective.

## Quick Contribution

For low-friction contributions, use the unified note action:

```
knowledge(
  action: "note",
  summary: "What you discovered or observed",
  tags: ["domain", "type", "context"]
)
```

Notes are automatically shared with all agents. Use this when you find something useful, spot a bug, or have an insight that others should know about.

`leave_note()` is a deprecated compatibility alias for `knowledge(action="note")`. Calls still work on servers that expose it, but new guidance and client adapters should use `knowledge(action=...)`.

## Full CRUD Operations

For more control, use the `knowledge()` tool with an action parameter:

| Action | Purpose |
|--------|---------|
| `store` | Create a new discovery with full metadata |
| `search` | Search by query, tags, or both |
| `get` | Get knowledge for a specific agent |
| `list` | List graph statistics or summary views |
| `update` | Modify an existing discovery (status, content, tags) |
| `details` | Get full details including graph relationships |
| `note` | Quick low-ceremony shared note |
| `cleanup` | Run lifecycle cleanup for stale entries |
| `stats` | Get knowledge graph statistics |
| `supersede` | Mark an older discovery as replaced by a newer one |
| `audit` | Run a knowledge graph lifecycle audit |

## Discovery Types

When storing a discovery, classify it:

| Type | When to Use |
|------|-------------|
| `note` | General observation, context, or reminder |
| `insight` | Understanding gained from analysis or pattern recognition |
| `bug_found` | A bug or defect you identified |
| `improvement` | A suggestion for how something could be better |
| `pattern` | A recurring pattern you noticed across multiple instances |
| `question` | An open question that needs an answer or follow-up |
| `architectural_decision`, `learning`, `rule` | Durable knowledge that should rarely be auto-archived |
| `experiment`, `exploration`, `observation` | Investigation notes where the conclusion may evolve |
| `bug_fix`, `refactoring`, `documentation` | Implementation or maintenance work already performed |

Check the live tool schema if you are unsure which enum values the current runtime accepts. Do not invent discovery types casually.

## Status Lifecycle

Every discovery has a status:

```
open  -->  resolved  -->  archived  -->  cold
  \-->  superseded
  \-->  closed / wont_fix / disputed
```

- **open**: Active, still relevant, may need attention
- **resolved**: The issue or finding has been addressed
- **archived**: No longer relevant (outdated, superseded, or duplicate)
- **superseded**: Replaced by a newer discovery; prefer the newer entry
- **closed** / **wont_fix** / **disputed**: Explicit operator or agent disposition
- **cold**: Long-term storage managed by lifecycle cleanup; list/stats surfaces use `including_cold=true` when cold rows should be included

Use `knowledge(action="supersede")` or store with a `supersedes` target when replacing older non-permanent entries. Permanent entries require explicit operator action to change.

## Backend Drift and Rehydration

The AGE graph is backed by durable PostgreSQL knowledge tables. On startup, the AGE backend compares graph rows with PostgreSQL rows:

- If AGE is empty and PostgreSQL has rows, it fully rehydrates AGE from PostgreSQL.
- If AGE has fewer rows than PostgreSQL, it rehydrates only the missing rows and related edges.
- If AGE has more rows than PostgreSQL, it warns for operator review instead of guessing.

Writes also tolerate graph/table drift by dropping `related_to` edges whose destination discovery is missing, instead of failing the whole write. For agents, the operational rule is simple: if a search result looks incomplete, retry or inspect with `knowledge(action="details")`, `knowledge(action="list")`, or `knowledge(action="audit")`; do not create a duplicate merely because one backend was briefly out of sync.

## Tagging Best Practices

Tags are how future agents find your contributions. Be intentional:

- **Include the domain**: `identity`, `database`, `performance`, `deployment`, `testing`
- **Include the type**: `bug`, `insight`, `pattern`, `config`, `dependency`
- **Include context**: `postgres`, `eisv`, `dialectic`, `discord-bridge`
- **Be specific**: `pool-connection-leak` is more useful than `bug`
- **Be consistent**: Check existing tags before inventing new ones

## Closing the Loop

The graph accumulates knowledge well but does not close loops automatically. This is a known gap that every agent should help address:

- **When you resolve something, update its status.** Do not leave it as `open`.
- **When you find a duplicate, archive the less complete one** and reference the better entry.
- **When a finding is outdated, archive it** with a note about what superseded it.
- **Periodically check for stale entries** in your domain using `knowledge(action="cleanup")`.

Unresolved entries create noise. Closed loops create trust in the graph.
