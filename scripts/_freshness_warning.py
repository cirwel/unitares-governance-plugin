#!/usr/bin/env python3
"""Output a freshness warning if a skill's last_verified is stale. Used by session-start hook."""

import sys
from datetime import datetime

import yaml


def _parse_frontmatter(content: str) -> dict:
    if not content.startswith("---\n"):
        return {}
    end = content.find("\n---", 4)
    if end == -1:
        return {}
    fm = yaml.safe_load(content[4:end])
    if not isinstance(fm, dict):
        return {}
    return fm


content = sys.argv[1] if len(sys.argv) > 1 else ""
fm = _parse_frontmatter(content)
if not fm:
    sys.exit(0)

meta = fm.get("metadata", {}) or {}
last_verified_raw = meta.get("unitares.last_verified")
freshness_days_raw = meta.get("unitares.freshness_days")

if not last_verified_raw or not freshness_days_raw:
    sys.exit(0)

verified = datetime.strptime(str(last_verified_raw), "%Y-%m-%d")
max_days = int(freshness_days_raw)
age = (datetime.now() - verified).days

if age > max_days:
    print(
        f"WARNING: This skill was last verified {age} days ago (threshold: {max_days}). "
        f"Treat specific thresholds and behavioral claims as potentially outdated."
    )
