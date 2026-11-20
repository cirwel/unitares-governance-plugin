"""scripts/thread-anchor-exec: the launcher an orchestrator uses for one turn."""

from __future__ import annotations

import json
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


SHELLS = [sh for sh in ("/bin/sh", "/bin/dash", "/usr/bin/dash") if Path(sh).exists()]


def _run(
    *args: str,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    shell: str | None = None,
) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k not in ANCHOR_VARS}
    base.update(env or {})
    return subprocess.run(
        [*([shell] if shell else []), str(LAUNCHER), *args],
        text=True,
        capture_output=True,
        timeout=20,
        env=base,
        cwd=cwd,
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


HOSTS = [
    ("claude", "claude-hooks.json", "CLAUDE_PLUGIN_ROOT"),
    ("codex", "codex-hooks.json", "PLUGIN_ROOT"),
]


def _hook_probe(manifest: str) -> str:
    """Python source for a stand-in CLI: run the shipped PreToolUse hook command
    for a bare start_session() call and print what the hook answered."""
    entries = json.loads((ROOT / "hooks" / manifest).read_text())["hooks"]["PreToolUse"]
    command = next(
        entry["hooks"][0]["command"] for entry in entries if "pre-governance-call" in str(entry)
    )
    payload = json.dumps(
        {
            "session_id": "thread-anchor-exec-test",
            "hook_event_name": "PreToolUse",
            "tool_name": "mcp__unitares-governance__start_session",
            "tool_input": {},
        }
    )
    return (
        "import subprocess, sys; "
        f"r = subprocess.run(['/bin/sh', '-c', {command!r}], input={payload!r}, "
        "text=True, capture_output=True, timeout=20); "
        "sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)"
    )


@pytest.mark.parametrize("host,manifest,root_var", HOSTS)
def test_shipped_hook_binds_the_launched_cli_to_the_thread(tmp_path, host, manifest, root_var):
    result = _run(
        "t1", "--", sys.executable, "-c", _hook_probe(manifest),
        env={root_var: str(ROOT)}, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "ignored unowned" not in result.stderr
    updated = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]
    assert updated["client_session_id"] == "agent:/thread-t1"
    assert updated["orchestrated"] is True


@pytest.mark.parametrize("host,manifest,root_var", HOSTS)
def test_shipped_hook_refuses_the_anchor_to_a_nested_cli(tmp_path, host, manifest, root_var):
    # The launched CLI starts another one, which inherits the whole environment.
    nested = _hook_probe(manifest)
    outer = (
        "import subprocess, sys; "
        f"r = subprocess.run([sys.executable, '-c', {nested!r}], text=True, capture_output=True); "
        "sys.stdout.write(r.stdout)"
    )
    result = _run(
        "t1", "--", sys.executable, "-c", outer,
        env={root_var: str(ROOT)}, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "agent:/thread-t1" not in result.stdout
    assert "orchestrated" not in result.stdout


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
        # Forms the server would rewrite, putting two ids on one identity.
        ["a.b", "--", "true"],
        ["team..ops", "--", "true"],
        ["caf\u00e9", "--", "true"],
        ["x" * 129, "--", "true"],
    ],
)
@pytest.mark.parametrize("locale", ["C", "en_US.UTF-8"])
@pytest.mark.parametrize("shell", SHELLS)
def test_bad_invocations_are_refused_before_anything_runs(args, locale, shell):
    result = _run(*args, env={"LC_ALL": locale}, shell=shell)
    assert result.returncode == 64
    assert "usage:" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("name", ["no-such-cli-for-thread-anchor-exec", "./no-such-cli"])
def test_missing_cli_is_reported(shell, name):
    result = _run("t1", "--", name, shell=shell)
    assert result.returncode == 127
    assert "command not found" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("shell", SHELLS)
def test_cli_that_cannot_run_is_told_apart_from_a_missing_one(tmp_path, shell):
    not_executable = tmp_path / "cli"
    not_executable.write_text("#!/bin/sh\n")
    for target in (not_executable, tmp_path):
        result = _run("t1", "--", str(target), shell=shell)
        assert result.returncode == 126
        assert "cannot execute" in result.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_cli_found_on_path_runs(shell):
    # `true` is also a shell builtin; it must still be found and run.
    assert _run("t1", "--", "true", shell=shell).returncode == 0
