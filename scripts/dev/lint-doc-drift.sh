#!/usr/bin/env bash
# lint-doc-drift.sh — catch known stale agent-facing documentation patterns.
#
# This is intentionally narrow. It guards the drift classes that have already
# caused bad instructions: version badge drift, retired cache commands, stale
# flat-cache guidance, and obsolete file-lease lifecycle prose.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

FAIL=0

check_absent() {
    local needle="$1"
    local reason="$2"
    shift 2

    local matches
    matches=$(grep -R -nF --exclude='lint-doc-drift.sh' -- "$needle" "$@" 2>/dev/null || true)
    if [[ -n "$matches" ]]; then
        echo "[drift] stale text found: ${reason}"
        echo "$matches"
        FAIL=1
    fi
}

check_absent \
    "session_cache.py list session" \
    "helper command is now 'session_cache.py list --workspace \"\$PWD\"'" \
    README.md CODEX_START.md commands hooks scripts tests

check_absent \
    "Post-edit heartbeats held leases" \
    "post-edit now releases the just-edited lease immediately" \
    README.md CODEX_START.md commands hooks scripts tests

check_absent \
    "heartbeats held leases after edits" \
    "post-edit now releases the just-edited lease immediately" \
    README.md CODEX_START.md commands hooks scripts tests

check_absent \
    "Keep continuity in \`.unitares/session.json\`" \
    "flat session.json is legacy/shared; docs should teach slot-scoped session caches" \
    README.md CODEX_START.md commands hooks scripts tests

check_absent \
    "treat \`.unitares/session.json\` as the neutral local continuity cache" \
    "flat session.json is legacy/shared; docs should teach slot-scoped session caches" \
    README.md CODEX_START.md commands hooks scripts tests

check_absent \
    "shape of .unitares/session.json" \
    "auto-checkin decisions consume slot-scoped session cache shape" \
    README.md CODEX_START.md commands hooks scripts tests

if ! python3 - <<'PY'
import json
import re
import sys
from pathlib import Path

root = Path(".")
versions = {}

for rel in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
    versions[rel] = json.loads((root / rel).read_text(encoding="utf-8"))["version"]

market = json.loads((root / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
market_entry = next(p for p in market["plugins"] if p["name"] == "unitares-governance")
versions[".claude-plugin/marketplace.json"] = market_entry["version"]

checkin_py = (root / "scripts/checkin.py").read_text(encoding="utf-8")
default_match = re.search(r'DEFAULT_PLUGIN_VERSION = "([^"]+)"', checkin_py)
if not default_match:
    print("[drift] scripts/checkin.py DEFAULT_PLUGIN_VERSION missing or unexpected")
    sys.exit(1)
versions["scripts/checkin.py DEFAULT_PLUGIN_VERSION"] = default_match.group(1)

readme = (root / "README.md").read_text(encoding="utf-8")
badge_match = re.search(r"badge/version-([^-]+)-blue\.svg", readme)
if not badge_match:
    print("[drift] README version badge missing or unexpected")
    sys.exit(1)
versions["README badge"] = badge_match.group(1)

if len(set(versions.values())) != 1:
    print("[drift] version drift across public metadata:")
    for source, version in versions.items():
        print(f"  {source}: {version}")
    sys.exit(1)
PY
then
    FAIL=1
fi

if [[ $FAIL -eq 0 ]]; then
    echo "[drift] OK — docs and public metadata match current contracts"
fi

exit $FAIL
