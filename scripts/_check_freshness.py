#!/usr/bin/env python3
"""Check skill freshness against proven attestations and source digests."""

import ast
import hashlib
import json
import os
import re
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
ATTESTATION_SCHEMA = "unitares.skill_attestation.v1"
HEX_DIGEST = re.compile(rf"^[0-9a-f]{{{DIGEST_HEX}}}$")


def skill_text_digest(skill_md: Path) -> str:
    """Digest of a SKILL.md as recorded in an attestation's `skill_digest`."""
    return hashlib.sha256(skill_md.read_bytes()).hexdigest()[:DIGEST_HEX]


def _valid_attestation(data: object, name: str) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("schema") != ATTESTATION_SCHEMA or data.get("skill") != name:
        return False
    if not isinstance(data.get("verifier"), str) or not data["verifier"].strip():
        return False
    if not isinstance(data.get("skill_digest"), str) or not HEX_DIGEST.fullmatch(
        data["skill_digest"]
    ):
        return False
    source_digests = data.get("source_digests")
    if not isinstance(source_digests, dict) or not source_digests:
        return False
    if any(
        not isinstance(source, str)
        or not source
        or not isinstance(digest, str)
        or not HEX_DIGEST.fullmatch(digest)
        for source, digest in source_digests.items()
    ):
        return False
    try:
        verified_at = datetime.strptime(
            data.get("verified_at", ""), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        verified_date = datetime.strptime(
            data.get("verified_date", ""), "%Y-%m-%d"
        ).date()
    except (TypeError, ValueError):
        return False
    return verified_date == verified_at.date() and verified_at <= datetime.now(
        timezone.utc
    )


def load_attestations(
    skills_dir: Path,
    name: str,
    canonical_skills_dir: Path | None,
) -> tuple[list[dict], list[str]]:
    """Load only strict, byte-proven canonical attestations, newest first."""
    adir = skills_dir / ".attestations" / name
    records: list[dict] = []
    errors: list[str] = []
    canonical_adir = (
        canonical_skills_dir / ".attestations" / name
        if canonical_skills_dir is not None
        else None
    )
    if canonical_adir is not None and canonical_adir.is_dir():
        local_names = (
            {path.name for path in adir.glob("*.json")} if adir.is_dir() else set()
        )
        for canonical in canonical_adir.glob("*.json"):
            if canonical.name not in local_names:
                relative = canonical.relative_to(canonical_skills_dir)
                errors.append(f"{relative}: missing from the plugin mirror")
    if not adir.is_dir():
        return records, errors
    for path in sorted(adir.glob("*.json"), reverse=True):
        relative = path.relative_to(skills_dir)
        canonical = (
            canonical_skills_dir / relative
            if canonical_skills_dir is not None
            else None
        )
        if canonical is None or not canonical.is_file():
            errors.append(f"{relative}: absent from canonical skills provenance")
            continue
        try:
            raw = path.read_bytes()
            if raw != canonical.read_bytes():
                errors.append(f"{relative}: differs from canonical skills provenance")
                continue
            data = json.loads(raw)
        except (OSError, ValueError):
            errors.append(f"{relative}: unreadable attestation")
            continue
        if not _valid_attestation(data, name):
            errors.append(f"{relative}: malformed, empty, mismatched, or future-dated")
            continue
        records.append(data)
    return records, errors


def attested_records(records: list[dict], skill_digest: str) -> list[dict]:
    """Records that certify exactly the current skill text, newest first."""
    return sorted(
        (r for r in records if r.get("skill_digest") == skill_digest),
        key=lambda r: r["verified_at"],
        reverse=True,
    )


def attested_date(
    skills_dir: Path,
    name: str,
    skill_digest: str,
    canonical_skills_dir: Path | None = None,
    *,
    records: list[dict] | None = None,
) -> str | None:
    """Newest date that canonically certifies exactly the current skill text.

    The first three parameters preserve the helper API introduced with the
    attestation-date rule. Hardened callers also supply canonical provenance;
    the checker passes its already validated records to avoid a second read.
    """
    if records is None:
        records, _ = load_attestations(skills_dir, name, canonical_skills_dir)
    certified = attested_records(records, skill_digest)
    return max((record["verified_date"] for record in certified), default=None)


def split_source_reference(source: str) -> tuple[str, str | None]:
    """Split a supported path[#symbol] reference, failing closed on ambiguity."""
    if source.count("#") > 1:
        raise ValueError("multiple fragment separators")
    path_text, separator, anchor = source.partition("#")
    path = Path(path_text)
    if not path_text or path.is_absolute() or ".." in path.parts:
        raise ValueError("source path must be a safe relative path")
    if separator and not anchor:
        raise ValueError("empty source fragment")
    if anchor and path.suffix not in {".py", ".md", ".markdown"}:
        raise ValueError("fragments are supported only for Python and Markdown")
    return path_text, anchor or None


def _python_fragment(text: str, anchor: str) -> str | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    lines = text.splitlines(keepends=True)

    def find(body: list[ast.stmt], name: str) -> ast.AST | None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == name:
                    return node
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                    return node
        return None

    node: ast.AST | None = None
    body = tree.body
    for part in anchor.split("."):
        node = find(body, part)
        if node is None:
            return None
        body = getattr(node, "body", [])
    start = node.lineno
    for decorator in getattr(node, "decorator_list", []):
        start = min(start, decorator.lineno)
    return "".join(lines[start - 1 : node.end_lineno])


def _markdown_fragment(text: str, anchor: str) -> str | None:
    lines = text.splitlines(keepends=True)
    heading = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
    start = level = None
    for index, line in enumerate(lines):
        match = heading.match(line)
        if not match:
            continue
        if start is None:
            if match.group(2).strip() == anchor:
                start, level = index, len(match.group(1))
        elif len(match.group(1)) <= level:
            return "".join(lines[start:index])
    return "".join(lines[start:]) if start is not None else None


def source_digest(path: Path, anchor: str | None) -> str | None:
    if anchor is None:
        content = path.read_bytes()
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        fragment = (
            _python_fragment(text, anchor)
            if path.suffix == ".py"
            else _markdown_fragment(text, anchor)
        )
        if fragment is None:
            return None
        content = fragment.encode("utf-8")
    return hashlib.sha256(content).hexdigest()[:DIGEST_HEX]


def check_skills(
    plugin_root: str,
    projects_root: str,
    canonical_skills_root: str | None = None,
) -> int:
    skills_dir = Path(plugin_root) / "skills"
    canonical_skills_dir = (
        Path(canonical_skills_root).resolve() if canonical_skills_root else None
    )
    has_stale = False

    for skill_dir in sorted(skills_dir.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        skill_name = skill_dir.name
        content = skill_file.read_text()
        meta = parse_frontmatter(content)
        records, errors = load_attestations(
            skills_dir, skill_name, canonical_skills_dir
        )
        for error in errors:
            print(f"  [{RED}UNTRUSTED{NC}] {skill_name}: {error}")
            has_stale = True
        certified: list[dict] = []
        if meta:
            current_skill_digest = skill_text_digest(skill_file)
            certified = attested_records(records, current_skill_digest)
            attested = attested_date(
                skills_dir,
                skill_name,
                current_skill_digest,
                canonical_skills_dir,
                records=records,
            )
            if attested and attested > meta["last_verified"]:
                meta["last_verified"] = attested

        if not meta:
            print(f"  [{YELLOW}-{NC}] {skill_name}: no freshness metadata")
            continue

        # Anchor everything to UTC so CI and local runs agree about day boundaries.
        max_days = max(meta["freshness_days"], FRESHNESS_FLOOR_DAYS)
        verified_date_start = datetime.strptime(
            meta["last_verified"], "%Y-%m-%d"
        ).replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - verified_date_start).days

        source_files = load_source_files(skill_dir, meta.get("source_files", []))
        source_modified = False
        modified_file = ""
        for src in source_files:
            try:
                path_text, anchor = split_source_reference(src)
            except ValueError as exc:
                source_modified = True
                modified_file = f"{src} (invalid source reference: {exc})"
                break
            full_path = Path(projects_root) / path_text
            if not full_path.is_file():
                source_modified = True
                modified_file = f"{src} (source file is missing)"
                break
            current_digest = source_digest(full_path, anchor)
            recorded_digests = {
                record["source_digests"].get(src) for record in certified
            }
            if current_digest is None:
                source_modified = True
                modified_file = f"{src} (fragment does not resolve)"
                break
            if current_digest not in recorded_digests:
                source_modified = True
                modified_file = f"{src} (current digest is not attested)"
                break

        if source_modified:
            print(
                f"  [{RED}STALE{NC}] {skill_name}: "
                f"verified {meta['last_verified']}, but {modified_file}"
            )
            has_stale = True
        elif age_days > max_days:
            print(
                f"  [{YELLOW}AGING{NC}] {skill_name}: verified {age_days} days ago "
                f"(threshold: {max_days})"
            )
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
    canonical_skills_root = sys.argv[3] if len(sys.argv) > 3 else None
    sys.exit(check_skills(plugin_root, projects_root, canonical_skills_root))
