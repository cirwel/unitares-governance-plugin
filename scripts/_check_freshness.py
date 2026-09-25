#!/usr/bin/env python3
"""Check skill freshness against source file modification times."""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Calendar-age floor (days). A skill's per-skill `freshness_days` is honored, but
# the effective AGING threshold is never below this floor — so stable reference
# skills don't flip the whole gate red every couple of weeks on calendar time
# alone (the source-drift STALE check below still fires immediately on real
# source changes). Override with SKILL_FRESHNESS_FLOOR_DAYS.
FRESHNESS_FLOOR_DAYS = int(os.environ.get("SKILL_FRESHNESS_FLOOR_DAYS", "30"))

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

    # Accept both layouts: nested `metadata.unitares.*` (current) and flat
    # top-level keys (the in-progress frontmatter refactor). Without the flat
    # fallback the parser would silently return {} on refactored skills and the
    # gate would stop checking them.
    meta = fm.get("metadata", {}) or {}
    last_verified = meta.get("unitares.last_verified") or fm.get("last_verified")
    freshness_days = meta.get("unitares.freshness_days") or fm.get("freshness_days")

    if not last_verified or not freshness_days:
        return {}

    # source_files may live in the flat frontmatter (refactor) instead of the
    # .freshness.yaml sidecar; surface it so the STALE drift check still works.
    fm_sources = fm.get("source_files") or []

    return {
        "last_verified": str(last_verified),
        "freshness_days": int(freshness_days),
        "source_files": [str(f) for f in fm_sources],
    }


def load_source_files(skill_dir: Path, frontmatter_sources: list[str]) -> list[str]:
    """source_files from the .freshness.yaml sidecar, else from frontmatter."""
    sidecar = skill_dir / ".freshness.yaml"
    if sidecar.exists():
        data = yaml.safe_load(sidecar.read_text())
        if isinstance(data, dict):
            files = data.get("source_files", [])
            if files:
                return [str(f) for f in files]
    return list(frontmatter_sources)


# Hex characters of sha256 kept per digest, matching unitares.
DIGEST_HEX = 16


def skill_text_digest(skill_md: Path) -> str:
    """Digest of a SKILL.md as recorded in an attestation's `skill_digest`."""
    return hashlib.sha256(skill_md.read_bytes()).hexdigest()[:DIGEST_HEX]


def load_attestations(skills_dir: Path, name: str) -> list[dict]:
    """Every readable attestation synced from unitares for a skill, newest first.

    Re-verifying a skill in unitares writes a new file under
    skills/.attestations/<skill>/<YYYYMMDDTHHMMSSffffffZ>-<8 hex>.json instead
    of editing SKILL.md (so concurrent stamping PRs cannot conflict); the format
    is defined in unitares scripts/client/_check_freshness.py. The file name
    leads with a microsecond UTC timestamp, so the lexically last is the newest.
    Unreadable files and records without a `source_digests` map are skipped.
    """
    adir = skills_dir / ".attestations" / name
    if not adir.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(adir.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("source_digests"), dict):
            records.append(data)
    return records


def attested_date(skills_dir: Path, name: str, skill_digest: str) -> str | None:
    """Newest `verified_date` among the attestations that vouch for the skill
    text on disk, or None.

    Mirrors unitares src/skill_attestations.py (THE RULE): if any attestation
    certified the current text (its `skill_digest` equals ``skill_digest``),
    exactly those vouch, whatever their age. Otherwise the current text was
    never certified and the newest attestation alone vouches. A stamp for other
    skill text from a stale branch must not reset AGING for the text actually
    on disk, even when it sorts newest.
    """
    records = load_attestations(skills_dir, name)
    certified = [r for r in records if r.get("skill_digest") == skill_digest]
    date = None
    for record in certified or records[:1]:
        verified = record.get("verified_date")
        if isinstance(verified, str) and verified and (date is None or verified > date):
            date = verified
    return date


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
        if meta:
            # Effective date: the later of the frontmatter and the vouching attestations.
            attested = attested_date(skills_dir, skill_name, skill_text_digest(skill_file))
            if attested and attested > meta["last_verified"]:
                meta["last_verified"] = attested

        if not meta:
            print(f"  [{YELLOW}-{NC}] {skill_name}: no freshness metadata")
            continue

        # Anchor everything to UTC so a CI runner (UTC) and a local machine
        # (e.g. Mountain Time) agree about day boundaries — otherwise the same
        # source mtime can read FRESH locally but STALE in CI near midnight.
        verified_date = datetime.strptime(meta["last_verified"], "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
        max_days = max(meta["freshness_days"], FRESHNESS_FLOOR_DAYS)
        verified_date_start = datetime.strptime(meta["last_verified"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - verified_date_start).days

        source_files = load_source_files(skill_dir, meta.get("source_files", []))
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
