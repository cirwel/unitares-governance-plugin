#!/usr/bin/env bash
# Advisory local diff review helper for unitares-governance-plugin.
#
# Locates a sibling unitares checkout's scripts/dev/review.sh when present (for
# reference/delegation) and invokes a configured cross-family review CLI
# (claude, codex, gemini, or antigravity) cleanly against a diff base
# (default: origin/master). Strictly advisory: never mutates the working tree,
# never posts PR comments automatically, and does not alter CI gates.
#
# Usage:
#   ./scripts/dev/review.sh --reviewer claude [--base origin/master] [--model MODEL]
#   ./scripts/dev/review.sh --reviewer codex  [--base origin/master] [--model MODEL]
#   ./scripts/dev/review.sh --reviewer gemini [--base origin/master] [--model MODEL]
#   ./scripts/dev/review.sh --reviewer antigravity [--base origin/master] [--model MODEL]
#   ./scripts/dev/review.sh --dry-run [--reviewer NAME] [--base REF]

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

usage() {
    cat <<'EOF'
usage: ./scripts/dev/review.sh --reviewer <claude|codex|gemini|antigravity> [options]

Advisory diff review helper for unitares-governance-plugin.

Options:
  --reviewer NAME   Explicit reviewer CLI to run: claude, codex, gemini, antigravity
                    (or set UNITARES_REVIEWER). No implicit provider is chosen.
  --base REF        Git diff base ref (default: origin/master, fallback: master).
  --model MODEL     Optional model identifier passed through to the reviewer CLI.
  --dry-run         Show resolved base, diff summary, sibling unitares helper,
                    and command without invoking any external CLI.
  -h, --help        Show this help text.

Exit codes:
  0  Review completed, diff is empty, or --dry-run / --help succeeded.
  2  Missing reviewer choice, invalid option, or unavailable reviewer CLI.
EOF
}

die() {
    echo "[review] $*" >&2
    exit 2
}

discover_sibling_unitares_review() {
    local candidate=""
    if [[ -n "${UNITARES_REVIEW_HELPER:-}" && -x "${UNITARES_REVIEW_HELPER}" ]]; then
        printf '%s\n' "${UNITARES_REVIEW_HELPER}"
        return 0
    fi
    local common_dir
    common_dir="$(git rev-parse --git-common-dir 2>/dev/null || true)"
    for candidate in \
        "${PROJECT_ROOT}/../unitares/scripts/dev/review.sh" \
        "${common_dir:+${common_dir}/../../unitares/scripts/dev/review.sh}"
    do
        if [[ -n "${candidate}" && -x "${candidate}" ]]; then
            (cd "$(dirname "${candidate}")" && printf '%s/%s\n' "$(pwd)" "$(basename "${candidate}")")
            return 0
        fi
    done
    return 1
}

