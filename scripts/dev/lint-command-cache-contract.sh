#!/usr/bin/env bash
# lint-command-cache-contract.sh — enforce S20.1 cache contract in command files
#
# Fails (exit 1) if any plugin `commands/*.md` file teaches the retired
# v1 cache patterns:
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

if [[ $FAIL -eq 0 ]]; then
    echo "[lint] OK — ${#FILES[@]} command file(s) checked"
fi

exit $FAIL
