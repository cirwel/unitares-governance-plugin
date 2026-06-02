"""Unit tests for scripts/checkin.py — build/redact/post/log helper."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import checkin  # noqa: E402


def test_kill_switch_skips_post(monkeypatch, tmp_path):
    """UNITARES_CHECKINS=off short-circuits before any network call."""
    log_path = tmp_path / "checkins.log"
    monkeypatch.setenv("UNITARES_CHECKINS", "off")
    monkeypatch.setenv("UNITARES_CHECKIN_LOG", str(log_path))

    with patch("checkin._post_to_governance") as mock_post:
        result = checkin.submit_checkin(
            event="turn_stop",
            response_text="test",
            complexity=0.3,
            confidence=0.7,
            client_session_id="agent-test1234",
            continuity_token="v1.faketoken",
            slot="test-slot",
        )

    assert result == "skip_kill_switch"
    mock_post.assert_not_called()
    assert log_path.exists()
    line = log_path.read_text().strip()
    assert "status=skip_kill_switch" in line
    assert "event=turn_stop" in line


def test_kill_switch_default_on(monkeypatch, tmp_path):
    """Unset UNITARES_CHECKINS defaults to on."""
    log_path = tmp_path / "checkins.log"
    monkeypatch.delenv("UNITARES_CHECKINS", raising=False)
    monkeypatch.setenv("UNITARES_CHECKIN_LOG", str(log_path))

    with patch("checkin._post_to_governance", return_value=(True, 42, None)) as mock_post:
        result = checkin.submit_checkin(
            event="session_start",
            response_text="init",
            complexity=0.1,
            confidence=0.9,
            client_session_id="agent-test1234",
            continuity_token="v1.faketoken",
            slot="test-slot",
        )

    assert result == "sent"
    mock_post.assert_called_once()


def test_submit_checkin_never_raises_on_garbage_inputs(monkeypatch, tmp_path):
    """The 'never raises' contract holds for garbage inputs.

    Hook authors may feed env-var strings, None, or bytes through these
    parameters. submit_checkin must return a status string, never raise.
    """
    log_path = tmp_path / "checkins.log"
    monkeypatch.setenv("UNITARES_CHECKIN_LOG", str(log_path))

    # None for numeric fields — float(None) raises TypeError
    with patch("checkin._post_to_governance", return_value=(True, 1, None)):
        result = checkin.submit_checkin(
            event="turn_stop",
            response_text="x",
            complexity=None,  # type: ignore[arg-type]
            confidence=None,  # type: ignore[arg-type]
            client_session_id="agent-x",
            continuity_token="v1.t",
            slot="s",
        )
    assert result == "error"

    # Non-string response_text — redact_secrets' re.sub raises TypeError on bytes
    with patch("checkin._post_to_governance", return_value=(True, 1, None)):
        result = checkin.submit_checkin(
            event="turn_stop",
            response_text=b"x",  # type: ignore[arg-type]
            complexity=0.3,
            confidence=0.7,
            client_session_id="agent-x",
            continuity_token="v1.t",
            slot="s",
        )
    assert result == "error"

    # Log should have two 'status=error' lines
    lines = log_path.read_text().strip().splitlines()
    assert sum(1 for l in lines if "status=error" in l) == 2


def test_log_format_resilient_to_pathological_error_text(monkeypatch, tmp_path):
    """Newlines, quotes, pipes, backslashes in error text do not corrupt the log."""
    log_path = tmp_path / "checkins.log"
    monkeypatch.setenv("UNITARES_CHECKIN_LOG", str(log_path))

    pathological = 'line1\nline2 | "quoted" \\escaped\\ \rcr'
    with patch("checkin._post_to_governance", return_value=(False, 99, pathological)):
        checkin.submit_checkin(
            event="turn_stop",
            response_text="x",
            complexity=0.3,
            confidence=0.7,
            client_session_id="agent-x",
            continuity_token="v1.t",
            slot="s",
        )

    content = log_path.read_text()
    # Exactly one line — the error must not have split the record.
    assert content.count("\n") == 1
    # No raw double-quote mid-line (the escaped form is `\"`)
    raw_line = content.strip()
    # Count of unescaped double-quotes should be exactly 2 (opening + closing of err=".."))
    # Backslash before each internal quote
    assert raw_line.count('\\"') == 2  # both original quotes survived escaped


def test_payload_shape(monkeypatch, tmp_path):
    """Built payload matches the documented contract."""
    monkeypatch.setenv("UNITARES_CHECKIN_LOG", str(tmp_path / "cl.log"))
    captured: dict = {}

    def fake_post(url, payload, timeout=5.0):
        captured["url"] = url
        captured["payload"] = payload
        return True, 33, None

    with patch("checkin._post_to_governance", side_effect=fake_post):
        checkin.submit_checkin(
            event="turn_stop",
            response_text="did stuff",
            complexity=0.4,
            confidence=0.7,
            client_session_id="agent-abc1234567",
            continuity_token="v1.tok",
            slot="slot-1",
            uuid="86ae619f-87e0-4040-8f29-eacece0c7904",
        )

    args = captured["payload"]["arguments"]
    assert captured["payload"]["name"] == "process_agent_update"
    assert args["response_text"] == "did stuff"
    assert args["complexity"] == 0.4
    assert args["confidence"] == 0.7
    assert args["client_session_id"] == "agent-abc1234567"
    assert "continuity_token" not in args
    assert args["metadata"]["source"] == "plugin_hook"
    assert args["metadata"]["event"] == "turn_stop"
    assert args["metadata"]["plugin_version"] == checkin._plugin_version()


def test_plugin_version_matches_package_metadata():
    """Every version location must stay in lockstep: the two package
    manifests, the marketplace listing /plugin reads to offer updates, the
    hook telemetry resolver, and its constant fallback. A drift in any one
    (e.g. bumping a manifest but not marketplace.json) ships a plugin whose
    updater advertises the wrong version."""
    plugin_root = Path(__file__).parent.parent
    versions = []
    for rel in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        data = json.loads((plugin_root / rel).read_text(encoding="utf-8"))
        versions.append(data["version"])

    # marketplace.json carries the version per-plugin under plugins[]; it is
    # the file /plugin reads to decide what update to offer, so it must match.
    market = json.loads(
        (plugin_root / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
    )
    market_entry = next(
        p for p in market["plugins"] if p["name"] == "unitares-governance"
    )
    versions.append(market_entry["version"])

    # DEFAULT_PLUGIN_VERSION is the fallback _plugin_version() returns when the
    # manifests are unreadable; guard it so it can't silently rot to a stale tag.
    versions.append(checkin.DEFAULT_PLUGIN_VERSION)

    assert len(set(versions)) == 1, f"version drift across locations: {versions}"
    assert checkin._plugin_version() == versions[0]


def test_payload_redacts_secrets(monkeypatch, tmp_path):
    """Secret-looking strings in response_text are redacted before POST."""
    monkeypatch.setenv("UNITARES_CHECKIN_LOG", str(tmp_path / "cl.log"))
    captured: dict = {}

    def fake_post(url, payload, timeout=5.0):
        captured["payload"] = payload
        return True, 10, None

    with patch("checkin._post_to_governance", side_effect=fake_post):
        checkin.submit_checkin(
            event="turn_stop",
            response_text="Leaked ANTHROPIC_API_KEY=sk-ant-api03-abc123DEF456ghi789JKL012",
            complexity=0.3,
            confidence=0.7,
            client_session_id="agent-x",
            continuity_token="v1.t",
            slot="s",
        )

    assert "sk-ant-api03" not in captured["payload"]["arguments"]["response_text"]
    assert "[REDACTED:anthropic_key]" in captured["payload"]["arguments"]["response_text"]


def test_response_text_truncated(monkeypatch, tmp_path):
    """Response text longer than 512 chars is truncated."""
    monkeypatch.setenv("UNITARES_CHECKIN_LOG", str(tmp_path / "cl.log"))
    captured: dict = {}

    def fake_post(url, payload, timeout=5.0):
        captured["payload"] = payload
        return True, 10, None

    with patch("checkin._post_to_governance", side_effect=fake_post):
        checkin.submit_checkin(
            event="turn_stop",
            response_text="x" * 2000,
            complexity=0.3,
            confidence=0.7,
            client_session_id="agent-x",
            continuity_token="v1.t",
            slot="s",
        )

    assert len(captured["payload"]["arguments"]["response_text"]) == 512


def test_post_failure_logged_as_fail(monkeypatch, tmp_path):
    """POST timeouts / errors are logged and returned as 'fail'."""
    log_path = tmp_path / "cl.log"
    monkeypatch.setenv("UNITARES_CHECKIN_LOG", str(log_path))

    with patch("checkin._post_to_governance", return_value=(False, 5000, "timeout")):
        result = checkin.submit_checkin(
            event="turn_stop",
            response_text="x",
            complexity=0.3,
            confidence=0.7,
            client_session_id="agent-x",
            continuity_token="v1.t",
            slot="s",
        )

    assert result == "fail"
    line = log_path.read_text().strip()
    assert "status=fail" in line
    assert 'err="timeout"' in line
