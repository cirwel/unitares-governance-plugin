"""Tests for the local identity-contract audit guardrail."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parent.parent
AUDIT = PLUGIN_ROOT / "scripts" / "audit_identity_contract.py"


def _run(workspace: Path, log: Path | None = None, *extra: str) -> subprocess.CompletedProcess[str]:
    args = ["python3", str(AUDIT), "--workspace", str(workspace), "--json", *extra]
    if log is not None:
        args.extend(["--log", str(log)])
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def _write_session(workspace: Path, name: str, payload: dict) -> Path:
    cache_dir = workspace / ".unitares"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _payload(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


def test_clean_slot_cache_passes(tmp_path: Path) -> None:
    _write_session(tmp_path, "session-agent-clean.json", {
        "schema_version": 2,
        "uuid": "00000000-0000-0000-0000-000000000001",
        "client_session_id": "agent-clean",
        "session_resolution_source": "explicit_client_session_id",
    })

    result = _run(tmp_path, tmp_path / "missing.log")

    assert result.returncode == 0
    payload = _payload(result)
    assert payload["errors"] == 0
    assert payload["warnings"] == 0


def test_token_at_rest_is_hard_error(tmp_path: Path) -> None:
    _write_session(tmp_path, "session-agent-bad.json", {
        "schema_version": 2,
        "uuid": "00000000-0000-0000-0000-000000000001",
        "client_session_id": "agent-bad",
        "continuity_token": "v1.real-token",
    })

    result = _run(tmp_path, tmp_path / "missing.log")

    assert result.returncode == 2
    payload = _payload(result)
    assert payload["errors"] == 1
    assert payload["findings"][0]["code"] == "session_cache_token_at_rest"


def test_empty_token_only_stub_is_hard_error(tmp_path: Path) -> None:
    _write_session(tmp_path, "session-agent-stub.json", {
        "schema_version": 2,
        "continuity_token": "",
    })

    result = _run(tmp_path, tmp_path / "missing.log")

    assert result.returncode == 2
    codes = {finding["code"] for finding in _payload(result)["findings"]}
    assert "session_cache_missing_identity" in codes


def test_weak_resolution_and_floor_log_are_warnings(tmp_path: Path) -> None:
    _write_session(tmp_path, "session-agent-weak.json", {
        "schema_version": 2,
        "uuid": "00000000-0000-0000-0000-000000000001",
        "client_session_id": "agent-weak",
        "session_resolution_source": "ip_ua_fingerprint",
    })
    log = tmp_path / "checkins.log"
    log.write_text(
        "2026-06-16T00:00:00Z | slot=s1 | event=substrate_turn_stop | uuid=? | status=floor_sent | latency_ms=5\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, log)

    assert result.returncode == 0
    payload = _payload(result)
    assert payload["errors"] == 0
    assert payload["warnings"] == 2
    assert {f["code"] for f in payload["findings"]} == {
        "weak_session_resolution_source",
        "checkin_fallback_status",
    }


def test_fail_on_warning_exits_one(tmp_path: Path) -> None:
    _write_session(tmp_path, "session.json", {
        "uuid": "00000000-0000-0000-0000-000000000001",
    })

    result = _run(tmp_path, tmp_path / "missing.log", "--fail-on-warning")

    assert result.returncode == 1
    payload = _payload(result)
    assert payload["errors"] == 0
    assert payload["warnings"] == 1
    assert payload["findings"][0]["code"] == "flat_session_cache_present"
