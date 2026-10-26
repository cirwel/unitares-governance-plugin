"""Fresh mints declare which vendor session they live in (portal hub H0).

With --host-harness and --host-session-id (the host's own, unsanitized id, not
the cache slot), a fresh mint sends `host_session = {harness, session_id,
workspace}` so the server can record which vendor session to resume and where.
An orchestrated resume never sends it, and nothing is sent without a harness or
with an id that does not look like a session id.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import onboard_helper  # noqa: E402
from onboard_helper import run_onboard  # noqa: E402

SESSION = "36fb66eb-7f82-41e8-a965-b91dbf03039f"


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, payload: dict, timeout: float, token: str | None) -> dict:
        self.calls.append(payload)
        return {"result": {
            "success": True,
            "uuid": "1111aaaa-0000-0000-0000-000000000000",
            "agent_id": "Claude_Code_1111aaaa",
            "client_session_id": "agent-1111aaaa",
            "display_name": "claude-x",
        }}

    @property
    def args(self) -> dict:
        return self.calls[0]["arguments"]


def _run(tmp_path: Path, **kwargs: Any) -> _FakeTransport:
    transport = _FakeTransport()
    run_onboard(
        server_url="http://unit-test",
        agent_name="claude-x",
        model_type="claude-code",
        workspace=tmp_path,
        post_json=transport,
        **kwargs,
    )
    return transport


def test_fresh_mint_declares_host_session(tmp_path: Path) -> None:
    project = tmp_path / "project"
    transport = _run(
        tmp_path, slot="cache-slot", host_harness="claude-code",
        host_session_id=SESSION, host_session_dir=project,
    )
    assert transport.args["force_new"] is True
    assert transport.args["host_session"] == {
        "harness": "claude-code", "session_id": SESSION, "workspace": str(project),
    }


def test_workspace_is_optional(tmp_path: Path) -> None:
    transport = _run(tmp_path, slot=SESSION, host_harness="codex", host_session_id=SESSION)
    assert transport.args["host_session"] == {"harness": "codex", "session_id": SESSION}


def test_the_cache_slot_is_never_declared(tmp_path: Path) -> None:
    """Without the host's own id nothing is declared, even with a slot."""
    assert "host_session" not in _run(tmp_path, slot=SESSION, host_harness="codex").args
    assert "host_session" not in _run(tmp_path / "b", host_session_id=SESSION).args


def test_odd_session_id_is_not_declared(tmp_path: Path) -> None:
    transport = _run(
        tmp_path, slot="s", host_harness="claude-code", host_session_id="a b/c",
    )
    assert "host_session" not in transport.args


def test_host_session_id_from_payload() -> None:
    from _slot_from_stdin import host_session_id_from_payload as raw

    assert raw(json.dumps({"session_id": SESSION})) == SESSION
    assert raw(json.dumps({"conversationId": "conv-1"})) == "conv-1"
    # Hashed-fallback fields are cache keys, not resumable ids.
    assert raw(json.dumps({"thread_id": "thr-1"})) == ""
    assert raw(json.dumps({"transcript_path": "/x/y.jsonl"})) == ""
    # Never rewritten: an id the slot rule would sanitize is dropped instead.
    assert raw(json.dumps({"session_id": "a b"})) == ""
    assert raw(json.dumps({"session_id": "s" * 129})) == ""
    assert raw("not json") == "" and raw("") == ""


def test_unresolvable_directory_never_blocks_onboarding(tmp_path: Path, monkeypatch) -> None:
    assert onboard_helper._optional_dir("~no_such_user_zz/x") is None
    assert onboard_helper._optional_dir("") is None
    transport = _FakeTransport()
    monkeypatch.setattr(
        onboard_helper, "run_onboard",
        lambda **kw: run_onboard(**{**kw, "post_json": transport}),
    )
    onboard_helper.main([
        "--name", "claude-x", "--workspace", str(tmp_path), "--slot", SESSION,
        "--host-harness", "claude-code", "--host-session-id", SESSION,
        "--host-session-dir", "~no_such_user_zz/x",
    ])
    assert transport.args["host_session"] == {"harness": "claude-code", "session_id": SESSION}


def test_orchestrated_resume_never_declares(tmp_path: Path) -> None:
    transport = _run(
        tmp_path,
        slot=SESSION,
        client_session_id="agent:/thread-x",
        orchestrated=True,
        host_harness="claude-code",
        host_session_id=SESSION,
        host_session_dir=tmp_path,
    )
    assert "force_new" not in transport.args
    assert "host_session" not in transport.args


def test_cli_flags_reach_the_request(tmp_path: Path, monkeypatch, capsys) -> None:
    transport = _FakeTransport()
    monkeypatch.setattr(onboard_helper, "_post_json", transport)
    monkeypatch.setattr(
        onboard_helper, "run_onboard",
        lambda **kw: run_onboard(**{**kw, "post_json": transport}),
    )
    onboard_helper.main([
        "--name", "claude-x",
        "--workspace", str(tmp_path),
        "--slot", SESSION,
        "--host-harness", "claude-code",
        "--host-session-id", SESSION,
        "--host-session-dir", str(tmp_path),
    ])
    capsys.readouterr()
    assert transport.args["host_session"] == {
        "harness": "claude-code", "session_id": SESSION,
        "workspace": str(tmp_path.resolve()),
    }
    json.dumps(transport.args)
