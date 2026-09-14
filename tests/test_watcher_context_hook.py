from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parent.parent
HOOK = ROOT / "hooks" / "watcher-context"


def _fake_agent(tmp_path: Path) -> tuple[Path, Path]:
    marker = tmp_path / "watcher-argv.jsonl"
    agent = tmp_path / "agent.py"
    agent.write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['WATCHER_MARKER']).open('a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print('<unitares-watcher-findings>fixture</unitares-watcher-findings>')\n",
        encoding="utf-8",
    )
    return agent, marker


def _run(
    tmp_path: Path,
    *,
    event: str,
    host: str,
    enabled: str = "1",
) -> subprocess.CompletedProcess[str]:
    agent, marker = _fake_agent(tmp_path)
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
        "UNITARES_WATCHER_ENABLED": enabled,
        "UNITARES_WATCHER_AGENT": str(agent),
        "WATCHER_MARKER": str(marker),
    }
    return subprocess.run(
        [str(HOOK), "--event", event, "--host", host],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )


def test_session_start_surfaces_read_only_backlog(tmp_path: Path) -> None:
    result = _run(tmp_path, event="session-start", host="codex")
    assert result.returncode == 0
    assert "unitares-watcher-findings" in result.stdout
    argv = json.loads((tmp_path / "watcher-argv.jsonl").read_text())
    assert argv == ["--print-unresolved"]


def test_prompt_uses_stable_host_worktree_audience(tmp_path: Path) -> None:
    first = _run(tmp_path, event="user-prompt-submit", host="codex")
    assert first.returncode == 0
    argv = json.loads((tmp_path / "watcher-argv.jsonl").read_text())
    assert argv[:2] == ["--surface-pending", "--audience"]
    assert re.fullmatch(r"codex:[0-9a-f]{20}", argv[2])


def test_hosts_get_distinct_delivery_audiences(tmp_path: Path) -> None:
    _run(tmp_path, event="user-prompt-submit", host="codex")
    codex_argv = json.loads((tmp_path / "watcher-argv.jsonl").read_text())
    (tmp_path / "watcher-argv.jsonl").unlink()
    _run(tmp_path, event="user-prompt-submit", host="claude")
    claude_argv = json.loads((tmp_path / "watcher-argv.jsonl").read_text())
    assert codex_argv[2].startswith("codex:")
    assert claude_argv[2].startswith("claude:")
    assert codex_argv[2].split(":", 1)[1] == claude_argv[2].split(":", 1)[1]


def test_disabled_watcher_is_silent(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        event="user-prompt-submit",
        host="codex",
        enabled="0",
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert not (tmp_path / "watcher-argv.jsonl").exists()
