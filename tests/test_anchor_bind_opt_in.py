"""The Stop hook and an orchestrated thread anchor: the scoped opt-in
(UNITARES_AUTO_ONBOARD=anchored) and what is reported when nothing binds."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from _session_lookup import _slot_filename  # noqa: E402
from anchor_bind_report import classify  # noqa: E402

from tests.test_session_start_checkin import RecordingHandler, _ReusableTCPServer  # noqa: E402

ANCHOR = "agent:/thread-4242"
OWNER = "424242"
SLOT = "anchor-slot-0001"


class AnchorHandler(RecordingHandler):
    calls: list[dict] = []
    onboard_result: dict = {}

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            data = {}
        AnchorHandler.calls.append(data)
        if data.get("name") == "onboard":
            response = {"result": AnchorHandler.onboard_result}
        else:
            response = {"result": {"success": True}}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


def _identity(**extra):
    return {
        "success": True,
        "uuid": "11111111-2222-4333-8444-555555555555",
        "agent_id": "Claude_20261121",
        "client_session_id": "agent-11111111-222",
        "session_resolution_source": "explicit_client_session_id",
        "display_name": "claude-thread-4242",
        "onboard_origin": "orchestrated_resume",
        **extra,
    }


def _stop(tmp_path, *, env, onboard_result=None, host_pid=OWNER, args=(), payload=None):
    """Run one Stop for a turn in which the agent bound nothing."""
    AnchorHandler.calls = []
    AnchorHandler.onboard_result = onboard_result if onboard_result is not None else _identity()
    srv = _ReusableTCPServer(("127.0.0.1", 0), AnchorHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    log = tmp_path / "checkins.log"
    try:
        result = subprocess.run(
            [str(PLUGIN_ROOT / "hooks" / "post-stop"), *args],
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "HOME": str(tmp_path),
                "PWD": str(tmp_path),
                "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
                "UNITARES_SERVER_URL": f"http://127.0.0.1:{srv.server_address[1]}",
                "UNITARES_CHECKIN_LOG": str(log),
                "UNITARES_HOOK_HOST_PID": host_pid,
                **env,
            },
            cwd=str(tmp_path),
            input=json.dumps(payload or {
                "hook_event_name": "Stop",
                "session_id": SLOT,
                "stop_hook_active": False,
                "last_assistant_message": "Did the work.",
            }),
            text=True, capture_output=True, timeout=90, check=False,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=2)
    onboards = [c for c in AnchorHandler.calls if c.get("name") == "onboard"]
    lines = log.read_text().splitlines() if log.exists() else []
    anchor_lines = [line for line in lines if "event=anchor_bind" in line]
    return result, onboards, anchor_lines


OWNED = {
    "UNITARES_CLIENT_SESSION_ID": ANCHOR,
    "UNITARES_ORCHESTRATED": "1",
    "UNITARES_ORCHESTRATED_OWNER_PID": OWNER,
}


def test_anchored_mode_binds_the_owner_of_the_anchor(tmp_path):
    result, onboards, anchor_lines = _stop(
        tmp_path, env={**OWNED, "UNITARES_AUTO_ONBOARD": "anchored"},
        onboard_result=_identity(is_new=False),
    )
    assert result.returncode == 0
    assert len(onboards) == 1
    arguments = onboards[0]["arguments"]
    assert arguments["client_session_id"] == ANCHOR
    assert arguments["orchestrated"] is True
    assert arguments["onboard_origin"] == "orchestrated_resume"
    assert "force_new" not in arguments or arguments["force_new"] is False
    assert len(anchor_lines) == 1 and "status=anchor_resumed" in anchor_lines[0]
    assert "uuid=11111111" in anchor_lines[0]
    assert result.stderr.strip() == ""
    cached = json.loads((tmp_path / ".unitares" / _slot_filename(SLOT)).read_text())
    assert cached["uuid"] == "11111111-2222-4333-8444-555555555555"


@pytest.mark.parametrize("env", [
    # No anchor at all: an ordinary session that inherited the variable.
    {},
    # A CLI the anchored agent started. run-hook.cmd strips the anchor for it
    # before any hook runs; this is the environment it is left with.
    {"UNITARES_ANCHOR_IGNORED": "1"},
    # The same child reaching the hook without that launcher: the owner is
    # some other process.
    {**OWNED, "UNITARES_ORCHESTRATED_OWNER_PID": "1"},
    # An anchor of nothing but whitespace, which the helper would strip and
    # then mint an ordinary identity for.
    {**OWNED, "UNITARES_CLIENT_SESSION_ID": "   "},
    {**OWNED, "UNITARES_CLIENT_SESSION_ID": "\u00a0\u00a0", "LC_ALL": "C"},
    {**OWNED, "UNITARES_CLIENT_SESSION_ID": ":/-_", "LC_ALL": "en_US.UTF-8"},
    # An anchor without the orchestrated marker.
    {"UNITARES_CLIENT_SESSION_ID": ANCHOR, "UNITARES_ORCHESTRATED_OWNER_PID": OWNER},
])
def test_anchored_mode_is_off_for_every_other_process(tmp_path, env):
    result, onboards, anchor_lines = _stop(
        tmp_path, env={**env, "UNITARES_AUTO_ONBOARD": "anchored"},
    )
    assert result.returncode == 0
    assert onboards == [], "anchored mode minted an identity for a process that owns no anchor"
    assert anchor_lines == []
    assert not (tmp_path / ".unitares" / _slot_filename(SLOT)).exists()


@pytest.mark.parametrize("value", [" anchored ", "ANCHORED", "anchored\n", "\tAnchored  "])
def test_anchored_is_recognised_however_it_is_spelled(tmp_path, value):
    """An unrecognised word counts as on, so a stray space must not turn the
    scoped opt-in into the unscoped one."""
    _, onboards, _ = _stop(tmp_path, env={"UNITARES_AUTO_ONBOARD": value})
    assert onboards == []


def test_on_still_binds_a_process_that_owns_no_anchor(tmp_path):
    """The contrast that makes `anchored` worth having: `on` reaches everyone."""
    _, onboards, _ = _stop(tmp_path, env={"UNITARES_AUTO_ONBOARD": "on"})
    assert len(onboards) == 1
    assert onboards[0]["arguments"]["onboard_origin"] == "harness_backstop"


@pytest.mark.parametrize("env,why", [
    ({"UNITARES_AUTO_ONBOARD": "off"}, "auto-onboarding is off"),
    ({"UNITARES_AUTO_ONBOARD": "anchored", "UNITARES_DISABLE_AUTO_ONBOARD": "1"}, "auto-onboarding is off"),
    ({"UNITARES_AUTO_ONBOARD": "anchored", "UNITARES_CHECKINS": "off"}, "check-ins are off"),
])
def test_an_owned_anchor_nothing_bound_is_reported(tmp_path, env, why):
    result, onboards, anchor_lines = _stop(tmp_path, env={**OWNED, **env})
    assert result.returncode == 0
    assert onboards == []
    assert len(anchor_lines) == 1 and "status=anchor_unbound" in anchor_lines[0]
    assert why in anchor_lines[0]
    assert "no identity for its thread anchor" in result.stderr
    assert ANCHOR not in result.stderr
    assert "--bind" in result.stderr


def test_a_bind_that_fails_is_reported_and_not_called_unbound(tmp_path):
    result, onboards, anchor_lines = _stop(
        tmp_path, env={**OWNED, "UNITARES_AUTO_ONBOARD": "anchored"},
        onboard_result={"success": False, "error": "lineage_declaration_required"},
    )
    assert result.returncode == 0
    assert len(onboards) == 1
    assert len(anchor_lines) == 1 and "status=anchor_bind_fail" in anchor_lines[0]
    assert "could not bind this turn's thread anchor" in result.stderr
    assert ANCHOR not in result.stderr


def test_a_bind_that_created_a_new_identity_says_so(tmp_path):
    _, _, anchor_lines = _stop(
        tmp_path, env={**OWNED, "UNITARES_AUTO_ONBOARD": "anchored"},
        onboard_result=_identity(is_new=True),
    )
    assert len(anchor_lines) == 1 and "status=anchor_minted" in anchor_lines[0]


def test_a_session_without_an_anchor_is_never_reported(tmp_path):
    for env in ({}, {"UNITARES_AUTO_ONBOARD": "on"}):
        result, _, anchor_lines = _stop(tmp_path, env=env)
        assert anchor_lines == []
        assert "anchor" not in result.stderr


@pytest.mark.parametrize("raw,status", [
    ('{"status": "ok", "uuid": "u", "is_new": false}', "anchor_resumed"),
    ('{"status": "ok", "uuid": "u", "is_new": true}', "anchor_minted"),
    ('{"status": "ok", "uuid": "u"}', "anchor_bound"),
])
def test_classify_a_bind_the_slot_now_holds(raw, status):
    assert classify(raw, bound_csid="agent-u")[0] == status


@pytest.mark.parametrize("raw", [
    '{"status": "ok", "uuid": ""}',
    '{"status": "onboard_failed", "error": "nope"}',
    "",
    "not json",
    "[]",
    # Bound on the server, but the slot still reads as unbound.
    '{"status": "ok", "uuid": "u", "is_new": false}',
    '{"status": "ok", "uuid": "u", "is_new": true}',
])
def test_classify_a_turn_left_without_an_identity(raw):
    assert classify(raw, bound_csid="")[0] == "anchor_bind_fail"


def _write_binding(tmp_path):
    """The slot as another hook would leave it after binding it."""
    cache = tmp_path / ".unitares"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / _slot_filename(SLOT)
    path.write_text(json.dumps({
        "uuid": "99999999-0000-4000-8000-000000000000",
        "client_session_id": "agent-99999999-000",
        "schema_version": 2,
        "slot": SLOT,
    }))
    path.chmod(0o600)


def test_a_turn_the_agent_bound_itself_is_not_reported(tmp_path):
    _write_binding(tmp_path)
    for mode in ("off", "anchored"):
        result, onboards, anchor_lines = _stop(
            tmp_path, env={**OWNED, "UNITARES_AUTO_ONBOARD": mode},
        )
        assert onboards == []
        assert anchor_lines == []
        assert "anchor" not in result.stderr


@pytest.mark.parametrize("raw", [
    '{"status": "onboard_superseded", "error": "newer local state preserved"}',
    '{"status": "onboard_failed", "error": "timeout"}',
    "",
])
def test_a_bind_that_lost_to_another_binding_is_not_called_a_failure(raw):
    assert classify(raw, bound_csid="agent-99999999-000") == ("anchor_bound", "", "")
    assert classify(raw, bound_csid="")[0] == "anchor_bind_fail"
    assert classify(raw, bound_csid="   ")[0] == "anchor_bind_fail"


def test_ok_without_an_identity_does_not_report_ok_as_the_error():
    status, _, error = classify('{"status": "ok", "uuid": ""}')
    assert status == "anchor_bind_fail" and error != "ok" and "identity" in error


def _report(*args, stdin="", log):
    return subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "anchor_bind_report.py"), *args],
        input=stdin, text=True, capture_output=True, timeout=20,
        env={"PATH": "/usr/bin:/bin", "UNITARES_CHECKIN_LOG": str(log)},
    )


def test_report_is_silent_on_stdout_and_never_fails(tmp_path):
    log = tmp_path / "checkins.log"
    cases = [
        (["--slot=s", "--unbound-reason=auto-onboarding is off"], ""),
        (["--slot=s"], "garbage"),
        (["--slot=-starts-with-a-dash", "--bound-csid=-c"], '{"status": "ok", "uuid": "u"}'),
        ([], ""),  # missing required argument
    ]
    for args, stdin in cases:
        result = _report(*args, stdin=stdin, log=log)
        assert result.returncode == 0, (args, result.stderr)
        assert result.stdout == ""


def test_a_log_that_cannot_be_written_still_gets_the_warning(tmp_path):
    result = _report(
        "--slot=s", "--unbound-reason=auto-onboarding is off",
        log=tmp_path,  # a directory: the append fails
    )
    assert result.returncode == 0
    assert "no identity for its thread anchor" in result.stderr


def test_a_failed_pre_bind_at_session_start_is_left_to_the_stop(tmp_path):
    log = tmp_path / "checkins.log"
    failed = '{"status": "onboard_failed", "error": "timeout"}'
    quiet = _report("--slot=s", "--successes-only", stdin=failed, log=log)
    assert quiet.stderr == "" and not log.exists()
    ok = _report("--slot=s", "--successes-only", "--bound-csid=agent-u", stdin='{"status": "ok", "uuid": "u", "is_new": false}', log=log)
    assert ok.stderr == "" and "status=anchor_resumed" in log.read_text()
    loud = _report("--slot=s", stdin=failed, log=log)
    assert "could not bind" in loud.stderr and "status=anchor_bind_fail" in log.read_text()


def test_audit_does_not_count_an_anchor_report_beside_its_floor_line(tmp_path):
    from audit_identity_contract import audit_checkin_log  # noqa: E402

    log = tmp_path / "checkins.log"
    _report("--slot=s", "--unbound-reason=auto-onboarding is off", log=log)
    codes = [finding.code for finding in audit_checkin_log(log)]
    assert codes == []


@pytest.mark.parametrize("value", [" on ", "o f f", "yes please"])
def test_other_values_are_read_as_they_were_before(tmp_path, value):
    """Only `anchored` is new. A word this hook does not know still counts as on."""
    _, onboards, _ = _stop(tmp_path, env={"UNITARES_AUTO_ONBOARD": value})
    assert len(onboards) == 1


def test_padded_anchored_still_binds_the_owner(tmp_path):
    _, onboards, anchor_lines = _stop(
        tmp_path, env={**OWNED, "UNITARES_AUTO_ONBOARD": " Anchored "},
        onboard_result=_identity(is_new=False),
    )
    assert len(onboards) == 1 and "status=anchor_resumed" in anchor_lines[0]


def test_an_idle_binding_the_stop_withdraws_is_reported_unbound(tmp_path):
    """A slot whose cached binding the activity check rejects ends the turn
    identity-free; with `on` no bind is attempted because the slot looked bound."""
    _write_binding(tmp_path)
    cache = tmp_path / ".unitares" / _slot_filename(SLOT)
    data = json.loads(cache.read_text())
    data["prior_process"] = True
    cache.write_text(json.dumps(data))
    result, onboards, anchor_lines = _stop(tmp_path, env={**OWNED, "UNITARES_AUTO_ONBOARD": "off"})
    assert result.returncode == 0
    assert len(anchor_lines) == 1 and "status=anchor_unbound" in anchor_lines[0]


def test_a_withdrawn_binding_is_not_answered_with_advice_to_turn_binding_on(tmp_path):
    result = _report(
        "--slot=s", "--withdrawn",
        "--unbound-reason=the session's cached binding was not usable for this turn",
        log=tmp_path / "checkins.log",
    )
    assert "cached binding was not usable" in result.stderr
    assert "--bind" not in result.stderr and "AUTO_ONBOARD" not in result.stderr
