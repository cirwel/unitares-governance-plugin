#!/usr/bin/env bash
# lint-command-cache-contract.sh — enforce S20.1 cache contract in command files
#
# Fails (exit 1) if any plugin `commands/*.md` file (or CODEX_START.md)
# teaches the retired v1 cache patterns or treats a workspace cache as proof:
#   1. `session_cache.py set session ...` invocations missing a non-empty
#      `--slot` (also `get session` and `reset-milestone`; see below)
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

# Every multi-file grep below also reads /dev/null, so grep prints
# `file:line:content` even when one file is passed; the parsers rely on it.

# Rules 1 and 3, and a slotless reset: every documented
# `session_cache.py set session`, `get session` and `reset-milestone` must name
# a non-empty slot. The helper's own argparse parser and slot normalisation
# judge each invocation, so every spelling the helper accepts (`--slot X`,
# `--slot=X`, an abbreviated option, a repeated option whose last value wins)
# is read the way the helper reads it. An empty value (`--slot=`, `--slot=""`,
# `--slot ''`) is slotless: `set session` then refuses to write, `get session`
# reads the legacy flat session.json, and `reset-milestone` resets the shared
# workspace milestone. `set session --allow-shared` is the substrate-earned
# exception (S20.1). An invocation the parser rejects, or one that runs past
# the end of its line, fails too: the lint cannot see its slot.
if ! python3 - "${FILES[@]}" <<'PY'
import contextlib
import importlib.util
import io
import re
import shlex
import sys
from pathlib import Path

sys.dont_write_bytecode = True
try:
    spec = importlib.util.spec_from_file_location(
        "session_cache_for_lint", Path("scripts/session_cache.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parser = module.build_parser()
    slot_suffix = module._slot_suffix
except Exception as exc:
    print(f"[lint] cannot load scripts/session_cache.py to read --slot values: {exc!r}")
    sys.exit(1)

INVOCATION = re.compile(
    r"session_cache\.py\s+(set\s+session|get\s+session|reset-milestone)\b"
)
SLOTLESS = {
    "set": (
        "`session_cache.py set session` without --slot or --allow-shared",
        "add --slot=<harness-session-id> (or --allow-shared for substrate-earned "
        "single-tenant case)",
    ),
    "get": (
        "`session_cache.py get session` without --slot",
        "discover slot via `session_cache.py list` then read with --slot=<slot> "
        "(S20.2 §3b).",
    ),
    "reset-milestone": (
        "slotless `session_cache.py reset-milestone`",
        "do not teach a manual reset; the post-checkin hook resets the current "
        "slot's milestone.",
    ),
}

failed = False
for name in sys.argv[1:]:
    try:
        lines = Path(name).read_text(encoding="utf-8").splitlines()
    except OSError:
        continue
    for line_no, line in enumerate(lines, 1):
        for match in INVOCATION.finditer(line):
            command = match.group(1).split()[0]
            end = len(line)
            if line.count("`", 0, match.start()) % 2:
                close = line.find("`", match.start())
                end = close if close != -1 else end
            args = None
            try:
                argv = shlex.split(line[match.start():end])[1:]
            except ValueError:
                argv = None
            if argv is not None:
                with contextlib.redirect_stderr(io.StringIO()):
                    try:
                        args = parser.parse_args(argv)
                    except SystemExit:
                        args = None
            if args is None:
                label = f"`session_cache.py {command}` that the helper's parser rejects"
                fix = "write the whole command in one code span on one line."
            elif command == "set" and args.allow_shared:
                continue
            elif slot_suffix(args.slot):
                continue
            else:
                label, fix = SLOTLESS[command]
            print(f"[lint] {name}:{line_no} — {label}")
            print(f"       offending: {line}")
            print(f"       fix: {fix}")
            failed = True
sys.exit(1 if failed else 0)
PY
then
    FAIL=1
fi

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

# Rule 4: a slot read is lineage context, never the current process's proof.
# Every `get session` line must carry the marker "lineage context only" and
# must not mention `client_session_id`, so a reader cannot take a session id
# from another slot's cache. It applies whatever spelling names the slot. This is a line-local text check: it catches
# the retired phrasings, not every way prose could mislead.
while IFS= read -r match; do
    [[ -z "$match" ]] && continue
    file=${match%%:*}
    line=${match#*:}
    line_no=${line%%:*}
    content=${line#*:}
    if [[ "$content" == *"lineage context only"* ]] && [[ "$content" != *"client_session_id"* ]]; then
        continue
    fi
    echo "[lint] ${file}:${line_no} — \`session_cache.py get session --slot\` not marked lineage-only"
    echo "       offending: ${content}"
    echo "       fix: say on the same line that the slot read is \"lineage context only\", and keep \`client_session_id\` off that line."
    FAIL=1
done < <(grep -nE 'session_cache\.py[[:space:]]+get[[:space:]]+session' "${FILES[@]}" /dev/null 2>/dev/null || true)

# Rule 5: retired guidance that steers an agent into adopting a workspace
# cache as its own session: treating the cache as the place to find a
# `client_session_id`, or picking a slot by recency.
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

if [[ $FAIL -eq 0 ]]; then
    echo "[lint] OK — ${#FILES[@]} command file(s) checked"
fi

exit $FAIL
