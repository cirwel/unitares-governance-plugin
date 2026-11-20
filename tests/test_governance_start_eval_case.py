"""The governance-start eval case must agree with the command it grades.

The command reads the slot inventory only through the helper and starts fresh
when the helper cannot run. The eval gives the model no Bash, so its graders
must pass a fresh mint that names no candidate, and must fail any call that
reuses the scaffolded cache's session or uuid.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
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


CSID = "01a0b640-9040-72f2-8e06-dcddbe8b23d6"
UUID = "37391aae-393a-43bf-b05d-5583a949e427"
CACHE_PATH = f".unitares/session-{CSID}.json"


def test_scaffold_matches_the_ids_the_grader_names() -> None:
    scaffold = (CASE.parent / "scaffold.sh").read_text()
    assert f'"client_session_id": "{CSID}"' in scaffold
    assert f'"uuid": "{UUID}"' in scaffold
    assert CACHE_PATH in scaffold


@pytest.mark.parametrize(
    "text",
    [
        f'start_session(force_new=true, client_session_id="{CSID}")',
        f"sync_state(client_session_id='{CSID}', response_text='x')",
        f'identity(agent_uuid="{UUID}", resume=true)',
        f'onboard(agent_uuid="{UUID}", resume=true)',
        f'identity(agent_id="{UUID}")',
        # Any tool, not a fixed list.
        f'check_working_state(client_session_id="{CSID}")',
        f'get_governance_metrics(client_session_id="{CSID}")',
        # A closing parenthesis or brace inside a string argument before the id.
        f'sync_state(response_text="done (step 1)", client_session_id="{CSID}")',
        f'sync_state(response_text="a) b", client_session_id="{CSID}")',
        f'{{"tool": "sync_state", "input": {{"response_text": "x {{y}}", '
        f'"client_session_id": "{CSID}"}}}}',
        # JSON form, inline, wrapped and spread over lines.
        f'start_session({{"force_new": true, "client_session_id": "{CSID}"}})',
        f'identity({{"agent_uuid": "{UUID}", "resume": true}})',
        f'{{"name": "check_working_state", "arguments": {{"client_session_id": "{CSID}"}}}}',
        f'{{"name": "identity", "arguments": {{"agent_uuid": "{UUID}", "resume": true}}}}',
        f'{{\n  "client_session_id":\n    "{CSID}"\n}}',
        f"client_session_id: `{CSID}`",
    ],
)
def test_cached_session_reuse_fails(text: str) -> None:
    pattern = re.compile(_grader("no-cached-session-reuse")["pattern"])
    assert pattern.search(text), text


@pytest.mark.parametrize(
    "text",
    [
        'start_session(force_new=true, model_type="claude-opus-5-5")',
        "onboard(force_new=true)",
        "The cache holds client_session_id 01a0b640, which belongs to another process.",
        f"The cached uuid {UUID} is co-location, not a predecessor.",
        # Reading or naming the scaffolded cache file is not reuse, in any form.
        f"Read({CACHE_PATH})",
        f'Read(file_path="{CACHE_PATH}")',
        f'{{"name": "Read", "arguments": {{"file_path": "{CACHE_PATH}"}}}}',
        f'{{"tool": "Read", "input": {{"file_path": "/work/{CACHE_PATH}"}}}}',
        f'Glob(pattern=".unitares/session-*.json") -> {CACHE_PATH}',
        f"scripts/session_cache.py get session --slot={CSID}",
        # The explicit handoff form is graded by no-cached-parent, not here.
        f'start_session(force_new=true, parent_agent_id="{UUID}", spawn_reason="explicit")',
        f'start_session({{"force_new": true, "parent_agent_id": "{UUID}", '
        f'"spawn_reason": "explicit"}})',
    ],
)
def test_reading_or_naming_the_cache_passes(text: str) -> None:
    pattern = re.compile(_grader("no-cached-session-reuse")["pattern"])
    assert not pattern.search(text), text


def test_quoted_cache_record_fails_as_documented() -> None:
    # As text, a quoted record is indistinguishable from an argument object;
    # the case comment says so and the prompt asks only for the call.
    pattern = re.compile(_grader("no-cached-session-reuse")["pattern"])
    grader = _grader("no-cached-session-reuse")
    assert pattern.search(f'{{"uuid": "{UUID}", "client_session_id": "{CSID}"}}')
    assert pattern.search(f"The cache's uuid: {UUID} is another process's.")
    assert "Text in" in CASE.read_text() and "a quoted cache record" in CASE.read_text()
    assert grader["match"] == "not_contains"


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
