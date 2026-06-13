#!/usr/bin/env python3
"""Check skill freshness against source file modification times."""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

RED = "\033[0;31m"
YELLOW = "\033[0;33m"
GREEN = "\033[0;32m"
NC = "\033[0m"


def parse_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter from skill file."""
    if not content.startswith("---\n"):
        return {}
    end = content.find("\n---", 4)
    if end == -1:
        return {}
    fm = yaml.safe_load(content[4:end])
    if not isinstance(fm, dict):
        return {}

    meta = fm.get("metadata", {}) or {}
    last_verified = meta.get("unitares.last_verified")
    freshness_days = meta.get("unitares.freshness_days")

    if not last_verified or not freshness_days:
        return {}

    return {
        "last_verified": str(last_verified),
        "freshness_days": int(freshness_days),
    }


def load_source_files(skill_dir: Path) -> list[str]:
    """Load source_files list from .freshness.yaml sidecar."""
    sidecar = skill_dir / ".freshness.yaml"
    if not sidecar.exists():
        return []
    data = yaml.safe_load(sidecar.read_text())
    if not isinstance(data, dict):
        return []
    files = data.get("source_files", [])
    return [str(f) for f in files] if files else []


def check_skills(plugin_root: str, projects_root: str) -> int:
    skills_dir = Path(plugin_root) / "skills"
    has_stale = False

    for skill_dir in sorted(skills_dir.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        skill_name = skill_dir.name
        content = skill_file.read_text()
        meta = parse_frontmatter(content)

        if not meta:
            print(f"  [{YELLOW}-{NC}] {skill_name}: no freshness metadata")
            continue

        # Anchor everything to UTC so a CI runner (UTC) and a local machine
        # (e.g. Mountain Time) agree about day boundaries — otherwise the same
        # source mtime can read FRESH locally but STALE in CI near midnight.
        verified_date = datetime.strptime(meta["last_verified"], "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
        max_days = meta["freshness_days"]
        verified_date_start = datetime.strptime(meta["last_verified"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - verified_date_start).days

        source_files = load_source_files(skill_dir)
        source_modified = False
        modified_file = ""
        for src in source_files:
            full_path = Path(projects_root) / src
            if full_path.exists():
                mtime = datetime.fromtimestamp(os.path.getmtime(full_path), tz=timezone.utc)
                if mtime > verified_date:
                    source_modified = True
                    modified_file = f"{src} (modified {mtime.strftime('%Y-%m-%d')})"
                    break

        if source_modified:
            print(f"  [{RED}STALE{NC}] {skill_name}: verified {meta['last_verified']}, but {modified_file}")
            has_stale = True
        elif age_days > max_days:
            print(f"  [{YELLOW}AGING{NC}] {skill_name}: verified {age_days} days ago (threshold: {max_days})")
            has_stale = True
        else:
            print(f"  [{GREEN}FRESH{NC}] {skill_name}: verified {age_days} days ago")

    if has_stale:
        print()
        print("Some skills are stale. Update last_verified after reviewing source changes.")
        return 1
    return 0


if __name__ == "__main__":
    plugin_root = sys.argv[1]
    projects_root = sys.argv[2]
    sys.exit(check_skills(plugin_root, projects_root))
