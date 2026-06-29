"""Unit tests for scripts/_redact.py — secret redaction regexes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _redact import redact_secrets, sanitize_model_visible_payload


def test_redacts_anthropic_api_key():
    text = "ran with ANTHROPIC_API_KEY=sk-ant-api03-abc123DEF456ghi789JKL"
    out = redact_secrets(text)
    assert "sk-ant-api03" not in out
    assert "[REDACTED:anthropic_key]" in out


def test_redacts_openai_api_key():
    text = "curl -H 'Authorization: Bearer sk-proj-abc123DEF456GHI789jkl012MNO345pqr678STU901vwx234'"
    out = redact_secrets(text)
    assert "sk-proj-" not in out
    assert "[REDACTED:openai_key]" in out


def test_redacts_github_token():
    text = "export GH_TOKEN=ghp_abc123DEF456ghi789JKL012mno345PQR"
    out = redact_secrets(text)
    assert "ghp_" not in out
    assert "[REDACTED:github_token]" in out


def test_redacts_aws_access_key():
    text = "AKIAIOSFODNN7EXAMPLE is the key"
    out = redact_secrets(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "[REDACTED:aws_key]" in out


def test_preserves_non_secret_text():
    text = "Ran pytest and 257 tests passed"
    assert redact_secrets(text) == text


def test_handles_none_input():
    assert redact_secrets(None) == ""


def test_handles_empty_string():
    assert redact_secrets("") == ""


def test_redacts_unitares_continuity_token_text():
    token = (
        "v1.eyJhaWQiOiJkNDAyZjQ4MC1jOGM1LTRkOWYtODA2Zi01ZGJhOTZlYjZhOTIifQ."
        "rN9Otpp62gEV9mtljSsFLIl1zi3NZ0rj3giFhy0ddBw"
    )
    out = redact_secrets(f"continuity_token={token}")
    assert token not in out
    assert "[REDACTED:unitares_continuity_token]" in out


def test_redacts_unitares_client_session_text():
    out = redact_secrets("Save client_session_id agent-d402f480-c8c")
    assert "agent-d402f480-c8c" not in out
    assert "[REDACTED:unitares_client_session]" in out


def test_sanitizes_model_visible_payload_recursively():
    payload = {
        "success": True,
        "client_session_id": "agent-d402f480-c8c",
        "continuity_token": "v1.payloadpayloadpayloadpayload.sigsignaturesignaturesignature",
        "raw_governance": {"debug": "hidden"},
        "continuity_token_supported": True,
        "nested": {
            "client-session-id": "agent-d402f480-c8c",
            "message": "bound to agent-d402f480-c8c",
        },
    }

    out = sanitize_model_visible_payload(payload)

    assert "client_session_id" not in out
    assert "continuity_token" not in out
    assert "continuity_token_supported" not in out
    assert "raw_governance" not in out
    assert "client-session-id" not in out["nested"]
    assert "agent-d402f480-c8c" not in out["nested"]["message"]


def test_sanitizes_embedded_json_text_payload():
    text = json.dumps({
        "success": True,
        "client_session_id": "agent-d402f480-c8c",
        "raw_governance": {"continuity_token": "v1.payloadpayloadpayloadpayload.sigsignaturesignaturesignature"},
        "next_action": "call sync_state with agent-d402f480-c8c",
    })

    out = sanitize_model_visible_payload({"content": [{"type": "text", "text": text}]})
    parsed = json.loads(out["content"][0]["text"])

    assert "client_session_id" not in parsed
    assert "raw_governance" not in parsed
    assert "agent-d402f480-c8c" not in parsed["next_action"]
