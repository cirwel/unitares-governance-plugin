"""Tests for the auto-checkin threshold decision.

The hook fires process_agent_update when edit_count or elapsed-time crosses
a threshold. These tests pin the exact conditions so a future refactor
cannot silently regress the behavior back to "never fire."
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from auto_checkin_decision import decide  # noqa: E402


FIXED_NOW = 2_000_000_000
SESSION = {
    "client_session_id": "sid-123",
    "continuity_token": "ct-xyz",
}


def _milestone(
    edit_count: int,
    files: list[str] | None = None,
    first_edit_ts: int | None = None,
) -> dict:
    return {
        "edit_count": edit_count,
        "files_touched": files or [],
        "first_edit_ts": first_edit_ts,
    }


def test_below_threshold_does_not_fire() -> None:
    result = decide(
        session=SESSION,
        milestone=_milestone(edit_count=4, files=[f"/f{i}" for i in range(4)]),
        edit_threshold=5,
        secs_threshold=600,
        now=FIXED_NOW,
    )
    assert result["fire"] is False


def test_edit_count_threshold_fires() -> None:
    result = decide(
        session=SESSION,
        milestone=_milestone(edit_count=5, files=[f"/f{i}" for i in range(5)]),
        edit_threshold=5,
        secs_threshold=600,
        now=FIXED_NOW,
    )
    assert result["fire"] is True
    assert result["reason"] == "edits>=5"


def test_time_threshold_fires_with_at_least_one_edit() -> None:
    result = decide(
        session=SESSION,
        milestone=_milestone(
            edit_count=2,
            files=["/f1", "/f2"],
            first_edit_ts=FIXED_NOW - 900,
        ),
        edit_threshold=5,
        secs_threshold=600,
        now=FIXED_NOW,
    )
    assert result["fire"] is True
    assert result["reason"] == "elapsed>=600s"


def test_time_threshold_does_not_fire_without_edits() -> None:
    # Empty accumulator must never auto-fire, even if clock is far ahead —
    # this guards against a spurious checkin when the user left the session
    # idle overnight with no actual work done.
    result = decide(
        session={**SESSION, "last_checkin_ts": FIXED_NOW - 10_000},
        milestone=_milestone(edit_count=0),
        edit_threshold=5,
        secs_threshold=600,
        now=FIXED_NOW,
    )
    assert result["fire"] is False


def test_last_checkin_ts_dominates_first_edit_ts() -> None:
    # A recent /checkin should suppress auto-fire even if first_edit_ts is
    # stale — last_checkin_ts is the authoritative anchor.
    result = decide(
        session={**SESSION, "last_checkin_ts": FIXED_NOW - 60},
        milestone=_milestone(
            edit_count=2,
            files=["/f1", "/f2"],
            first_edit_ts=FIXED_NOW - 10_000,
        ),
        edit_threshold=5,
        secs_threshold=600,
        now=FIXED_NOW,
    )
    assert result["fire"] is False


def test_complexity_scales_with_file_diversity() -> None:
    one_file = decide(
        session=SESSION,
        milestone=_milestone(edit_count=5, files=["/f1"]),
        now=FIXED_NOW,
    )
    six_files = decide(
        session=SESSION,
        milestone=_milestone(edit_count=5, files=[f"/f{i}" for i in range(6)]),
        now=FIXED_NOW,
    )
    assert six_files["complexity"] > one_file["complexity"]
    # Cap keeps the hook from ever claiming maximum complexity.
    assert six_files["complexity"] <= 0.85


def test_complexity_capped_on_many_files() -> None:
    result = decide(
        session=SESSION,
        milestone=_milestone(edit_count=20, files=[f"/f{i}" for i in range(20)]),
        now=FIXED_NOW,
    )
    assert result["complexity"] == 0.85


def test_response_text_includes_concrete_file_names() -> None:
    result = decide(
        session=SESSION,
        milestone=_milestone(edit_count=5, files=["/a.py", "/b.py", "/c.py"]),
        now=FIXED_NOW,
    )
    assert "/a.py" in result["response_text"]
    assert "/b.py" in result["response_text"]
    assert "[hook]" in result["response_text"]


def test_response_text_summarizes_overflow() -> None:
    files = [f"/f{i}" for i in range(10)]
    result = decide(
        session=SESSION,
        milestone=_milestone(edit_count=10, files=files),
        now=FIXED_NOW,
    )
    # Should sample a few names and summarize the rest — not dump all 10.
    assert "+5 more" in result["response_text"]


def test_uses_client_session_id_and_ignores_continuity_token() -> None:
    with_token = decide(
        session={"continuity_token": "ct", "client_session_id": "sid"},
        milestone=_milestone(edit_count=5, files=["/f"]),
        now=FIXED_NOW,
    )
    assert "continuity_token" not in with_token
    assert with_token["client_session_id"] == "sid"

    sid_only = decide(
        session={"client_session_id": "sid"},
        milestone=_milestone(edit_count=5, files=["/f"]),
        now=FIXED_NOW,
    )
    assert "continuity_token" not in sid_only
    assert sid_only["client_session_id"] == "sid"


def test_edit_threshold_floor_of_one() -> None:
    # Pathological config (threshold=0) must not cause infinite-fire loops —
    # the helper coerces it to the documented floor of 1.
    result = decide(
        session=SESSION,
        milestone=_milestone(edit_count=1, files=["/f"]),
        edit_threshold=0,
        now=FIXED_NOW,
    )
    assert result["fire"] is True
