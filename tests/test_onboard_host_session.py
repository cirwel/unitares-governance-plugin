"""Fresh mints declare which vendor session they live in (portal hub H0).

The slot is the host's own session id. With --host-harness, a fresh mint sends
`host_session = {harness, session_id: slot, workspace}` so the server can
record which vendor session to resume and where. An orchestrated resume never
sends it, and nothing is sent without a harness or with a slot that does not
look like a session id.
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
        tmp_path, slot=SESSION, host_harness="claude-code", host_session_dir=project,
    )
    assert transport.args["force_new"] is True
    assert transport.args["host_session"] == {
        "harness": "claude-code", "session_id": SESSION, "workspace": str(project),
    }


def test_workspace_is_optional(tmp_path: Path) -> None:
    transport = _run(tmp_path, slot=SESSION, host_harness="codex")
    assert transport.args["host_session"] == {"harness": "codex", "session_id": SESSION}


def test_nothing_declared_without_harness_or_slot(tmp_path: Path) -> None:
    assert "host_session" not in _run(tmp_path, slot=SESSION).args
    assert "host_session" not in _run(tmp_path / "b", host_harness="codex").args


def test_odd_slot_is_not_declared(tmp_path: Path) -> None:
    transport = _run(tmp_path, slot="a slot/with spaces", host_harness="claude-code")
    assert "host_session" not in transport.args


def test_orchestrated_resume_never_declares(tmp_path: Path) -> None:
    transport = _run(
        tmp_path,
        slot=SESSION,
        client_session_id="agent:/thread-x",
        orchestrated=True,
        host_harness="claude-code",
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
        "--host-session-dir", str(tmp_path),
    ])
    capsys.readouterr()
    assert transport.args["host_session"] == {
        "harness": "claude-code", "session_id": SESSION,
        "workspace": str(tmp_path.resolve()),
    }
    json.dumps(transport.args)
