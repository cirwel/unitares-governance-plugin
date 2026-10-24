from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LINTER = ROOT / "scripts" / "dev" / "lint-changed-files.sh"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )


def test_scope_guard_scans_unusual_filenames_without_word_splitting(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "security-test@example.invalid")
    _git(tmp_path, "config", "user.name", "Security Test")
    (tmp_path / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "baseline.txt")
    _git(tmp_path, "commit", "-m", "baseline")
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    unusual = ["space name.txt", "tab\tname.txt", "line\nbreak.txt", "-leading.txt"]
    for name in unusual:
        forbidden = "Claude-" + "Session: must-be-detected\n"
        (tmp_path / name).write_text(forbidden, encoding="utf-8")
    _git(tmp_path, "add", "--", *unusual)
    _git(tmp_path, "commit", "-m", "unusual paths")

    result = subprocess.run(
        [str(LINTER), base],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    for name in unusual:
        assert f"file={name}" in result.stdout
