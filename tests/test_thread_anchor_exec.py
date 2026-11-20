"""scripts/thread-anchor-exec: the launcher an orchestrator uses for one turn."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "thread-anchor-exec"
ANCHOR_VARS = (
    "UNITARES_CLIENT_SESSION_ID",
    "UNITARES_ORCHESTRATED",
    "UNITARES_ORCHESTRATED_OWNER_PID",
    "UNITARES_PARENT_AGENT_ID",
    "UNITARES_SPAWN_REASON",
)


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k not in ANCHOR_VARS}
    base.update(env or {})
    return subprocess.run(
        [str(LAUNCHER), *args],
        text=True,
        capture_output=True,
        timeout=20,
        env=base,
    )


def _report(*names: str) -> list[str]:
    code = "import os; print(os.getpid()); " + "; ".join(
        f"print(os.environ.get({name!r}, '<unset>'))" for name in names
    )
    return [sys.executable, "-c", code]


def test_cli_runs_as_the_owner_process_with_the_thread_anchor():
    result = _run("1573223283237847113", "--", *_report(*ANCHOR_VARS[:3]))
    assert result.returncode == 0, result.stderr
    pid, anchor, marker, owner = result.stdout.split()
    assert anchor == "agent:/thread-1573223283237847113"
    assert marker == "1"
    # The CLI replaced the launcher, so the declared owner is the CLI itself.
    assert owner == pid


def test_hook_launcher_would_accept_the_anchor():
    # hooks/run-hook.cmd takes its parent PID as the hook host and keeps the
    # anchor only when that equals the declared owner.
    code = (
        "import os, subprocess; "
        "print(subprocess.run(['sh', '-c', "
        "'[ \"$PPID\" = \"$UNITARES_ORCHESTRATED_OWNER_PID\" ] && echo kept || echo dropped'], "
        "text=True, capture_output=True).stdout.strip())"
    )
    result = _run("t1", "--", sys.executable, "-c", code)
    assert result.stdout.strip() == "kept", result.stderr


def test_plugin_forwards_the_anchor_for_the_launched_cli():
    code = (
        "import os, sys; "
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
        "os.environ['UNITARES_HOOK_HOST_PID'] = str(os.getpid()); "
        "import governance_call_inject as g; print(g._env_anchor())"
    )
    result = _run("t1", "--", sys.executable, "-c", code)
    assert result.stdout.strip() == "agent:/thread-t1", result.stderr


def test_nested_cli_is_refused_the_inherited_anchor():
    inner = (
        "import os, sys; "
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
        "os.environ['UNITARES_HOOK_HOST_PID'] = str(os.getpid()); "
        "import governance_call_inject as g; print(repr(g._env_anchor()))"
    )
    outer = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {inner!r}])"
    result = _run("t1", "--", sys.executable, "-c", outer)
    assert result.stdout.strip() == "''", result.stderr


def test_inherited_lineage_is_cleared():
    result = _run(
        "t1",
        "--",
        *_report("UNITARES_PARENT_AGENT_ID", "UNITARES_SPAWN_REASON"),
        env={"UNITARES_PARENT_AGENT_ID": "someone-else", "UNITARES_SPAWN_REASON": "dispatch"},
    )
    assert result.stdout.split()[1:] == ["<unset>", "<unset>"]


def test_exit_status_and_arguments_pass_through():
    result = _run("t1", "--", sys.executable, "-c", "import sys; print(sys.argv[1:]); sys.exit(7)", "a b", "--", "c")
    assert result.returncode == 7
    assert result.stdout.strip() == "['a b', '--', 'c']"


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["t1"],
        ["t1", "--"],
        ["t1", "true"],
        ["", "--", "true"],
        ["a/b", "--", "true"],
        ["a b", "--", "true"],
        ["x" * 129, "--", "true"],
    ],
)
def test_bad_invocations_are_refused_before_anything_runs(args):
    result = _run(*args)
    assert result.returncode == 64
    assert "usage:" in result.stderr
    assert result.stdout == ""


def test_missing_cli_is_reported():
    result = _run("t1", "--", "no-such-cli-for-thread-anchor-exec")
    assert result.returncode == 127
    assert "command not found" in result.stderr
