"""Resource observation shadow collector (cirwel/unitares#2410).

The collector's value as a shadow instrument depends on its boundaries, so the
tests pin those first: default off, local only, minimal content, never failing
a turn. Behavior tests follow.
"""

from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
HOOK = PLUGIN_ROOT / "hooks" / "resource-observe"
SCRIPT = PLUGIN_ROOT / "scripts" / "resource_shadow.py"

sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
import resource_shadow  # noqa: E402

UUID = "86ae619f-87e0-4040-8f29-eacece0c7904"


def _assistant(tokens: tuple[int, int, int], *, model="claude-test-1", sidechain=False):
    inp, create, read = tokens
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": create,
                "cache_read_input_tokens": read,
                "output_tokens": 999,
            },
        },
    }


def _transcript(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def _run_hook(payload: dict, *, env_extra: dict, cwd: Path, event: str = "stop"):
    env = os.environ.copy()
    env.pop("UNITARES_RESOURCE_SHADOW", None)
    env.update(env_extra)
    return subprocess.run(
        [str(HOOK), "--event", event, "--host", "claude"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=15,
        cwd=str(cwd),
        env=env,
    )


def _rows(directory: Path) -> list[dict]:
    rows = []
    for path in sorted(directory.glob("*.jsonl")):
        rows.extend(json.loads(line) for line in path.read_text().splitlines())
    return rows


# --- boundaries -----------------------------------------------------------


def test_default_off_writes_nothing(tmp_path):
    out = tmp_path / "shadow"
    transcript = _transcript(tmp_path, [_assistant((1, 2, 50_000))])
    result = _run_hook(
        {"session_id": "s1", "transcript_path": str(transcript)},
        env_extra={"UNITARES_RESOURCE_SHADOW_DIR": str(out)},
        cwd=tmp_path,
    )
    assert result.returncode == 0
    assert result.stdout == "" and result.stderr == ""
    assert not out.exists()


@pytest.mark.parametrize("value", ["0", "off", "false", "", "no"])
def test_false_like_values_stay_off(value):
    assert resource_shadow.enabled({"UNITARES_RESOURCE_SHADOW": value}) is False


@pytest.mark.parametrize("value", ["1", "on", "TRUE", "yes"])
def test_truthy_values_enable(value):
    assert resource_shadow.enabled({"UNITARES_RESOURCE_SHADOW": value}) is True


def test_collector_has_no_network_path():
    tree = ast.parse(SCRIPT.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "subprocess"}
    assert not imported & forbidden
    # The only plugin-local import is the read-only session lookup.
    assert imported - set(sys.stdlib_module_names) == {"_session_lookup"}


def test_tool_failure_records_name_only(tmp_path):
    record = resource_shadow.build_record(
        "tool-failure",
        "claude",
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "cat ~/.config/secret"},
            "error": "permission denied: /Users/someone/private",
            "is_interrupt": False,
            "cwd": "/Users/someone/project",
        },
        agent_uuid=UUID,
    )
    blob = json.dumps(record)
    assert record["tool_name"] == "Bash"
    assert record["is_interrupt"] is False
    for leaked in ("secret", "permission denied", "/Users/someone", "command"):
        assert leaked not in blob
    assert set(record) == {
        "schema", "ts", "host", "event", "session_id", "agent_uuid",
        "tool_name", "is_interrupt",
    }


