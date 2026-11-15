: << 'CMDBLOCK'
@echo off
setlocal EnableDelayedExpansion
REM Cross-platform polyglot wrapper for hook scripts.
if "%~1"=="" (
    echo run-hook.cmd: missing script name >&2
    exit /b 1
)
set "HOOK_DIR=%~dp0"
REM Eval isolation, mirrored from the Unix branch below: see the comment there.
if "%EVAL_UNITARES_OFFLINE%"=="1" (
    set "UNITARES_SERVER_URL=http://127.0.0.1:9"
    set "UNITARES_LEASE_PLANE_URL=http://127.0.0.1:9"
    set "LEASE_PLANE_BASE_URL=http://127.0.0.1:9"
    set "UNITARES_SIDECAR_URL=http://127.0.0.1:9"
    set "UNITARES_AUTO_ONBOARD=off"
    set "UNITARES_DISABLE_AUTO_ONBOARD=1"
    set "UNITARES_CHECKINS=off"
    set "UNITARES_FILE_LEASES_ENABLED=0"
    set "UNITARES_FILE_LEASES_REQUIRED=0"
)
if exist "C:\Program Files\Git\bin\bash.exe" (
    "C:\Program Files\Git\bin\bash.exe" "%HOOK_DIR%run-hook.cmd" "%~1" %2 %3 %4 %5 %6 %7 %8 %9
    exit /b !ERRORLEVEL!
)
if exist "C:\Program Files (x86)\Git\bin\bash.exe" (
    "C:\Program Files (x86)\Git\bin\bash.exe" "%HOOK_DIR%run-hook.cmd" "%~1" %2 %3 %4 %5 %6 %7 %8 %9
    exit /b !ERRORLEVEL!
)
echo run-hook.cmd: Git Bash is required to run UNITARES hooks >&2
if /I "%~1"=="pre-edit" (
    if /I "%UNITARES_FILE_LEASES_REQUIRED%"=="1" goto required_lease_bash
    if /I "%UNITARES_FILE_LEASES_REQUIRED%"=="true" goto required_lease_bash
    if /I "%UNITARES_FILE_LEASES_REQUIRED%"=="on" goto required_lease_bash
    if /I "%UNITARES_FILE_LEASES_REQUIRED%"=="yes" goto required_lease_bash
)
exit /b 1
:required_lease_bash
echo {"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"UNITARES required file leases need Git Bash on Windows."}}
exit /b 0
CMDBLOCK

# Unix: hooks are an automatic security boundary. Do not inherit executable
# search prefixes from the editor/session: a workspace-writable directory in
# PATH must never supply python3, git, curl, bash, or another hook utility.
PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
# `claude plugin eval` strips every env var except EVAL_*, so the usual
# UNITARES_* opt-outs cannot reach an eval child, while its hooks still run
# as the operator against localhost. EVAL_UNITARES_OFFLINE=1 turns off every
# network path: the kill switches (check-ins, lazy onboarding, file leases)
# plus a closed port for each endpoint. config/defaults.env only fills unset
# values, so these survive the hooks sourcing it. LEASE_PLANE_BASE_URL is set
# as well as UNITARES_LEASE_PLANE_URL because file_lease_hook.py prefers it.
if [ "${EVAL_UNITARES_OFFLINE:-}" = "1" ]; then
    UNITARES_SERVER_URL="http://127.0.0.1:9"
    UNITARES_LEASE_PLANE_URL="http://127.0.0.1:9"
    LEASE_PLANE_BASE_URL="http://127.0.0.1:9"
    UNITARES_SIDECAR_URL="http://127.0.0.1:9"
    UNITARES_AUTO_ONBOARD=off
    UNITARES_DISABLE_AUTO_ONBOARD=1
    UNITARES_CHECKINS=off
    UNITARES_FILE_LEASES_ENABLED=0
    UNITARES_FILE_LEASES_REQUIRED=0
    export UNITARES_SERVER_URL UNITARES_LEASE_PLANE_URL LEASE_PLANE_BASE_URL \
        UNITARES_SIDECAR_URL UNITARES_AUTO_ONBOARD UNITARES_DISABLE_AUTO_ONBOARD \
        UNITARES_CHECKINS UNITARES_FILE_LEASES_ENABLED UNITARES_FILE_LEASES_REQUIRED
fi
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_NAME="$1"
shift
# Dispatch sets this to the PID of the CLI it launches. A nested CLI inherits
# the thread anchor but has a different hook host PID; it must not resume the
# dispatch turn's identity. Keep the check here so SessionStart, PreToolUse and
# Stop all see the same process-scoped environment. An unmarked anchor also
# fails closed; older launchers must add the owner marker before upgrading.
# A launcher that deliberately declares lineage for a nested CLI must clear
# the inherited anchor/owner first. Otherwise inherited and newly declared
# lineage are indistinguishable here, so both are discarded.
UNITARES_HOOK_HOST_PID="$PPID"
export UNITARES_HOOK_HOST_PID
if { [ -n "${UNITARES_CLIENT_SESSION_ID:-}" ] \
        || [ -n "${UNITARES_ORCHESTRATED_OWNER_PID:-}" ]; } \
    && [ "$UNITARES_HOOK_HOST_PID" != "${UNITARES_ORCHESTRATED_OWNER_PID:-}" ]; then
    UNITARES_ANCHOR_IGNORED=1
    export UNITARES_ANCHOR_IGNORED
    unset UNITARES_CLIENT_SESSION_ID UNITARES_ORCHESTRATED \
        UNITARES_ORCHESTRATED_OWNER_PID UNITARES_PARENT_AGENT_ID UNITARES_SPAWN_REASON
fi
if [ -x /bin/bash ]; then
    BASH_BIN=/bin/bash
elif [ -x /usr/bin/bash ]; then
    BASH_BIN=/usr/bin/bash
else
    BASH_BIN=""
fi
if [ -z "$BASH_BIN" ]; then
    echo "run-hook.cmd: Bash is required to run UNITARES hooks" >&2
    exit 1
fi
HOOK_HOST=""
PREVIOUS_ARG=""
for ARG in "$@"; do
    if [ "$PREVIOUS_ARG" = "--host" ]; then
        HOOK_HOST="$ARG"
    fi
    PREVIOUS_ARG="$ARG"
done
REPORTER="${SCRIPT_DIR}/../scripts/hook_invocations.py"
if [ "$HOOK_HOST" = "codex" ] && [ -f "$REPORTER" ] && command -v python3 >/dev/null 2>&1; then
    # The child used to be exec'd directly, so its PPID was the hook host.
    # Keep that PID available across the reporting process boundary.
    exec python3 "$REPORTER" run --plugin-root "${SCRIPT_DIR}/.." \
        --hook "$SCRIPT_NAME" -- "$BASH_BIN" "${SCRIPT_DIR}/${SCRIPT_NAME}" "$@"
fi
# Claude runs the Stop hook asynchronously and kills it when the CLI process
# exits. A one-shot `claude -p` turn exits within a second of Stop, so the
# hook never reached its check-in: orchestrated sessions (one process per
# turn) recorded nothing and were never lazily onboarded. Read the payload
# now, hand it to a child that ignores the exit signals, and return at once.
# UNITARES_STOP_DETACH=0 keeps the hook in the foreground. Codex and agy run
# Stop synchronously and need its stdout, so they are left alone.
#
# The ignored signals are inherited by the hook and its helpers, so nothing
# short of KILL stops one that hangs, and the host no longer ends it at exit.
# A watchdog therefore kills the hook's process tree after
# UNITARES_STOP_HOOK_LIMIT_S seconds (default 120; the hook's own onboard and
# check-in budgets add up to well under that).
if [ "$SCRIPT_NAME" = "post-stop" ] && [ "$HOOK_HOST" = "claude" ] \
        && [ "${UNITARES_STOP_DETACH:-1}" != "0" ]; then
    STOP_PAYLOAD="$(cat 2>/dev/null || true)"
    STOP_LIMIT="${UNITARES_STOP_HOOK_LIMIT_S:-120}"
    case "$STOP_LIMIT" in
        ''|*[!0-9]*) STOP_LIMIT=120 ;;
    esac
    (
        trap '' HUP TERM INT
        kill_tree() {
            for child in $(pgrep -P "$1" 2>/dev/null); do
                kill_tree "$child"
            done
            kill -KILL "$1" 2>/dev/null
        }
        printf '%s' "$STOP_PAYLOAD" | "$BASH_BIN" "${SCRIPT_DIR}/${SCRIPT_NAME}" "$@" &
        HOOK_PID=$!
        ( sleep "$STOP_LIMIT"; kill_tree "$HOOK_PID" ) &
        WATCHDOG_PID=$!
        wait "$HOOK_PID"
        kill_tree "$WATCHDOG_PID"
    ) </dev/null >/dev/null 2>&1 &
    exit 0
fi
exec "$BASH_BIN" "${SCRIPT_DIR}/${SCRIPT_NAME}" "$@"
