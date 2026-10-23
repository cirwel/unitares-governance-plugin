"""The opt-in continuity question: asked at a compaction, answered at the turn's end."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import continuity_probe  # noqa: E402
from test_compaction_continuity import BINDING, HOSTS, SLOT, UUID, _cache, _event  # noqa: E402
from test_session_start_checkin import (  # noqa: E402
    PLUGIN_ROOT,
    RecordingHandler,
    _context,
    _ReusableTCPServer,
    _serve_and_run,
)

COMPACTED_AT = "2026-10-22T18:00:00.000000Z"
SUCCESSOR = "11111111-2222-4333-8444-555555555555"
PROBE_ON = {"UNITARES_CONTINUITY_PROBE": "on"}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    # Markers live under HOME; the hooks run with HOME=tmp_path as well.
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture
def probe_on(monkeypatch):
    monkeypatch.setenv("UNITARES_CONTINUITY_PROBE", "on")
    monkeypatch.delenv("UNITARES_CHECKINS", raising=False)


def _marker(**overrides):
    path = continuity_probe.marker_path(SLOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "compaction_agent_uuid": UUID,
        "compaction_observed_at": COMPACTED_AT,
        "host": "claude",
        "probe_version": "v1",
        "guidance_present": True,
        "asked_at": time.time(),
        **overrides,
    }))
    return path


def _stop(message, **extra):
    return json.dumps({"session_id": SLOT, "last_assistant_message": message, **extra})


@pytest.mark.parametrize(
    ("message", "answer"),
    [
        ("Done.\ncontinuity-probe: same", "same_process"),
        ("Done.\n\n`continuity-probe: new`", "new_process"),
        ("**Continuity-Probe: Cannot-Tell**", "cannot_tell"),
        ("continuity-probe: cannot tell", "cannot_tell"),
        ("continuity-probe: new\nOn reflection:\ncontinuity-probe: same", "same_process"),
        ("Done, no answer line.", "no_answer"),
        ("continuity-probe: maybe", "no_answer"),
        # A mention inside a sentence is not an answer line.
        ("I will add continuity-probe: same later.", "no_answer"),
        ("", "no_answer"),
    ],
)
def test_parse_answer_reads_the_last_answer_line(message, answer):
    assert continuity_probe.parse_answer(message) == answer


def test_nothing_is_asked_unless_opted_in(monkeypatch):
    monkeypatch.delenv("UNITARES_CONTINUITY_PROBE", raising=False)
    assert continuity_probe.should_ask(json.dumps(_event())) is None


def test_the_checkin_kill_switch_also_turns_the_probe_off(probe_on, monkeypatch):
    monkeypatch.setenv("UNITARES_CHECKINS", "off")
    assert continuity_probe.enabled() is False
    assert continuity_probe.should_ask(json.dumps(_event())) is None


def test_the_marker_is_private_and_holds_no_proof(tmp_path, probe_on):
    slot = continuity_probe.should_ask(json.dumps(_event()))
    assert slot == SLOT
    assert continuity_probe.record_asked(
        slot, host="codex",
        compaction={"agent_uuid": UUID, "observed_at": COMPACTED_AT,
                    "client_session_id": BINDING},
    )
    marker = continuity_probe.marker_path(SLOT)
    assert marker.parent == tmp_path / ".unitares"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    data = json.loads(marker.read_text())
    assert data["compaction_agent_uuid"] == UUID
    assert data["compaction_observed_at"] == COMPACTED_AT
    assert data["guidance_present"] is True
    assert data["probe_version"] == continuity_probe.PROBE_VERSION
    assert BINDING not in marker.read_text()


def test_the_answer_is_found_after_the_agent_changes_directory(tmp_path):
    # The cache is in the workspace; Stop runs from elsewhere and reaches the
    # HOME mirror of the same slot. The marker does not move with it.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _cache(workspace)
    _cache(tmp_path)
    _marker()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    payload = continuity_probe.build_payload(
        _stop("continuity-probe: same"), host="claude", cwd=str(elsewhere)
    )
    assert payload["probe_answer"] == "same_process"


def test_the_answer_goes_on_the_current_binding_and_names_the_compaction(tmp_path):
    _cache(tmp_path)
    marker = _marker()
    payload = continuity_probe.build_payload(
        _stop("Finished.\ncontinuity-probe: same"), host="claude", cwd=str(tmp_path)
    )
    assert payload["observation_kind"] == "context_compaction_probe"
    assert payload["agent_uuid"] == UUID
    assert payload["client_session_id"] == BINDING
    assert payload["probe_answer"] == "same_process"
    assert payload["compaction_agent_uuid"] == UUID
    assert payload["compaction_observed_at"] == COMPACTED_AT
    assert payload["guidance_present"] is True
    # Only the answer's value leaves; the message text does not.
    assert "Finished" not in json.dumps(payload)
    assert not marker.exists()
    # Answered once: the next Stop finds nothing.
    assert continuity_probe.build_payload(
        _stop("continuity-probe: new"), host="claude", cwd=str(tmp_path)
    ) is None


def test_an_agent_that_minted_answers_from_its_new_identity(tmp_path):
    _cache(tmp_path, uuid=SUCCESSOR, client_session_id="agent-successor-binding")
    _marker()
    payload = continuity_probe.build_payload(
        _stop("continuity-probe: new"), host="codex", cwd=str(tmp_path)
    )
    assert payload["agent_uuid"] == SUCCESSOR
    assert payload["compaction_agent_uuid"] == UUID
    assert payload["probe_answer"] == "new_process"


def test_a_turn_without_an_answer_is_recorded_as_none_given(tmp_path):
    _cache(tmp_path)
    _marker()
    payload = continuity_probe.build_payload(_stop("Done."), host="claude", cwd=str(tmp_path))
    assert payload["probe_answer"] == "no_answer"


@pytest.mark.parametrize(
    "case", ["stale", "inherited", "unbound", "bad_marker", "foreign_host"]
)
def test_unusable_markers_are_consumed_without_an_answer(tmp_path, case):
    host = "claude"
    if case == "inherited":
        _cache(tmp_path, prior_process=True)
    elif case == "unbound":
        _cache(tmp_path, client_session_id="")
    else:
        _cache(tmp_path)
    if case == "stale":
        marker = _marker(asked_at=time.time() - continuity_probe.MAX_MARKER_AGE_S - 1)
    elif case == "bad_marker":
        marker = _marker(compaction_agent_uuid="not-a-uuid")
    else:
        marker = _marker()
    if case == "foreign_host":
        host = "gemini"
    payload = continuity_probe.build_payload(
        _stop("continuity-probe: same"), host=host, cwd=str(tmp_path)
    )
    assert payload is None
    if case != "foreign_host":
        assert not marker.exists()


def _run_post_stop(tmp_path, *, cwd, host, stop_payload, extra_env=None):
    RecordingHandler.calls = []
    RecordingHandler.paths = []
    srv = _ReusableTCPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "UNITARES_SERVER_URL": f"http://127.0.0.1:{srv.server_address[1]}",
            "UNITARES_CHECKIN_LOG": str(tmp_path / "checkins.log"),
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PWD": str(cwd),
            **(extra_env or {}),
        }
        subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop"), "--host", host],
            env=env, cwd=str(cwd), input=stop_payload, text=True,
            timeout=30, check=False, capture_output=True,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)
    return [c for c in RecordingHandler.calls
            if c.get("observation_kind") == "context_compaction_probe"]


@pytest.mark.parametrize("host", sorted(HOSTS))
def test_question_at_compaction_and_answer_at_turn_end(tmp_path, host):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _cache(workspace)

    stdout, calls = _serve_and_run(
        tmp_path, cwd=workspace, host=host, hook_payload=_event(), extra_env=PROBE_ON,
    )
    ctx = _context(stdout)
    # The question comes ahead of the compaction guidance, which still follows.
    assert continuity_probe.QUESTION in ctx
    assert ctx.index(continuity_probe.QUESTION) < ctx.index(
        f"{HOSTS[host]} reported context compaction"
    )
    assert BINDING not in ctx
    # SessionStart still writes only the compaction record.
    assert [c["observation_kind"] for c in calls] == ["context_compaction"]
    compacted_at = calls[0]["observed_at"]
    assert continuity_probe.marker_path(SLOT).exists()

    answers = _run_post_stop(
        tmp_path, cwd=workspace, host=host, extra_env=PROBE_ON,
        stop_payload=_stop("Picked up where the summary left off.\ncontinuity-probe: same"),
    )
    assert len(answers) == 1, answers
    answer = answers[0]
    assert answer["probe_answer"] == "same_process"
    assert answer["agent_uuid"] == UUID
    assert answer["client_session_id"] == BINDING
    assert answer["compaction_agent_uuid"] == UUID
    assert answer["compaction_observed_at"] == compacted_at
    assert answer["host_family"] == host
    assert not continuity_probe.marker_path(SLOT).exists()


@pytest.mark.parametrize("host", sorted(HOSTS))
def test_without_the_opt_in_nothing_is_asked(tmp_path, host):
    _cache(tmp_path)
    stdout, calls = _serve_and_run(tmp_path, host=host, hook_payload=_event())
    assert "continuity-probe" not in _context(stdout)
    assert [c["observation_kind"] for c in calls] == ["context_compaction"]
    assert not continuity_probe.marker_path(SLOT).exists()


def test_no_question_when_the_compaction_was_not_recorded(tmp_path, monkeypatch):
    # Nothing answers the compaction record, so there is nothing to pair an
    # answer with; the question is not asked.
    _cache(tmp_path)
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path),
        "UNITARES_CONTINUITY_PROBE": "on",
    }
    result = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "compaction_report.py"),
         "--host", "claude", "--server-url", "http://127.0.0.1:9", "--timeout", "0.2",
         json.dumps(_event())],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=10,
    )
    assert result.stdout == ""
    assert not continuity_probe.marker_path(SLOT).exists()


def test_a_marker_left_while_the_probe_is_off_is_consumed_unanswered(tmp_path):
    _cache(tmp_path)
    marker = _marker()
    answers = _run_post_stop(
        tmp_path, cwd=tmp_path, host="claude",
        stop_payload=_stop("continuity-probe: same"),
    )
    assert answers == []
    assert not marker.exists()