def test_session_cache_token_is_never_copied(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    (workspace / ".unitares").mkdir(parents=True)
    (workspace / ".unitares" / "session-s1.json").write_text(json.dumps({
        "uuid": UUID,
        "client_session_id": "agent-86ae619f-87e",
        "continuity_token": "v1.SHOULD-NEVER-APPEAR",
    }))
    out = tmp_path / "shadow"
    transcript = _transcript(tmp_path, [_assistant((1, 2, 50_000))])
    result = _run_hook(
        {"session_id": "s1", "transcript_path": str(transcript)},
        env_extra={
            "UNITARES_RESOURCE_SHADOW": "1",
            "UNITARES_RESOURCE_SHADOW_DIR": str(out),
        },
        cwd=workspace,
    )
    assert result.returncode == 0
    rows = _rows(out)
    assert len(rows) == 1
    assert rows[0]["agent_uuid"] == UUID
    raw = "".join(p.read_text() for p in out.glob("*.jsonl"))
    assert "SHOULD-NEVER-APPEAR" not in raw
    assert "agent-86ae619f" not in raw


def test_malformed_input_never_fails_the_turn(tmp_path):
    out = tmp_path / "shadow"
    env = os.environ.copy()
    env.update({"UNITARES_RESOURCE_SHADOW": "1", "UNITARES_RESOURCE_SHADOW_DIR": str(out)})
    for body in ("not json", "[1,2]", ""):
        result = subprocess.run(
            [str(HOOK), "--event", "stop", "--host", "claude"],
            input=body, text=True, capture_output=True, timeout=15,
            cwd=str(tmp_path), env=env,
        )
        assert result.returncode == 0
        assert result.stdout == "" and result.stderr == ""


def test_unwritable_directory_never_fails_the_turn(tmp_path):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    result = _run_hook(
        {"session_id": "s1"},
        env_extra={
            "UNITARES_RESOURCE_SHADOW": "1",
            "UNITARES_RESOURCE_SHADOW_DIR": str(blocker / "sub"),
        },
        cwd=tmp_path,
    )
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""


def test_codex_host_and_unknown_event_are_ignored(tmp_path):
    out = tmp_path / "shadow"
    env = {"UNITARES_RESOURCE_SHADOW": "1", "UNITARES_RESOURCE_SHADOW_DIR": str(out)}
    assert resource_shadow.main(["--host", "codex", "--event", "stop"], env=env) == 0
    assert resource_shadow.main(["--host", "claude", "--event", "bogus"], env=env) == 0
    assert not out.exists()


def test_file_and_directory_permissions(tmp_path):
    out = tmp_path / "shadow"
    record = resource_shadow.build_record("tool-failure", "claude", {"tool_name": "Read"}, agent_uuid=None)
    path = resource_shadow.append_record(out, record)
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --- behavior -------------------------------------------------------------


@pytest.mark.parametrize(
    ("tokens", "band"),
    [(None, None), (-1, None), (0, 0), (7_999, 0), (8_000, 8_000), (150_000, 128_000),
     (713_578, 512_000), (1_000_000, 1_000_000), (5_000_000, 1_000_000)],
)
def test_ctx_band(tokens, band):
    assert resource_shadow.ctx_band(tokens) == band


def test_stop_uses_last_main_chain_message(tmp_path):
    transcript = _transcript(tmp_path, [
        _assistant((2, 100, 20_000)),
        {"type": "user", "message": {"content": "hi"}},
        _assistant((2, 500, 140_000), model="claude-main-1"),
        _assistant((1, 1, 900_000), model="claude-sub-1", sidechain=True),
        {"type": "system", "subtype": "note"},
    ])
    record = resource_shadow.build_record(
        "stop", "claude", {"session_id": "s1", "transcript_path": str(transcript)}, agent_uuid=UUID,
    )
    assert record["ctx_tokens_band"] == 128_000
    assert record["model"] == "claude-main-1"
    assert record["agent_uuid"] == UUID
    assert record["schema"] == "resource_shadow.v1"
    assert record["ts"].endswith("Z")


def test_missing_transcript_records_null_band(tmp_path):
    record = resource_shadow.build_record(
        "stop", "claude", {"session_id": "s1", "transcript_path": str(tmp_path / "gone.jsonl")},
        agent_uuid=None,
    )
    assert record["ctx_tokens_band"] is None and record["model"] is None


def test_long_transcript_tail_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(resource_shadow, "TRANSCRIPT_TAIL_BYTES", 4_000)
    filler = [{"type": "user", "message": {"content": "x" * 200}} for _ in range(100)]
    transcript = _transcript(tmp_path, [_assistant((0, 0, 300_000))] + filler)
    tokens, _model = resource_shadow.last_main_chain_usage(str(transcript))
    assert tokens is None  # the only usage row is outside the bounded tail
    transcript = _transcript(tmp_path, filler + [_assistant((0, 0, 300_000))])
    tokens, _model = resource_shadow.last_main_chain_usage(str(transcript))
    assert tokens == 300_000


def test_pre_compact_trigger(tmp_path):
    transcript = _transcript(tmp_path, [_assistant((0, 0, 190_000))])
    for trigger, expected in (("auto", "auto"), ("manual", "manual"), ("other", "unknown"), (None, "unknown")):
        record = resource_shadow.build_record(
            "pre-compact", "claude",
            {"session_id": "s1", "transcript_path": str(transcript), "trigger": trigger},
            agent_uuid=None,
        )
        assert record["compact_trigger"] == expected
        assert record["ctx_tokens_band"] == 128_000


def test_unsafe_tool_and_session_values_are_scrubbed():
    record = resource_shadow.build_record(
        "tool-failure", "claude",
        {"session_id": "../../etc/passwd", "tool_name": "rm -rf /; echo"},
        agent_uuid=None,
    )
    assert record["tool_name"] == "other"
    assert record["session_id"] is None


def test_end_to_end_appends_one_row_per_event(tmp_path):
    out = tmp_path / "shadow"
    transcript = _transcript(tmp_path, [_assistant((2, 3, 70_000))])
    env = {"UNITARES_RESOURCE_SHADOW": "1", "UNITARES_RESOURCE_SHADOW_DIR": str(out)}
    base = {"session_id": "s1", "transcript_path": str(transcript)}
    _run_hook(base, env_extra=env, cwd=tmp_path, event="stop")
    _run_hook({**base, "tool_name": "mcp__x__y"}, env_extra=env, cwd=tmp_path, event="tool-failure")
    _run_hook({**base, "trigger": "auto"}, env_extra=env, cwd=tmp_path, event="pre-compact")
    rows = _rows(out)
    assert [r["event"] for r in rows] == ["stop", "tool-failure", "pre-compact"]
    assert rows[0]["ctx_tokens_band"] == 64_000
    assert rows[1]["tool_name"] == "mcp__x__y"


# --- manifest -------------------------------------------------------------


def _resource_handlers(config: dict) -> list[tuple[str, dict, dict]]:
    found = []
    for event, groups in config["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                if " resource-observe " in handler["command"]:
                    found.append((event, group, handler))
    return found


def test_claude_manifest_registers_async_resource_handlers():
    config = json.loads((PLUGIN_ROOT / "hooks" / "claude-hooks.json").read_text())
    handlers = _resource_handlers(config)
    by_event = {event: handler for event, _group, handler in handlers}
    assert set(by_event) == {"Stop", "PostToolUseFailure", "PreCompact"}
    assert all(handler.get("async") is True for handler in by_event.values())
    assert "--event stop" in by_event["Stop"]["command"]
    assert "--event tool-failure" in by_event["PostToolUseFailure"]["command"]
    assert "--event pre-compact" in by_event["PreCompact"]["command"]


def test_codex_manifest_does_not_register_resource_handlers():
    config = json.loads((PLUGIN_ROOT / "hooks" / "codex-hooks.json").read_text())
    assert _resource_handlers(config) == []
