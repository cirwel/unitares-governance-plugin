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
HOSTS = [
    ("claude", "claude-hooks.json", "CLAUDE_PLUGIN_ROOT"),
    ("codex", "codex-hooks.json", "PLUGIN_ROOT"),
]


def _run(
    *args: str,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    shell: str | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    # HOME is the per-test sandbox from conftest.py, so hooks run here write
    # their receipts and caches there.
    base = {k: v for k, v in os.environ.items() if k not in ANCHOR_VARS}
    base.update(env or {})
    return subprocess.run(
        [*([shell] if shell else []), str(LAUNCHER), *args],
        text=True,
        capture_output=True,
        timeout=30,
        env=base,
        cwd=cwd,
        input=stdin,
    )


def _report(*names: str) -> list[str]:
    code = "import os; print(os.getpid()); " + "; ".join(
        f"print(os.environ.get({name!r}, '<unset>'))" for name in names
    )
    return [sys.executable, "-c", code]


def _executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    "thread_id",
    ["1573223283237847113", "1732062000.004200", "-1001234567890", "a_b-C.9", "x" * 128],
)
def test_cli_runs_as_the_owner_process_with_the_thread_anchor(shell, thread_id):
    result = _run(thread_id, "--", *_report(*ANCHOR_VARS[:3]), shell=shell)
    assert result.returncode == 0, result.stderr
    pid, anchor, marker, owner = result.stdout.split()
    assert anchor == f"agent:/thread-{thread_id}"
    assert marker == "1"
    # The CLI replaced the launcher, so the declared owner is the CLI itself.
    assert owner == pid


def _hook_probe(manifest: str) -> str:
    """Python source for a stand-in CLI: run the shipped PreToolUse hook command
    for a bare start_session() call and print what the hook did as JSON."""
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
        "import json, subprocess; "
        f"r = subprocess.run(['/bin/sh', '-c', {command!r}], input={payload!r}, "
        "text=True, capture_output=True, timeout=20); "
        "print(json.dumps({'rc': r.returncode, 'out': r.stdout, 'err': r.stderr}))"
    )


@pytest.mark.parametrize("host,manifest,root_var", HOSTS)
def test_shipped_hook_binds_the_launched_cli_to_the_thread(tmp_path, host, manifest, root_var):
    result = _run(
        "t1", "--", sys.executable, "-c", _hook_probe(manifest),
        env={root_var: str(ROOT)}, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    hook = json.loads(result.stdout)
    assert hook["rc"] == 0 and hook["err"].strip() == "", hook
    updated = json.loads(hook["out"])["hookSpecificOutput"]["updatedInput"]
    assert updated["client_session_id"] == "agent:/thread-t1"
    assert updated["orchestrated"] is True


@pytest.mark.parametrize("host,manifest,root_var", HOSTS)
def test_shipped_hook_refuses_the_anchor_to_a_nested_cli(tmp_path, host, manifest, root_var):
    # The launched CLI starts another one, which inherits the whole environment.
    outer = (
        "import subprocess, sys; "
        f"r = subprocess.run([sys.executable, '-c', {_hook_probe(manifest)!r}], "
        "text=True, capture_output=True); "
        "sys.stdout.write(r.stdout); sys.stderr.write(r.stderr); sys.exit(r.returncode)"
    )
    result = _run(
        "t1", "--", sys.executable, "-c", outer,
        env={root_var: str(ROOT)}, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    hook = json.loads(result.stdout)
    # The hook ran to completion and declined to rewrite the call; an early
    # failure would also leave no anchor in its output.
    assert hook["rc"] == 0 and hook["err"].strip() == "", hook
    assert "agent:/thread-t1" not in hook["out"]
    assert "orchestrated" not in hook["out"]


def test_inherited_lineage_is_cleared():
    result = _run(
        "t1",
        "--",
        *_report("UNITARES_PARENT_AGENT_ID", "UNITARES_SPAWN_REASON"),
        env={"UNITARES_PARENT_AGENT_ID": "someone-else", "UNITARES_SPAWN_REASON": "dispatch"},
    )
    assert result.stdout.split()[1:] == ["<unset>", "<unset>"]


@pytest.mark.parametrize("shell", SHELLS)
def test_arguments_input_and_exit_status_pass_through(shell):
    code = "import sys; print(sys.argv[1:]); print(sys.stdin.read()); sys.exit(7)"
    result = _run("t1", "--", sys.executable, "-c", code, "a b", "--", "c", shell=shell, stdin="turn text")
    assert result.returncode == 7
    assert result.stdout.splitlines() == ["['a b', '--', 'c']", "turn text"]


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["t1"],
        ["t1", "--"],
        ["t1", "touch", "ran"],
        ["", "--", "touch", "ran"],
        ["a/b", "--", "touch", "ran"],
        ["a b", "--", "touch", "ran"],
        ["a:b", "--", "touch", "ran"],
        ["x" * 129, "--", "touch", "ran"],
        # Forms the server would rewrite, putting two ids on one identity.
        ["team..ops", "--", "touch", "ran"],
        ["café", "--", "touch", "ran"],
    ],
)
@pytest.mark.parametrize("locale", ["C", "en_US.UTF-8"])
@pytest.mark.parametrize("shell", SHELLS)
def test_bad_invocations_are_refused_before_anything_runs(tmp_path, args, locale, shell):
    result = _run(*args, env={"LC_ALL": locale}, shell=shell, cwd=tmp_path)
    assert result.returncode == 64
    assert "usage:" in result.stderr
    assert result.stdout == ""
    assert not (tmp_path / "ran").exists()


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("name", ["no-such-cli-for-thread-anchor-exec", "./no-such-cli", "if", ":"])
def test_missing_cli_is_reported(tmp_path, shell, name):
    # `if` and `:` exist only inside the shell; there is no such program.
    result = _run("t1", "--", name, shell=shell, env={"PATH": str(tmp_path)}, cwd=tmp_path)
    assert result.returncode == 127
    assert "command not found" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("shell", SHELLS)
def test_cli_that_cannot_run_is_told_apart_from_a_missing_one(tmp_path, shell):
    not_executable = tmp_path / "cli"
    not_executable.write_text("#!/bin/sh\n")
    by_path = [str(not_executable), str(tmp_path)]
    for target, env in [(by_path[0], {}), (by_path[1], {}), ("cli", {"PATH": str(tmp_path)})]:
        result = _run("t1", "--", target, shell=shell, env=env)
        assert result.returncode == 126, (target, result.stderr)
        assert "cannot execute" in result.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_cli_is_found_on_path_past_a_copy_that_cannot_run(tmp_path, shell):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "cli").write_text("not runnable\n")
    _executable(second / "cli", "#!/bin/sh\necho ran \"$UNITARES_CLIENT_SESSION_ID\"\n")
    result = _run("t1", "--", "cli", shell=shell, env={"PATH": f"{first}:{second}:/usr/bin:/bin"})
    assert result.stdout.strip() == "ran agent:/thread-t1", result.stderr


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("name", ["true", "-c", "-l"])
def test_cli_named_like_a_builtin_or_an_option_still_runs_anchored(tmp_path, shell, name):
    # `true` is a shell builtin, and `-c` / `-l` are options of bash's `exec`
    # (`-c` would clear the environment and with it the anchor).
    _executable(tmp_path / name, "#!/bin/sh\necho \"$UNITARES_CLIENT_SESSION_ID\"\n")
    result = _run("t1", "--", name, shell=shell, env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "agent:/thread-t1"
