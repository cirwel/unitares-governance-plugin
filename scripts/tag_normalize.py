#!/usr/bin/env python3
"""Deterministic tag normalization for knowledge-graph MCP calls.

Tag fragmentation in the shared knowledge graph is overwhelmingly a
*formatting* problem: ``Postgres``, ``postgres``, and ``PostgreSQL`` are one
tag filed three ways, and every variant is a future search miss. This module
canonicalizes tags at the client write-time chokepoint (the pre-governance
PreToolUse hook) so they land consistently regardless of whether the server
also normalizes on write.

Scope is intentionally narrow and deterministic — casing, separators,
surrounding punctuation, de-duplication, and a tiny hand-curated map of pure
*spelling* variants. It deliberately does NOT do:

- plural stripping (too lossy: ``metrics``->``metric``, ``kubernetes``->
  ``kubernete``), or
- semantic synonym merging (``auth`` <-> ``identity``).

Those are curation / entity-resolution concerns that belong in the server
lifecycle pass, not a write-time formatter. See docs/ontology-need.md.

Fail-open by construction: every public function returns the input unchanged
on anything it cannot confidently normalize.
"""

from __future__ import annotations

import re
from typing import Any

# Suffixes of governance MCP tools whose schemas carry a `tags` list.
# Restricting to these keeps the formatter off unrelated governance calls
# that share the pre-governance hook.
TAG_BEARING_SUFFIXES = frozenset({"knowledge", "search_shared_memory", "leave_note"})

# Pure spelling-variant canonicalization, applied AFTER formatting. Keys are
# already in normalized (lowercased, hyphenated) form. Keep this small and
# unambiguous: semantic synonyms do not belong here, and anything ambiguous
# (e.g. ``pg``, ``k8s``) is left alone on purpose. One-off singular/plural
# fixes are allowed only when the canonical tag is obvious.
_CANONICAL = {
    "postgresql": "postgres",
    "postgre": "postgres",
    "residents": "resident",
}

# A run of anything that is not a lowercase letter or digit becomes one
# hyphen. Applied after lowercasing, so this folds spaces, underscores,
# dots, slashes, and repeated hyphens into a single separator.
_NON_ALNUM_RUN = re.compile(r"[^a-z0-9]+")


def normalize_tag(raw: Any) -> str:
    """Canonicalize a single tag. Returns '' for unusable input."""
    if not isinstance(raw, str):
        return ""
    s = raw.strip().lower()
    if not s:
        return ""
    s = _NON_ALNUM_RUN.sub("-", s)
    s = s.strip("-")
    if not s:
        return ""
    return _CANONICAL.get(s, s)


def normalize_tag_list(tags: Any) -> list | None:
    """Normalize a list of tags: canonicalize, drop empties, de-dup in order.

    Returns the new list only when it differs from the input; returns None
    when there is nothing to change (or the input is not a list of values we
    should touch), so callers can cheaply detect a no-op.
    """
    if not isinstance(tags, list):
        return None
    out: list = []
    seen: set = set()
    for item in tags:
        norm = normalize_tag(item)
        if not norm:
            # Drop unusable/empty entries, but never silently discard a
            # non-string payload we don't understand — bail out instead.
            if not isinstance(item, str):
                return None
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out if out != tags else None


def normalize_call_tags(tool_input: dict) -> bool:
    """Normalize the `tags` field of a governance call in place.

    Returns True iff `tool_input` was modified. Safe to call on any dict;
    it only acts when a `tags` list is present and actually changes.
    """
    if not isinstance(tool_input, dict):
        return False
    if "tags" not in tool_input:
        return False
    normalized = normalize_tag_list(tool_input.get("tags"))
    if normalized is None:
        return False
    tool_input["tags"] = normalized
    return True
