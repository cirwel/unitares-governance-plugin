"""The compaction reporter reports only this slot's own usable binding."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import compaction_report  # noqa: E402

UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
BINDING = "agent-compaction-report-binding"
SLOT = "compaction-report-slot"


def _cache(root, **overrides):
    path = root / ".unitares" / f"session-{SLOT}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "uuid": UUID, "client_session_id": BINDING,
        "continuity_token": "never-sent", "slot": SLOT, "schema_version": 2,
        **overrides,
    }))
    path.chmod(0o600)
    return path


def _event(**overrides):
    return json.dumps({"session_id": SLOT, "source": "compact", **overrides})


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_usable_binding_builds_an_audit_only_observation(home, host):
    _cache(home)
    payload = compaction_report.build_payload(_event(), host=host, cwd=str(home))
    assert payload["agent_uuid"] == UUID
    assert payload["client_session_id"] == BINDING
    assert payload["observation_kind"] == "context_compaction"
    assert payload["host_family"] == host
    assert payload["slot_hash"] == hashlib.sha256(SLOT.encode()).hexdigest()[:32]
    assert payload["execution_mode"] == "unknown"
    assert payload["execution_mode_source"] == "unspecified"
    assert payload["observed_at"].endswith("Z")
    assert "never-sent" not in json.dumps(payload)


@pytest.mark.parametrize(
    "event, cache, host",
    [
        (_event(source="startup"), {}, "claude"),
        (_event(source="resume"), {}, "claude"),
        (_event(), {"prior_process": True}, "claude"),
        (_event(), {"client_session_id": ""}, "codex"),
        (_event(), {"uuid": "not-a-uuid"}, "codex"),
        (_event(), {}, "unknown-host"),
        ("{not json", {}, "claude"),
        (json.dumps({"source": "compact"}), {}, "claude"),
    ],
    ids=["startup", "resume", "inherited", "no-binding", "bad-uuid", "host", "bad-json", "slotless"],
)
def test_nothing_is_reported_without_this_slots_own_binding(home, event, cache, host):
    _cache(home, **cache)
    assert compaction_report.build_payload(event, host=host, cwd=str(home)) is None


def test_missing_cache_reports_nothing(home):
    assert compaction_report.build_payload(_event(), host="claude", cwd=str(home)) is None


def test_kill_switch_posts_nothing(home, monkeypatch):
    _cache(home)
    monkeypatch.chdir(home)
    posted = []
    monkeypatch.setattr(compaction_report, "post", lambda *a, **k: posted.append(a))
    monkeypatch.setenv("UNITARES_CHECKINS", "off")
    assert compaction_report.main(["--host", "claude", "--server-url", "http://x", _event()]) == 0
    assert posted == []


def test_main_posts_this_slots_observation_once(home, monkeypatch):
    _cache(home)
    monkeypatch.chdir(home)
    posted = []
    monkeypatch.setattr(
        compaction_report, "post", lambda url, payload, **k: posted.append((url, payload))
    )
    assert compaction_report.main(["--host", "codex", "--server-url", "http://srv", _event()]) == 0
    assert [(url, payload["observation_kind"]) for url, payload in posted] == [
        ("http://srv", "context_compaction")
    ]


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"success": false}'])
def test_post_is_false_and_silent_for_odd_responses(monkeypatch, body):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size=-1):
            return body if size < 0 else body[:size]

    monkeypatch.setattr(compaction_report, "authorization_safe_urlopen", lambda *a, **k: _Response())
    assert compaction_report.post("http://srv", {"a": 1}, timeout=0.1) is False


def test_unreachable_server_is_silent(home, monkeypatch, capsys):
    _cache(home)
    monkeypatch.chdir(home)
    assert compaction_report.main(
        ["--host", "claude", "--server-url", "http://127.0.0.1:1", "--timeout", "0.5", _event()]
    ) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
