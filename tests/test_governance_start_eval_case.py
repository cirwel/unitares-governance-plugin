"""The governance-start eval case must agree with the command it grades.

The command reads the slot inventory only through the helper and starts fresh
when the helper cannot run. The eval gives the model no Bash, so its graders
must pass a fresh mint that names no candidate, and must fail any call that
reuses the scaffolded cache's session or uuid.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "evals" / "governance-start-command" / "case.yaml"
COMMAND = ROOT / "commands" / "governance-start.md"


def _case() -> dict:
    return yaml.safe_load(CASE.read_text())


def _grader(name: str) -> dict:
    return next(g for g in _case()["graders"] if g["name"] == name)


def test_regex_graders_compile() -> None:
    for grader in _case()["graders"]:
        if grader["type"] == "regex":
            re.compile(grader["pattern"])


def test_cached_session_reuse_is_graded_inside_calls_only() -> None:
    pattern = re.compile(_grader("no-cached-session-reuse")["pattern"])

    reused = [
        'start_session(force_new=true, client_session_id="01a0b640-9040-72f2-8e06-dcddbe8b23d6")',
        "sync_state(client_session_id='01a0b640-9040-72f2-8e06-dcddbe8b23d6', response_text='x')",
        'identity(agent_uuid="37391aae-393a-43bf-b05d-5583a949e427", resume=true)',
        # Tools outside the original list, and JSON-form arguments.
        'check_working_state(client_session_id="01a0b640-9040-72f2-8e06-dcddbe8b23d6")',
        'get_governance_metrics(client_session_id="01a0b640-9040-72f2-8e06-dcddbe8b23d6")',
        'start_session({"force_new": true, "client_session_id": "01a0b640-9040-72f2-8e06-dcddbe8b23d6"})',
        'identity({"agent_uuid": "37391aae-393a-43bf-b05d-5583a949e427", "resume": true})',
        '{"name": "check_working_state", "arguments": '
        '{"client_session_id": "01a0b640-9040-72f2-8e06-dcddbe8b23d6"}}',
        '{"name": "identity", "arguments": '
        '{"agent_uuid": "37391aae-393a-43bf-b05d-5583a949e427", "resume": true}}',
    ]
    allowed = [
        'start_session(force_new=true, model_type="claude-opus-5-5")',
        "The cache holds client_session_id 01a0b640, which belongs to another process.",
        "onboard(force_new=true)",
        # Reading or quoting the scaffolded cache file is not reuse.
        "Read(.unitares/session-01a0b640-9040-72f2-8e06-dcddbe8b23d6.json)",
        '{"uuid": "37391aae-393a-43bf-b05d-5583a949e427", '
        '"client_session_id": "01a0b640-9040-72f2-8e06-dcddbe8b23d6"}',
        # The explicit handoff form is graded by no-cached-parent, not here.
        'start_session(force_new=true, parent_agent_id="37391aae-393a-43bf-b05d-5583a949e427", '
        'spawn_reason="explicit")',
    ]

    assert all(pattern.search(text) for text in reused)
    assert not any(pattern.search(text) for text in allowed)


def test_fresh_identity_accepts_the_helper_unavailable_path() -> None:
    case = _case()
    command = COMMAND.read_text()
    criteria = _grader("fresh-identity")["criteria"]

    # Without Bash the model cannot run the helper, and the command then says
    # to start fresh without a candidate, so the grader must not require the
    # cache to have been found.
    assert "Bash" not in case["execution"]["allowed_tools"]
    assert "If the helper is unavailable, start fresh without a candidate" in command
    assert "without looking for one" not in criteria
    assert "helper could not be run" in criteria
    assert "reuses a client_session_id" in criteria
