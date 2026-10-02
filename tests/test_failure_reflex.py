"""Contract tests for the host-observed repeat-failure reflex."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
HOOK = ROOT / "hooks" / "post-tool-failure"
sys.path.insert(0, str(SCRIPTS))

import failure_reflex  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "UNITARES_FAILURE_REFLEX",
        "UNITARES_FAILURE_REFLEX_THRESHOLD",
        "UNITARES_FAILURE_REFLEX_WINDOW_S",
        "UNITARES_FAILURE_REFLEX_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


def _payload(
    *,
    slot: str = "claude-slot",
    tool: str = "Bash",
    tool_input: dict | None = None,
    event: str = "PostToolUseFailure",
    **extra,
) -> str:
    body = {
        "session_id": slot,
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": tool_input
        if tool_input is not None
        else {
            "command": "pytest tests/test_x.py",
            "description": "run tests",
            "timeout": 120000,
        },
        # Field names per the documented PostToolUseFailure payload.
        "error_message": "Command failed with exit code 1",
        "tool_response": "SECRET-ERROR-TEXT",
        "exit_code": 1,
        "is_interrupt": False,
        "tool_use_id": "toolu_test",
    }
    body.update(extra)
    return json.dumps(body)


def _state(home: Path, slot: str = "claude-slot") -> dict:
    path = failure_reflex.reflex_state_path(home, slot)
    return json.loads(path.read_text(encoding="utf-8"))


def test_fires_on_third_identical_failure_and_not_before(tmp_path):
    results = [
        failure_reflex.observe(_payload(), home=tmp_path, now=100.0 + i)
        for i in range(3)
    ]
    assert [r[0] for r in results] == ["recorded", "recorded", "fired"]
    assert results[0][1] is None and results[1][1] is None
    context = results[2][1]
    assert "failed 3 times" in context
    assert "not a verdict" in context
    assert "Bash" in context


def test_refires_every_threshold_not_every_failure(tmp_path):
    statuses = [
        failure_reflex.observe(_payload(), home=tmp_path, now=100.0 + i)[0]
        for i in range(7)
    ]
    assert statuses == [
        "recorded",
        "recorded",
        "fired",
        "recorded",
        "recorded",
        "fired",
        "recorded",
    ]
    assert _state(tmp_path)["reflex_fired_total"] == 2


def test_different_calls_do_not_add_up(tmp_path):
    for i, cmd in enumerate(["ls a", "ls b", "ls c", "ls d"]):
        status, context = failure_reflex.observe(
            _payload(tool_input={"command": cmd}), home=tmp_path, now=100.0 + i
        )
        assert status == "recorded" and context is None
    assert _state(tmp_path)["failure_total"] == 4


def test_reworded_description_is_still_the_same_call(tmp_path):
    statuses = [
        failure_reflex.observe(
            _payload(tool_input={"command": "make", "description": f"try {i}"}),
            home=tmp_path,
            now=100.0 + i,
        )[0]
        for i in range(3)
    ]
    assert statuses[-1] == "fired"


def test_failures_outside_window_expire(tmp_path):
    failure_reflex.observe(_payload(), home=tmp_path, now=0.0)
    failure_reflex.observe(_payload(), home=tmp_path, now=1.0)
    status, _ = failure_reflex.observe(_payload(), home=tmp_path, now=10_000.0)
    assert status == "recorded"


def test_slots_are_isolated(tmp_path):
    for i in range(2):
        failure_reflex.observe(_payload(slot="a"), home=tmp_path, now=100.0 + i)
    status, _ = failure_reflex.observe(_payload(slot="b"), home=tmp_path, now=103.0)
    assert status == "recorded"


def test_ledger_stores_no_tool_content_or_identity(tmp_path):
    for i in range(3):
        failure_reflex.observe(_payload(), home=tmp_path, now=100.0 + i)
    raw = failure_reflex.reflex_state_path(tmp_path, "claude-slot").read_text()
    assert "pytest" not in raw
    assert "SECRET-ERROR-TEXT" not in raw
    state = json.loads(raw)
    assert state["network_emission"] == "none"
    assert state["evidence_source"] == "hook_derived"
    for forbidden in (
        "tool_input",
        "tool_response",
        "error_message",
        "uuid",
        "agent_id",
        "eisv",
    ):
        assert forbidden not in state


def test_interrupts_and_non_failure_events_are_ignored(tmp_path):
    for i in range(3):
        status, _ = failure_reflex.observe(
            _payload(is_interrupt=True), home=tmp_path, now=100.0 + i
        )
        assert status == "skip_interrupt"
    status, _ = failure_reflex.observe(
        _payload(event="PostToolUse"), home=tmp_path, now=200.0
    )
    assert status == "skip_not_failure"


def test_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("UNITARES_FAILURE_REFLEX", "off")
    status, _ = failure_reflex.observe(_payload(), home=tmp_path, now=1.0)
    assert status == "skip_disabled"


def test_threshold_is_configurable_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("UNITARES_FAILURE_REFLEX_THRESHOLD", "1")  # clamps to 2
    statuses = [
        failure_reflex.observe(_payload(), home=tmp_path, now=100.0 + i)[0]
        for i in range(2)
    ]
    assert statuses == ["recorded", "fired"]


def test_hook_emits_additional_context_json_only_when_firing(tmp_path):
    env = {
        **os.environ,
        "UNITARES_FAILURE_REFLEX_DIR": str(tmp_path),
        "UNITARES_FAILURE_REFLEX": "on",
        "UNITARES_FAILURE_REFLEX_THRESHOLD": "3",
    }
    outputs = []
    for _ in range(3):
        proc = subprocess.run(
            [str(HOOK), "--host", "claude"],
            input=_payload(),
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert proc.returncode == 0
        outputs.append(proc.stdout.strip())
    assert outputs[0] == "" and outputs[1] == ""
    body = json.loads(outputs[2])
    assert body["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert "failed 3 times" in body["hookSpecificOutput"]["additionalContext"]


def test_hook_is_registered_for_all_claude_tool_failures():
    hooks = json.loads((ROOT / "hooks" / "claude-hooks.json").read_text())
    groups = hooks["hooks"]["PostToolUseFailure"]
    commands = [
        (g.get("matcher"), h["command"]) for g in groups for h in g["hooks"]
    ]
    assert any(
        m == "*" and "post-tool-failure" in c for m, c in commands
    ), commands
