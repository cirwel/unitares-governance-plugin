"""The plugin freshness checker reads attestation dates synced from unitares.

Re-verifying a skill in unitares writes a new file under
skills/.attestations/<skill>/ instead of editing SKILL.md, so the effective
verified date is the later of the frontmatter and the attestations that vouch
for the SKILL.md on disk (unitares src/skill_attestations.py, THE RULE).
"""

import hashlib
import json
import re
import shutil
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
        "---\n# Demo\n"
    )


def _run(root: Path) -> subprocess.CompletedProcess:
    canonical = root / "canonical-skills"
    if not canonical.exists():
        shutil.copytree(root / "skills", canonical)
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            str(root),
            str(root / "projects"),
            str(canonical),
        ],
        capture_output=True, text=True,
    )


def test_old_frontmatter_date_alone_is_aging(tmp_path):
    _write_skill(tmp_path, _day(60))
    result = _run(tmp_path)
    assert result.returncode == 1 and "AGING" in result.stdout


def test_a_recent_attestation_makes_it_fresh(tmp_path):
    _write_skill(tmp_path, _day(60))
    _attest(
        tmp_path,
        "20260101T000000Z-aaaaaaaa.json",
        _day(2),
        _digest(tmp_path),
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


def _attest(
    root: Path,
    filename: str,
    verified: str,
    skill_digest: str | None = None,
    *,
    source_digests: dict[str, str] | None = None,
) -> None:
    adir = root / "skills" / ".attestations" / "demo"
    adir.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "unitares.skill_attestation.v1",
        "skill": "demo",
        "verified_at": f"{verified}T12:00:00Z",
        "verified_date": verified,
        "verifier": "test verifier",
        "source_digests": source_digests or {"unitares/src/absent.py": "a" * 16},
        "skill_digest": skill_digest or "0" * 16,
    }
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


def test_uncertified_text_falls_back_to_the_newest_stamp(tmp_path):
    _write_skill(tmp_path, _day(60))
    _attest(tmp_path, "20260101T000000000000Z-aaaaaaaa.json", _day(45))
    _attest(tmp_path, "20260102T000000000000Z-bbbbbbbb.json", _day(2))
    result = _run(tmp_path)
    assert result.returncode == 0 and "FRESH" in result.stdout, result.stdout


def test_attestation_must_match_canonical_provenance(tmp_path):
    _write_skill(tmp_path, _day(1))
    _attest(tmp_path, "20260101T000000000000Z-aaaaaaaa.json", _day(1), _digest(tmp_path))
    shutil.copytree(tmp_path / "skills", tmp_path / "canonical-skills")
    path = next((tmp_path / "skills/.attestations/demo").glob("*.json"))
    record = json.loads(path.read_text())
    record["verified_date"] = _day(0)
    record["verified_at"] = f"{_day(0)}T12:00:00Z"
    path.write_text(json.dumps(record))

    result = _run(tmp_path)
    assert result.returncode == 1
    assert "UNTRUSTED" in result.stdout and "differs from canonical" in result.stdout


def test_attestation_without_canonical_provenance_is_rejected(tmp_path):
    _write_skill(tmp_path, _day(1))
    _attest(tmp_path, "20260101T000000000000Z-aaaaaaaa.json", _day(1), _digest(tmp_path))

    result = subprocess.run(
        [sys.executable, str(CHECKER), str(tmp_path), str(tmp_path / "projects")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "UNTRUSTED" in result.stdout and "absent from canonical" in result.stdout


def test_missing_canonical_attestation_is_rejected(tmp_path):
    _write_skill(tmp_path, _day(1))
    shutil.copytree(tmp_path / "skills", tmp_path / "canonical-skills")
    canonical_record = (
        tmp_path
        / "canonical-skills/.attestations/demo/20260101T000000000000Z-aaaaaaaa.json"
    )
    canonical_record.parent.mkdir(parents=True)
    canonical_record.write_text(
        json.dumps(
            {
                "schema": "unitares.skill_attestation.v1",
                "skill": "demo",
                "verified_at": f"{_day(1)}T12:00:00Z",
                "verified_date": _day(1),
                "verifier": "canonical verifier",
                "source_digests": {"unitares/src/source.py": "a" * 16},
                "skill_digest": _digest(tmp_path),
            }
        )
    )

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "missing from the plugin mirror" in result.stdout


def test_empty_or_future_attestation_is_rejected(tmp_path):
    _write_skill(tmp_path, _day(1))
    future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    _attest(
        tmp_path,
        "20260101T000000000000Z-aaaaaaaa.json",
        future,
        _digest(tmp_path),
        source_digests={},
    )
    path = next((tmp_path / "skills/.attestations/demo").glob("*.json"))
    record = json.loads(path.read_text())
    record["source_digests"] = {}
    path.write_text(json.dumps(record))

    result = _run(tmp_path)
    assert result.returncode == 1
    assert "malformed, empty, mismatched, or future-dated" in result.stdout


def test_anchored_source_digest_is_validated(tmp_path):
    skill = tmp_path / "skills/demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f'---\nname: demo\nlast_verified: "{_day(60)}"\nfreshness_days: 14\n'
        "source_files:\n  - unitares/src/source.py#kept\n---\n# Demo\n"
    )
    source = tmp_path / "projects/unitares/src/source.py"
    source.parent.mkdir(parents=True)
    source.write_text("def kept():\n    return 1\n\ndef unrelated():\n    return 1\n")
    fragment_digest = hashlib.sha256(b"def kept():\n    return 1\n").hexdigest()[:16]
    _attest(
        tmp_path,
        "20260101T000000000000Z-aaaaaaaa.json",
        _day(1),
        _digest(tmp_path),
        source_digests={"unitares/src/source.py#kept": fragment_digest},
    )
    assert _run(tmp_path).returncode == 0

    source.write_text("def kept():\n    return 2\n\ndef unrelated():\n    return 1\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "current digest is not attested" in result.stdout


def test_missing_source_file_fails_closed(tmp_path):
    skill = tmp_path / "skills/demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f'---\nname: demo\nlast_verified: "{_day(1)}"\nfreshness_days: 14\n'
        "source_files:\n  - unitares/src/missing.py\n---\n# Demo\n"
    )

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "source file is missing" in result.stdout
