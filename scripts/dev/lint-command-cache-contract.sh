#!/usr/bin/env bash
# lint-command-cache-contract.sh — enforce S20.1 cache contract in command files
#
# Fails (exit 1) if any plugin `commands/*.md` file (or CODEX_START.md)
# teaches the retired v1 cache patterns or treats a workspace cache as proof:
#   1. `session_cache.py set session ...` invocations missing `--slot`
#      → would write the flat session.json that the post-PR-19 hook
#        deliberately ignores. This is the original S11-a regression.
#   2. Persisting `continuity_token` (the field, not the boolean
#      `continuity_token_supported` flag) inside a cache write payload
#      list. The v2 schema is lineage-only; the field belongs only in
#      deprecation/legacy framing.
#
# Allowed: discussion of `continuity_token` as a deprecated/legacy
# concept (e.g., diagnose.md's "PATH 0 advanced edge path" framing).
# The lint targets *teaching the agent to write it*, not naming it.
#
# Usage:
#   ./scripts/dev/lint-command-cache-contract.sh
#   ./scripts/dev/lint-command-cache-contract.sh commands/foo.md   # one file
#
# Plan-doc context: §6 in the
# unitares repo names this as the regression-prevention test.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

FILES=()
if [[ $# -gt 0 ]]; then
    FILES=("$@")
else
    while IFS= read -r f; do
        FILES+=("$f")
    done < <(find commands -name '*.md' -type f)
    # CODEX_START.md teaches the same cache helpers to Codex agents.
    [[ -f CODEX_START.md ]] && FILES+=("CODEX_START.md")
fi

FAIL=0

# Rule 1: every `session_cache.py set session ...` line must include --slot
# (allow `--allow-shared` as the substrate-earned escape hatch from S20.1).
while IFS= read -r match; do
    [[ -z "$match" ]] && continue
    file=${match%%:*}
    line=${match#*:}
    line_no=${line%%:*}
    content=${line#*:}
    if [[ "$content" == *"--slot"* ]] || [[ "$content" == *"--allow-shared"* ]]; then
        continue
    fi
    echo "[lint] ${file}:${line_no} — \`session_cache.py set session\` without --slot or --allow-shared"
    echo "       offending: ${content}"
    echo "       fix: add --slot=<harness-session-id> (or --allow-shared for substrate-earned single-tenant case)"
    FAIL=1
done < <(grep -nE 'session_cache\.py +set +session' "${FILES[@]}" 2>/dev/null || true)

# Rule 2: bullet-list persistence of `continuity_token` (the bare field, not the _supported flag).
# Pattern: a markdown bullet like `  - \`continuity_token\`` (i.e., the field is named in a list of
# fields to persist). Flag if the file ALSO contains a `set session ...` line in the same file —
# that's the strong signal this is a write-side persistence list, not a discussion.
for file in "${FILES[@]}"; do
    if ! grep -qE 'session_cache\.py +set +session' "$file" 2>/dev/null; then
        # No write-side context — token name is being discussed, not persisted. Skip.
        continue
    fi
    while IFS= read -r match; do
        [[ -z "$match" ]] && continue
        line_no=${match%%:*}
        content=${match#*:}
        echo "[lint] ${file}:${line_no} — persists \`continuity_token\` in cache write payload"
        echo "       offending: ${content}"
        echo "       fix: drop the field. v2 cache schema is lineage-only (S11/S20). Do not cache \`continuity_token_supported\` either (S20.3); read it from the live response."
        FAIL=1
    done < <(grep -nE '^\s*-\s+\`continuity_token\`\s*$' "$file" 2>/dev/null || true)
done

# Rule 3 (S20.2): bare `session_cache.py get session` reads the legacy flat
# session.json — the same anti-pattern Rule 1 catches on the write side.
# Commands must read via `--slot=<id>` or first call `list` to discover the
# slot. Allow `get` of other kinds (e.g. milestone) and any `get session`
# invocation that includes `--slot`.
while IFS= read -r match; do
    [[ -z "$match" ]] && continue
    file=${match%%:*}
    line=${match#*:}
    line_no=${line%%:*}
    content=${line#*:}
    if [[ "$content" == *"--slot"* ]]; then
        continue
    fi
    echo "[lint] ${file}:${line_no} — \`session_cache.py get session\` without --slot"
    echo "       offending: ${content}"
    echo "       fix: discover slot via \`session_cache.py list\` then read with --slot=<slot> (S20.2 §3b)."
    FAIL=1
done < <(grep -nE 'session_cache\.py +get +session' "${FILES[@]}" 2>/dev/null || true)

# Rule 4: a slot read is lineage context, never the current process's proof.
# Every `get session --slot` line must say so on the same line, so a reader
# cannot take a `client_session_id` from another slot's cache.
while IFS= read -r match; do
    [[ -z "$match" ]] && continue
    file=${match%%:*}
    line=${match#*:}
    line_no=${line%%:*}
    content=${line#*:}
    if [[ "$content" != *"--slot"* ]] || [[ "$content" == *"lineage"* ]]; then
        continue
    fi
    echo "[lint] ${file}:${line_no} — \`session_cache.py get session --slot\` not marked lineage-only"
    echo "       offending: ${content}"
    echo "       fix: say on the same line that the slot read is lineage context only, not identity proof."
    FAIL=1
done < <(grep -nE 'session_cache\.py +get +session' "${FILES[@]}" 2>/dev/null || true)

# Rule 5: retired guidance that steers an agent into adopting a workspace
# cache as its own session: treating the cache as the place to find a
# `client_session_id`, picking a slot by recency, or resetting a milestone
# without naming the slot (a slotless reset writes the shared workspace file).
check_phrase() {
    local pattern="$1"
    local reason="$2"
    while IFS= read -r match; do
        [[ -z "$match" ]] && continue
        echo "[lint] ${match%%:*}:$(cut -d: -f2 <<<"$match") — ${reason}"
        echo "       offending: $(cut -d: -f3- <<<"$match")"
        FAIL=1
    done < <(grep -inE -- "$pattern" "${FILES[@]}" /dev/null 2>/dev/null || true)
}
check_phrase 'first place to look' \
    "workspace caches are lineage hints, not where to find this process's client_session_id"
check_phrase 'pick the most recent' \
    "recency of a workspace cache is not identity or lineage proof"
while IFS= read -r match; do
    [[ -z "$match" ]] && continue
    content=$(cut -d: -f3- <<<"$match")
    [[ "$content" == *"--slot"* ]] && continue
    echo "[lint] ${match%%:*}:$(cut -d: -f2 <<<"$match") — slotless \`session_cache.py reset-milestone\`"
    echo "       offending: ${content}"
    echo "       fix: do not teach a manual reset; the post-checkin hook resets the current slot's milestone."
    FAIL=1
done < <(grep -nE 'session_cache\.py +reset-milestone' "${FILES[@]}" /dev/null 2>/dev/null || true)

if [[ $FAIL -eq 0 ]]; then
    echo "[lint] OK — ${#FILES[@]} command file(s) checked"
fi

exit $FAIL
