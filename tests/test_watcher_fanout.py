from __future__ import annotations

import json
from pathlib import Path

from scripts.watcher_fanout import fan_out


def test_fan_out_normalizes_each_path_for_the_watcher(tmp_path: Path) -> None:
    marker = tmp_path / "events.jsonl"
    hook = tmp_path / "watcher-hook"
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    hook.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "pathlib.Path(os.environ['WATCHER_MARKER']).open('ab').write(sys.stdin.buffer.read() + b'\\n')\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    import os

    previous = os.environ.get("WATCHER_MARKER")
    os.environ["WATCHER_MARKER"] = str(marker)
    try:
        assert (
            fan_out(
                hook,
                ["/repo/a.py", "src/b.ts", "../shared/c.py"],
                host="codex",
                workspace=workspace,
            )
            == 3
        )
    finally:
        if previous is None:
            os.environ.pop("WATCHER_MARKER", None)
        else:
            os.environ["WATCHER_MARKER"] = previous

    events = [json.loads(line) for line in marker.read_text().splitlines()]
    assert [event["tool_input"]["file_path"] for event in events] == [
        "/repo/a.py",
        str(workspace / "src/b.ts"),
        str(tmp_path / "shared/c.py"),
    ]
    assert all(event["tool_name"] == "Edit" for event in events)
    assert all(event["source_host"] == "codex" for event in events)


def test_fan_out_fails_silent_for_untrusted_or_invalid_input(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert fan_out(missing, ["/repo/a.py"], host="codex") == 0
    assert fan_out(missing, ["/repo/a.py"], host="unknown") == 0
