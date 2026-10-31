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
    exec python3 "$REPORTER" run --plugin-root "${SCRIPT_DIR}/.." \
        --hook "$SCRIPT_NAME" -- "$BASH_BIN" "${SCRIPT_DIR}/${SCRIPT_NAME}" "$@"
fi
exec "$BASH_BIN" "${SCRIPT_DIR}/${SCRIPT_NAME}" "$@"
