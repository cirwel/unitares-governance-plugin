"""The Codex hook report records only outcomes the plugin actually observes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORTER = ROOT / "scripts" / "hook_invocations.py"


def _env(report_dir: Path, **extra: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "UNITARES_HOOK_REPORT_DIR": str(report_dir),
        **extra,
    }


def _plugin(tmp_path: Path, *, timeout: float = 2.0) -> Path:
    root = tmp_path / "plugin"
    (root / ".codex-plugin").mkdir(parents=True)
    (root / "hooks").mkdir()
    (root / "scripts").mkdir()
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"version": "9.8.7"})
    )
    (root / "hooks" / "codex-hooks.json").write_text(json.dumps({
        "hooks": {"PostToolUse": [{"hooks": [{
            "command": "run-hook.cmd probe --host codex",
            "timeout": timeout,
        }]}]},
    }))
    return root


def _run(root: Path, report_dir: Path, session_id: str, code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPORTER), "run", "--plugin-root", str(root),
         "--hook", "probe", "--", sys.executable, "-c", code],
        input=json.dumps({"session_id": session_id, "private": "do-not-journal"}),
        text=True,
        capture_output=True,
        env=_env(report_dir),
        timeout=5,
        check=False,
    )


def _report(report_dir: Path, session_id: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(REPORTER), "report", "--session-id", session_id, "--json"],
        text=True,
        capture_output=True,
        env=_env(report_dir),
        timeout=5,
        check=True,
    )
    return json.loads(result.stdout)


def test_report_counts_observed_outcomes_and_preserves_stream(tmp_path: Path) -> None:
    root = _plugin(tmp_path, timeout=0.4)
    report_dir = tmp_path / "receipts"
    session_id = "secret-raw-session-id"
    succeeded = _run(root, report_dir, session_id, "import sys; sys.stdout.write(sys.stdin.read())")
    assert succeeded.returncode == 0
    assert "do-not-journal" in succeeded.stdout
    skipped = _run(
        root, report_dir, session_id,
        "import os, pathlib; pathlib.Path(os.environ['UNITARES_HOOK_STATUS_FILE']).write_text('skipped')",
    )
    assert skipped.returncode == 0
    failed = _run(root, report_dir, session_id, "import sys; sys.exit(7)")
    assert failed.returncode == 7
    timed_out = _run(root, report_dir, session_id, "import time; time.sleep(2)")
    assert timed_out.returncode == 124

    result = _report(report_dir, session_id)
    assert len(result["versions"]) == 1
    assert result["versions"][0].startswith("9.8.7 (")
    assert result["counts"] == {
        "fired": 4, "succeeded": 1, "skipped": 1,
        "timed_out": 1, "failed": 1, "incomplete": 0,
    }
    assert result["hooks"]["probe"] == {
        "succeeded": 1, "skipped": 1, "failed": 1, "timed_out": 1,
    }
    journal = next(report_dir.glob("*.jsonl")).read_text()
    assert session_id not in journal
    assert "do-not-journal" not in journal
    assert "private" not in journal


def test_unfinished_start_is_incomplete_not_timeout(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    report_dir = tmp_path / "receipts"
    session_id = "another-session"
    assert _run(root, report_dir, session_id, "pass").returncode == 0
    journal = next(report_dir.glob("*.jsonl"))
    first = json.loads(journal.read_text().splitlines()[0])
    journal.write_text(json.dumps(first) + "\n")
    counts = _report(report_dir, session_id)["counts"]
    assert counts["fired"] == 1
    assert counts["incomplete"] == 1
    assert counts["timed_out"] == 0


def test_wrapper_uses_reporter_for_codex(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    shutil.copy(ROOT / "hooks" / "run-hook.cmd", root / "hooks" / "run-hook.cmd")
    shutil.copy(REPORTER, root / "scripts" / "hook_invocations.py")
    child = root / "hooks" / "probe"
    child.write_text("#!/bin/sh\ncat\n")
    child.chmod(0o755)
    report_dir = tmp_path / "receipts"
    result = subprocess.run(
        ["/bin/sh", str(root / "hooks" / "run-hook.cmd"), "probe", "--host", "codex"],
        input=json.dumps({"session_id": "wrapper-session"}),
        text=True,
        capture_output=True,
        env=_env(report_dir),
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"session_id": "wrapper-session"}
    assert _report(report_dir, "wrapper-session")["counts"]["succeeded"] == 1


def test_disabled_watcher_reports_an_explicit_skip(tmp_path: Path) -> None:
    report_dir = tmp_path / "receipts"
    session_id = "watcher-session"
    result = subprocess.run(
        ["/bin/sh", str(ROOT / "hooks" / "run-hook.cmd"), "watcher-context",
         "--event", "user-prompt-submit", "--host", "codex"],
        input=json.dumps({"session_id": session_id}),
        text=True,
        capture_output=True,
        env=_env(report_dir, UNITARES_WATCHER_ENABLED="0"),
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert _report(report_dir, session_id)["hooks"]["watcher-context:user-prompt-submit"] == {
        "skipped": 1,
    }
