"""The Claude Stop hook must outlive the process that started it.

Claude Code runs Stop hooks asynchronously and kills them when the CLI exits.
A one-shot ``claude -p`` turn exits within a second of Stop, so a foreground
hook never reached its check-in. The dispatcher hands the payload to a child
that ignores the exit signals and returns at once.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "hooks" / "run-hook.cmd"

# Stands in for hooks/post-stop: slow enough to outlast its parent, and it
# records the payload it was given plus its arguments.
FAKE_STOP = """#!/usr/bin/env bash
payload="$(cat)"
sleep 1
printf '%s|%s' "$payload" "$*" > "$PROBE_OUT"
printf '{}\\n'
"""


def _plugin(tmp_path: Path) -> Path:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy(WRAPPER, hooks / "run-hook.cmd")
    fake = hooks / "post-stop"
    fake.write_text(FAKE_STOP)
    fake.chmod(0o755)
    return hooks / "run-hook.cmd"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(tmp_path), "PROBE_OUT": str(tmp_path / "out"), **extra}


def _wait_for(path: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return True
        time.sleep(0.05)
    return False


def test_claude_stop_returns_at_once_and_finishes_in_the_background(tmp_path: Path) -> None:
    wrapper = _plugin(tmp_path)
    started = time.monotonic()
    res = subprocess.run(
        ["sh", str(wrapper), "post-stop", "--host", "claude"],
        input='{"session_id":"s-1"}', capture_output=True, text=True,
        env=_env(tmp_path), timeout=10,
    )
    elapsed = time.monotonic() - started

    assert res.returncode == 0
    assert res.stdout == ""
    assert elapsed < 0.9, f"dispatcher waited for the hook ({elapsed:.2f}s)"
    assert not (tmp_path / "out").exists()
    assert _wait_for(tmp_path / "out", 5)
    assert (tmp_path / "out").read_text() == '{"session_id":"s-1"}|--host claude'


def test_claude_stop_survives_the_signal_that_ends_its_parent(tmp_path: Path) -> None:
    wrapper = _plugin(tmp_path)
    proc = subprocess.Popen(
        ["sh", str(wrapper), "post-stop", "--host", "claude"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=_env(tmp_path), start_new_session=True, text=True,
    )
    proc.communicate('{"session_id":"s-2"}', timeout=10)
    # What a host does to a hook's process group when it exits.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    assert _wait_for(tmp_path / "out", 5)
    assert (tmp_path / "out").read_text().startswith('{"session_id":"s-2"}|')


def test_detach_can_be_switched_off(tmp_path: Path) -> None:
    wrapper = _plugin(tmp_path)
    res = subprocess.run(
        ["sh", str(wrapper), "post-stop", "--host", "claude"],
        input='{"session_id":"s-3"}', capture_output=True, text=True,
        env=_env(tmp_path, UNITARES_STOP_DETACH="0"), timeout=10,
    )
    assert res.returncode == 0
    assert (tmp_path / "out").read_text() == '{"session_id":"s-3"}|--host claude'
    assert res.stdout == "{}\n"


def test_codex_and_agy_stop_stay_in_the_foreground(tmp_path: Path) -> None:
    for host in ("codex", "agy"):
        (tmp_path / host).mkdir()
        wrapper = _plugin(tmp_path / host)
        res = subprocess.run(
            ["sh", str(wrapper), "post-stop", "--host", host],
            input='{"session_id":"s-4"}', capture_output=True, text=True,
            env=_env(tmp_path / host), timeout=10,
        )
        # They read the hook's stdout, so it must have run to completion.
        assert res.stdout.strip().endswith("{}"), (host, res.stdout, res.stderr)
        assert (tmp_path / host / "out").read_text().startswith('{"session_id":"s-4"}|')
