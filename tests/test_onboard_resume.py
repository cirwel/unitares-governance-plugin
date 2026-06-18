"""Resume-by-anchor onboarding (UNITARES_CLIENT_SESSION_ID).

When a spawner sets a stable per-conversation anchor (the Discord bridge's
canonical case: one `claude -p` process per user turn), every turn must resume
the SAME governance identity instead of minting a fresh uuid each time. This is
continuity, not lineage — the turns are the same agent resumed.

Pins the client-side contract: anchor set => send `client_session_id`, omit
`force_new`, declare no lineage, and do not slot-scope the name. Gated +
additive: no anchor => byte-identical to the legacy fresh-mint flow; an explicit
`force_new` overrides the anchor (a deliberate clean break).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from onboard_helper import run_onboard  # noqa: E402


class _FakeTransport:
    """Record every outbound request so tests can assert on what was sent."""

    def __init__(self, response: dict[str, Any]):
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, payload: dict, timeout: float, token: str | None) -> dict:
        self.calls.append({"url": url, "payload": payload, "token": token})
        return self._response


def _onboard_ok(uuid: str, display_name: str = "claude-thread-x") -> dict:
    return {
        "result": {
            "success": True,
            "uuid": uuid,
            "agent_id": f"Claude_Code_{uuid[:8]}",
            "client_session_id": "agent:/thread-x",
            "session_resolution_source": "explicit_client_session_id",
            "display_name": display_name,
        }
    }


def _sent_args(transport: _FakeTransport) -> dict:
    return transport.calls[0]["payload"]["arguments"]


def test_anchor_resumes_sends_csid_omits_force_new_and_lineage(tmp_path: Path) -> None:
    transport = _FakeTransport(_onboard_ok("1111aaaa-0000-0000-0000-000000000000"))

    result = run_onboard(
        server_url="http://unit-test",
        agent_name="claude-thread-x",
        model_type="claude-code",
        workspace=tmp_path,
        slot="turn-2-session-id",  # changes per turn — must NOT scope the name
        client_session_id="agent:/thread-x",
        post_json=transport,
    )

    assert result["status"] == "ok"
    args = _sent_args(transport)
    assert args["client_session_id"] == "agent:/thread-x"
    # Resume, not mint: no force_new, no lineage.
    assert "force_new" not in args
    assert "parent_agent_id" not in args
    assert "spawn_reason" not in args
    # Stable name — NOT slot-scoped (no '#fingerprint'), so it's one identity.
    assert args["name"] == "claude-thread-x"


def test_anchor_ignores_cached_uuid_no_lineage(tmp_path: Path) -> None:
    """Even with a prior cached uuid (the per-turn lineage source), resume must
    not declare it as a parent — the turns are the same agent, not a chain."""
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    (cache_dir / "session-turn-1.json").write_text('{"uuid": "prior-uuid-from-turn-1"}')

    transport = _FakeTransport(_onboard_ok("2222bbbb-0000-0000-0000-000000000000"))
    run_onboard(
        server_url="http://unit-test",
        agent_name="claude-thread-x",
        model_type="claude-code",
        workspace=tmp_path,
        slot="turn-1",
        client_session_id="agent:/thread-x",
        post_json=transport,
    )

    args = _sent_args(transport)
    assert "parent_agent_id" not in args
    assert args["client_session_id"] == "agent:/thread-x"


def test_explicit_force_new_overrides_anchor(tmp_path: Path) -> None:
    transport = _FakeTransport(_onboard_ok("3333cccc-0000-0000-0000-000000000000"))

    run_onboard(
        server_url="http://unit-test",
        agent_name="claude-thread-x",
        model_type="claude-code",
        workspace=tmp_path,
        slot="s1",
        client_session_id="agent:/thread-x",
        force_new=True,  # deliberate clean break wins
        post_json=transport,
    )

    args = _sent_args(transport)
    assert args["force_new"] is True
    assert "client_session_id" not in args


def test_blank_anchor_falls_back_to_fresh_mint(tmp_path: Path) -> None:
    transport = _FakeTransport(_onboard_ok("4444dddd-0000-0000-0000-000000000000"))

    run_onboard(
        server_url="http://unit-test",
        agent_name="claude-thread-x",
        model_type="claude-code",
        workspace=tmp_path,
        slot="s1",
        client_session_id="   ",  # blank => legacy fresh-mint
        post_json=transport,
    )

    args = _sent_args(transport)
    assert args["force_new"] is True
    assert "client_session_id" not in args


def test_no_anchor_is_byte_identical_to_legacy(tmp_path: Path) -> None:
    """Unset anchor => the exact legacy payload (force_new + slot-scoped name)."""
    transport = _FakeTransport(_onboard_ok("5555eeee-0000-0000-0000-000000000000"))

    run_onboard(
        server_url="http://unit-test",
        agent_name="claude-thread-x",
        model_type="claude-code",
        workspace=tmp_path,
        slot="s1",
        # client_session_id omitted entirely
        post_json=transport,
    )

    args = _sent_args(transport)
    assert args["force_new"] is True
    assert "client_session_id" not in args
    # Name IS slot-scoped in the legacy path.
    assert args["name"].startswith("claude-thread-x#")
