"""Secret redaction for plugin check-in payloads.

Matches common API key / token patterns and replaces them with a labelled
placeholder before text is submitted to governance. Governance data lives
on the operator's own machine, so this is defense in depth — not a
security boundary.

Patterns are deliberately narrow. We'd rather miss some secrets than
mangle legitimate text that happens to look secret-ish.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("anthropic_key", re.compile(r"sk-ant-[a-zA-Z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"sk-(?:proj-)?[a-zA-Z0-9]{32,}")),
    ("github_token", re.compile(r"gh[pousr]_[a-zA-Z0-9]{20,}")),
    ("aws_key", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    ("generic_bearer", re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{40,}\b")),
    ("unitares_continuity_token", re.compile(r"\bv1\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b")),
    ("unitares_client_session", re.compile(r"\bagent:/[A-Za-z0-9][A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*\b")),
    ("unitares_client_session", re.compile(r"\bagent-(?=[A-Za-z0-9_.:\-]*\d)[A-Za-z0-9][A-Za-z0-9_.:\-]{8,}\b")),
]

_MODEL_HIDDEN_KEYS = frozenset({
    "client_session_id",
    "continuity_token",
    "continuity_token_supported",
    "raw_governance",
})


def redact_secrets(text: Optional[str]) -> str:
    """Replace recognized secret patterns in ``text`` with labelled tokens."""
    if not text:
        return ""
    result = text
    for label, pattern in _PATTERNS:
        result = pattern.sub(f"[REDACTED:{label}]", result)
    return result


def _normalized_key(key: Any) -> str:
    if not isinstance(key, str):
        return ""
    return key.replace("-", "_").lower()


def sanitize_model_visible_payload(value: Any) -> Any:
    """Remove governance proof material from normal model-facing payloads.

    The sidecar still receives and uses the raw server response internally for
    cache stamping. This helper is only for payloads that leave the facade and
    may become visible to an agent, transcript, or operator-facing normal path.
    """
    if isinstance(value, dict):
        sanitized: dict[Any, Any] = {}
        for key, item in value.items():
            if _normalized_key(key) in _MODEL_HIDDEN_KEYS:
                continue
            sanitized[key] = sanitize_model_visible_payload(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_model_visible_payload(item) for item in value]
    if isinstance(value, str):
        return _sanitize_model_visible_text(value)
    return value


def _sanitize_model_visible_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return redact_secrets(text)
        return json.dumps(
            sanitize_model_visible_payload(parsed),
            separators=(",", ":"),
            sort_keys=True,
        )
    return redact_secrets(text)