REVIEWER="${UNITARES_REVIEWER:-}"
BASE=""
MODEL=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --reviewer)
            [[ $# -ge 2 && -n "${2:-}" ]] || die "--reviewer requires a value"
            REVIEWER="$2"
            shift 2
            ;;
        --base)
            [[ $# -ge 2 && -n "${2:-}" ]] || die "--base requires a git ref"
            BASE="$2"
            shift 2
            ;;
        --model)
            [[ $# -ge 2 && -n "${2:-}" ]] || die "--model requires a value"
            MODEL="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1 (run with --help)"
            ;;
    esac
done

if [[ -z "${BASE}" ]]; then
    if git rev-parse --verify --quiet origin/master >/dev/null 2>&1; then
        BASE="origin/master"
    elif git rev-parse --verify --quiet master >/dev/null 2>&1; then
        BASE="master"
    else
        die "cannot resolve default base ref (origin/master or master); pass --base REF"
    fi
else
    git rev-parse --verify --quiet "${BASE}" >/dev/null 2>&1 \
        || die "base ref not found: ${BASE}"
fi

SIBLING_HELPER="$(discover_sibling_unitares_review || true)"

# Prefer committed branch delta (BASE...HEAD); fall back to working-tree diff
# against BASE when HEAD has no commits ahead of BASE.
DIFF_MODE="${BASE}...HEAD"
DIFF_STAT="$(git diff --stat "${DIFF_MODE}" 2>/dev/null || true)"
if [[ -z "${DIFF_STAT}" ]]; then
    DIFF_MODE="${BASE}"
    DIFF_STAT="$(git diff --stat "${DIFF_MODE}" 2>/dev/null || true)"
fi

if [[ "${DRY_RUN}" == true ]]; then
    echo "[review] repo: ${PROJECT_ROOT}"
    echo "[review] base: ${BASE} (diff spec: ${DIFF_MODE})"
    echo "[review] sibling unitares review.sh: ${SIBLING_HELPER:-not found}"
    echo "[review] reviewer: ${REVIEWER:-<unset>}${MODEL:+ (model=${MODEL})}"
    if [[ -z "${DIFF_STAT}" ]]; then
        echo "[review] diff: empty"
    else
        echo "[review] diff stat:"
        printf '%s\n' "${DIFF_STAT}"
    fi
    exit 0
fi

if [[ -z "${REVIEWER}" ]]; then
    avail=()
    for cli in claude codex gemini agy; do
        command -v "${cli}" >/dev/null 2>&1 && avail+=("${cli}")
    done
    echo "[review] no reviewer selected; pass --reviewer <claude|codex|gemini|antigravity> or set UNITARES_REVIEWER" >&2
    if [[ ${#avail[@]} -gt 0 ]]; then
        echo "[review] available CLIs on PATH: ${avail[*]}" >&2
    else
        echo "[review] no supported review CLIs found on PATH" >&2
    fi
    if [[ -n "${SIBLING_HELPER}" ]]; then
        echo "[review] sibling unitares review helper located at: ${SIBLING_HELPER}" >&2
    fi
    exit 2
fi

if [[ -z "${DIFF_STAT}" ]]; then
    echo "[review] empty diff against ${BASE}; nothing to review"
    exit 0
fi

REVIEW_PROMPT="Review this unitares-governance-plugin diff against ${BASE}. Focus on correctness, identity/slot safety, hook I/O contracts, security boundaries, and regressions. List concrete findings with file and line references, or reply CLEAN if none."

build_input() {
    printf '%s\n\n' "${REVIEW_PROMPT}"
    printf '=== git diff --stat %s ===\n' "${DIFF_MODE}"
    git diff --stat "${DIFF_MODE}"
    printf '\n=== git diff %s ===\n' "${DIFF_MODE}"
    git diff "${DIFF_MODE}"
}

case "${REVIEWER}" in
    claude)
        command -v claude >/dev/null 2>&1 || die "claude CLI not found on PATH"
        # Enforce read-only review posture: plan permission mode, read-only
        # built-in tools, and MCP servers disabled so no write-capable MCP
        # tools can execute on an untrusted diff.
        CMD=(
            claude -p
            --permission-mode plan
            --tools Read,Grep,Glob
            --strict-mcp-config
            --mcp-config '{"mcpServers":{}}'
            --disallowedTools 'mcp__*'
        )
        [[ -n "${MODEL}" ]] && CMD+=(--model "${MODEL}")
        build_input | "${CMD[@]}"
        ;;
    codex)
        command -v codex >/dev/null 2>&1 || die "codex CLI not found on PATH"
        # Enforce read-only sandbox for untrusted diffs.
        CMD=(codex exec -s read-only)
        [[ -n "${MODEL}" ]] && CMD+=(-m "${MODEL}")
        build_input | "${CMD[@]}" -
        ;;
    gemini)
        command -v gemini >/dev/null 2>&1 || die "gemini CLI not found on PATH"
        # Fail closed unless the installed gemini CLI advertises explicit
        # read-only enforcement (--approval-mode plan + --sandbox).
        GEMINI_HELP="$(gemini --help 2>&1 || true)"
        if grep -q -- '--approval-mode' <<<"${GEMINI_HELP}" \
            && grep -q -- '--sandbox' <<<"${GEMINI_HELP}"; then
            CMD=(gemini -p "${REVIEW_PROMPT}" --approval-mode plan --sandbox)
            [[ -n "${MODEL}" ]] && CMD+=(-m "${MODEL}")
            git diff "${DIFF_MODE}" | "${CMD[@]}"
        else
            die "gemini CLI does not advertise verified read-only flags (--approval-mode plan + --sandbox); refusing to run unrestricted tools on untrusted diff (use --reviewer antigravity, claude, or codex)"
        fi
        ;;
    antigravity|agy)
        command -v agy >/dev/null 2>&1 || die "agy CLI not found on PATH"
        # Fail closed unless this agy binary advertises --mode and --sandbox.
        AGY_HELP="$(agy --help 2>&1 || true)"
        if grep -q -- '--mode' <<<"${AGY_HELP}" \
            && grep -q -- '--sandbox' <<<"${AGY_HELP}"; then
            CMD=(agy -p --mode plan --sandbox --disable-slash-commands)
            [[ -n "${MODEL}" ]] && CMD+=(--model "${MODEL}")
            build_input | "${CMD[@]}"
        else
            die "agy CLI does not advertise verified read-only flags (--mode plan + --sandbox); refusing to run unrestricted tools on untrusted diff"
        fi
        ;;
    *)
        die "unsupported reviewer: ${REVIEWER} (expected claude, codex, gemini, or antigravity)"
        ;;
esac
