"""The plugin freshness checker reads attestation dates synced from unitares.

Re-verifying a skill in unitares writes a new file under
skills/.attestations/<skill>/ instead of editing SKILL.md, so the effective
verified date is the later of the frontmatter and the attestations that vouch
for the SKILL.md on disk (unitares src/skill_attestations.py, THE RULE).
"""

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHECKER = Path(__file__).resolve().parents[1] / "scripts/_check_freshness.py"


def _day(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _write_skill(root: Path, last_verified: str) -> None:
    skill = root / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f'---\nname: demo\nlast_verified: "{last_verified}"\nfreshness_days: 14\n'
        "source_files:\n  - unitares/src/absent.py\n---\n# Demo\n"
    )


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), str(root), str(root / "projects")],
        capture_output=True, text=True,
    )


def test_old_frontmatter_date_alone_is_aging(tmp_path):
    _write_skill(tmp_path, _day(60))
    result = _run(tmp_path)
    assert result.returncode == 1 and "AGING" in result.stdout


def test_a_recent_attestation_makes_it_fresh(tmp_path):
    _write_skill(tmp_path, _day(60))
    adir = tmp_path / "skills" / ".attestations" / "demo"
    adir.mkdir(parents=True)
    (adir / "20260101T000000Z-aaaaaaaa.json").write_text(
        json.dumps({"verified_date": _day(2), "source_digests": {}})
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout
    # The age shown comes from the attestation, not the 60-day frontmatter. It
    # reads 3 if the run crosses midnight UTC after the fixture was written.
    assert re.search(r"verified [23] days ago", result.stdout), result.stdout


def test_attestations_dir_is_not_treated_as_a_skill(tmp_path):
    _write_skill(tmp_path, _day(1))
    (tmp_path / "skills" / ".attestations" / "demo").mkdir(parents=True)
    result = _run(tmp_path)
    assert result.returncode == 0
    assert ".attestations" not in result.stdout


def _digest(root: Path) -> str:
    return hashlib.sha256((root / "skills/demo/SKILL.md").read_bytes()).hexdigest()[:16]


def _attest(root: Path, filename: str, verified: str, skill_digest: str | None = None) -> None:
    adir = root / "skills" / ".attestations" / "demo"
    adir.mkdir(parents=True, exist_ok=True)
    record = {"verified_date": verified, "source_digests": {}}
    if skill_digest is not None:
        record["skill_digest"] = skill_digest
    (adir / filename).write_text(json.dumps(record))


def test_a_newer_stamp_for_other_skill_text_does_not_vouch(tmp_path):
    # A stale branch re-stamped different SKILL.md text; it sorts newest but
    # nobody re-verified the text on disk, so the certified stamp decides.
    _write_skill(tmp_path, _day(60))
    _attest(tmp_path, "20260101T000000000000Z-aaaaaaaa.json", _day(40), _digest(tmp_path))
    _attest(tmp_path, "20260102T000000000000Z-bbbbbbbb.json", _day(1), "0000000000000000")
    result = _run(tmp_path)
    assert result.returncode == 1 and "AGING" in result.stdout, result.stdout


def test_the_latest_date_among_certified_stamps_wins_regardless_of_name_order(tmp_path):
    _write_skill(tmp_path, _day(60))
    _attest(tmp_path, "20260101T000000000000Z-aaaaaaaa.json", _day(3), _digest(tmp_path))
    _attest(tmp_path, "20260102T000000000000Z-bbbbbbbb.json", _day(45), _digest(tmp_path))
    # Exit status alone pins the rule (3 days is FRESH, 45 is AGING) and does
    # not flip if the run crosses midnight UTC, as an exact day count can.
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout


def test_uncertified_text_falls_back_to_the_newest_stamp_alone(tmp_path):
    _write_skill(tmp_path, _day(60))
    _attest(tmp_path, "20260101T000000000000Z-aaaaaaaa.json", _day(2))
    _attest(tmp_path, "20260102T000000000000Z-bbbbbbbb.json", _day(45))
    result = _run(tmp_path)
    assert result.returncode == 1 and "AGING" in result.stdout, result.stdout
