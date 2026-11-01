"""The Codex hook report records only outcomes the plugin actually observes."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
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
            "command": '"$_unitares_runner" probe --host codex',
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
    assert result["versions"][0].startswith("9.8.7 (definition ")
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


def test_session_start_timeout_does_not_match_watcher_event(tmp_path: Path) -> None:
    from scripts.hook_invocations import _configured_timeout

    root = _plugin(tmp_path, timeout=15)
    (root / "hooks" / "codex-hooks.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [
            {"command": '"$_unitares_runner" session-start --host codex', "timeout": 15},
            {"command": '"$_unitares_runner" watcher-context --event session-start --host codex',
             "timeout": 12},
        ]}]},
    }))
    assert _configured_timeout(root, "session-start") == 15


def test_report_distinguishes_script_only_changes(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    (root / "hooks" / "run-hook.cmd").write_text("runner")
    (root / "scripts" / "hook_invocations.py").write_text("reporter")
    child = root / "hooks" / "probe"
    report_dir = tmp_path / "receipts"
    child.write_text("first")
    assert _run(root, report_dir, "version-session", "pass").returncode == 0
    child.write_text("second")
    assert _run(root, report_dir, "version-session", "pass").returncode == 0
    versions = _report(report_dir, "version-session")["versions"]
    assert len(versions) == 2
    assert all(version.startswith("9.8.7 (definition ") for version in versions)


def test_post_activity_receives_original_host_pid(tmp_path: Path) -> None:
    root = _plugin(tmp_path)
    shutil.copy(ROOT / "hooks" / "run-hook.cmd", root / "hooks" / "run-hook.cmd")
    shutil.copy(ROOT / "hooks" / "post-activity", root / "hooks" / "post-activity")
    shutil.copy(REPORTER, root / "scripts" / "hook_invocations.py")
    observer = root / "scripts" / "activity_observer.py"
    observer.write_text(
        "import os, sys, pathlib\n"
        "pathlib.Path(os.environ['UNITARES_TEST_PID_FILE']).write_text(sys.argv[sys.argv.index('--host-pid') + 1])\n"
    )
    pid_file = tmp_path / "host-pid"
    result = subprocess.run(
        ["/bin/sh", str(root / "hooks" / "run-hook.cmd"), "post-activity", "--host", "codex"],
        input=json.dumps({"session_id": "pid-session"}), text=True, capture_output=True,
        env=_env(tmp_path / "receipts", UNITARES_TEST_PID_FILE=str(pid_file)),
        timeout=5, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert pid_file.read_text() == str(os.getpid())


def test_cancelling_reporter_stops_hook_descendants(tmp_path: Path) -> None:
    if os.name == "nt":
        return  # Windows uses taskkill /T; this test exercises POSIX process trees.
    root = _plugin(tmp_path, timeout=30)
    shutil.copy(ROOT / "hooks" / "run-hook.cmd", root / "hooks" / "run-hook.cmd")
    shutil.copy(REPORTER, root / "scripts" / "hook_invocations.py")
    child = root / "hooks" / "probe"
    child.write_text("#!/bin/sh\nsleep 30 &\necho $! > \"$UNITARES_TEST_CHILD_PID_FILE\"\nwait\n")
    child.chmod(0o755)
    pid_file = tmp_path / "child-pid"
    process = subprocess.Popen(
        ["/bin/sh", str(root / "hooks" / "run-hook.cmd"), "probe", "--host", "codex"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_env(tmp_path / "receipts", UNITARES_TEST_CHILD_PID_FILE=str(pid_file)),
    )
    sleeper = None
    try:
        assert process.stdin is not None
        process.stdin.write(json.dumps({"session_id": "cancel-session"}).encode())
        process.stdin.close()
        deadline = time.monotonic() + 3
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()
        sleeper = int(pid_file.read_text())
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=3)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = subprocess.run(["ps", "-p", str(sleeper), "-o", "stat="],
                                   text=True, capture_output=True, check=False).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(0.02)
        else:
            raise AssertionError("hook descendant remained active after reporter cancellation")
        assert _report(tmp_path / "receipts", "cancel-session")["counts"]["incomplete"] == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        if sleeper is not None:
            try:
                os.kill(sleeper, signal.SIGKILL)
            except ProcessLookupError:
                pass
