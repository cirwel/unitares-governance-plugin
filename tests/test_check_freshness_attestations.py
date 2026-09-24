"""The plugin freshness checker reads attestation dates synced from unitares.

Re-verifying a skill in unitares writes a new file under
skills/.attestations/<skill>/ instead of editing SKILL.md, so the effective
verified date is the later of the frontmatter and the newest attestation.
"""

import json
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
    assert "verified 2 days ago" in result.stdout


def test_attestations_dir_is_not_treated_as_a_skill(tmp_path):
    _write_skill(tmp_path, _day(1))
    (tmp_path / "skills" / ".attestations" / "demo").mkdir(parents=True)
    result = _run(tmp_path)
    assert result.returncode == 0
    assert ".attestations" not in result.stdout
