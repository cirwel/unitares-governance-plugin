: << 'CMDBLOCK'
@echo off
setlocal EnableDelayedExpansion
REM Cross-platform polyglot wrapper for hook scripts.
if "%~1"=="" (
    echo run-hook.cmd: missing script name >&2
    exit /b 1
)
set "HOOK_DIR=%~dp0"
if exist "C:\Program Files\Git\bin\bash.exe" (
    "C:\Program Files\Git\bin\bash.exe" "%HOOK_DIR%%~1" %2 %3 %4 %5 %6 %7 %8 %9
    exit /b !ERRORLEVEL!
)
if exist "C:\Program Files (x86)\Git\bin\bash.exe" (
    "C:\Program Files (x86)\Git\bin\bash.exe" "%HOOK_DIR%%~1" %2 %3 %4 %5 %6 %7 %8 %9
    exit /b !ERRORLEVEL!
)
where bash >nul 2>nul
if !ERRORLEVEL! equ 0 (
    bash "%HOOK_DIR%%~1" %2 %3 %4 %5 %6 %7 %8 %9
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

# Unix: run the named script directly
PATH="${PATH:+${PATH}:}/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
# `claude plugin eval` strips every env var except EVAL_*, so the usual
# UNITARES_* opt-outs cannot reach an eval child, while its hooks still run
# as the operator against localhost. EVAL_UNITARES_OFFLINE=1 points every
# endpoint at a closed port and disables lazy onboarding, so eval runs cannot
# mint identities or write check-ins on the live server.
if [ "${EVAL_UNITARES_OFFLINE:-}" = "1" ]; then
    UNITARES_SERVER_URL="http://127.0.0.1:9"
    UNITARES_LEASE_PLANE_URL="http://127.0.0.1:9"
    UNITARES_SIDECAR_URL="http://127.0.0.1:9"
    UNITARES_AUTO_ONBOARD=off
    UNITARES_DISABLE_AUTO_ONBOARD=1
    export UNITARES_SERVER_URL UNITARES_LEASE_PLANE_URL UNITARES_SIDECAR_URL \
        UNITARES_AUTO_ONBOARD UNITARES_DISABLE_AUTO_ONBOARD
fi
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_NAME="$1"
shift
if [ -x /bin/bash ]; then
    BASH_BIN=/bin/bash
elif [ -x /usr/bin/bash ]; then
    BASH_BIN=/usr/bin/bash
else
    BASH_BIN="$(command -v bash 2>/dev/null || true)"
fi
if [ -z "$BASH_BIN" ]; then
    echo "run-hook.cmd: Bash is required to run UNITARES hooks" >&2
    exit 1
fi
exec "$BASH_BIN" "${SCRIPT_DIR}/${SCRIPT_NAME}" "$@"
