#!/usr/bin/env python3
"""Audit local UNITARES identity-contract surfaces.

This is an operator/CI guardrail for the shared client contract. It does not
prove identity and does not contact the governance server. It checks the local
surfaces that thin clients can actually corrupt:

* slot-scoped session caches under .unitares/
* the optional legacy flat session.json
* hook diagnostic lines in checkins.log
* optional captured MCP response JSON files passed via --response

Exit codes:
  0 = no errors, and no warnings unless --fail-on-warning is set
  1 = warnings only with --fail-on-warning
  2 = hard contract violations
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CACHE_DIR = ".unitares"
WEAK_SOURCES = (
    "ip_ua",
    "fingerprint",
    "path2",
    "recent_onboard",
    "implicit",
    "heuristic",
)
FALLBACK_STATUSES = {"floor_sent", "floor_fail", "fail", "error"}
SLOT_RE = re.compile(r"session-(?P<slot>[A-Za-z0-9_-]{1,64})\.json$")
IDENTITY_RESPONSE_SCHEMA = "s22.identity_response.v1"


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


def _workspace_path(raw: str | None) -> Path:
    return Path(raw or os.getcwd()).expanduser().resolve()


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "JSON root is not an object"
    return data, None


def _read_json_any(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:
        return None, str(exc)


def _session_paths(workspace: Path) -> list[Path]:
    cache_dir = workspace / CACHE_DIR
    if not cache_dir.is_dir():
        return []
    paths: list[Path] = []
    for path in cache_dir.iterdir():
        if not path.is_file():
            continue
        if path.name == "session.json" or SLOT_RE.fullmatch(path.name):
            paths.append(path)
    return sorted(paths)


def _identity_assurance_tier(data: dict[str, Any]) -> str:
    ia = data.get("identity_assurance")
    if isinstance(ia, dict):
        tier = ia.get("tier")
        if isinstance(tier, str):
            return tier.strip().lower()
    ctx = data.get("identity_context")
    if isinstance(ctx, dict):
        nested = ctx.get("identity_assurance")
        if isinstance(nested, dict):
            tier = nested.get("tier")
            if isinstance(tier, str):
                return tier.strip().lower()
    return ""


def audit_session_caches(workspace: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _session_paths(workspace):
        rel = str(path)
        is_flat = path.name == "session.json"
        data, error = _read_json(path)
        if error:
            findings.append(Finding(
                "error",
                "session_cache_invalid_json",
                rel,
                f"session cache is unreadable JSON: {error}",
            ))
            continue
        assert data is not None

        uuid = data.get("uuid")
        sid = data.get("client_session_id")
        token = data.get("continuity_token")

        if isinstance(token, str) and token.strip():
            findings.append(Finding(
                "error",
                "session_cache_token_at_rest",
                rel,
                "v2 session caches must not persist non-empty continuity_token",
            ))

        if not uuid and not sid:
            findings.append(Finding(
                "error",
                "session_cache_missing_identity",
                rel,
                "session cache has neither uuid nor client_session_id",
            ))

        if is_flat and (uuid or sid):
            findings.append(Finding(
                "warning",
                "flat_session_cache_present",
                rel,
                "flat session.json is legacy/shared; prefer slot-scoped session-<slot>.json",
            ))

        source = data.get("session_resolution_source")
        if isinstance(source, str) and any(part in source.lower() for part in WEAK_SOURCES):
            findings.append(Finding(
                "warning",
                "weak_session_resolution_source",
                rel,
                f"session_resolution_source is weak or heuristic: {source}",
            ))

        tier = _identity_assurance_tier(data)
        if tier and tier != "strong":
            findings.append(Finding(
                "warning",
                "weak_identity_assurance",
                rel,
                f"identity_assurance tier is {tier}, expected strong for ordinary check-ins",
            ))

    return findings


def _parse_log_line(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in line.strip().split("|"):
        part = part.strip()
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key.strip()] = value.strip().strip('"')
    return fields


def _parse_log_timestamp(line: str) -> datetime | None:
    raw = line.split("|", 1)[0].strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_since(raw: str | None, *, now: datetime | None = None) -> datetime | None:
    """Parse a monitor window.

    Supports ISO-8601 timestamps plus shorthand durations: 30m, 12h, 7d.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d+)([mhd])", text, flags=re.IGNORECASE)
    if match:
        value = int(match.group(1))
        unit = match.group(2).lower()
        current = now or datetime.now(timezone.utc)
        if unit == "m":
            return current - timedelta(minutes=value)
        if unit == "h":
            return current - timedelta(hours=value)
        return current - timedelta(days=value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--since must be ISO-8601 or shorthand like 30m, 12h, 7d") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def audit_checkin_log(
    log_path: Path,
    *,
    log_tail: int | None = None,
    since: datetime | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    if not log_path.exists():
        return findings
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return [Finding(
            "warning",
            "checkin_log_unreadable",
            str(log_path),
            f"check-in log could not be read: {exc}",
        )]

    indexed_lines = list(enumerate(lines, start=1))
    if log_tail is not None and log_tail > 0:
        indexed_lines = indexed_lines[-log_tail:]

    for index, line in indexed_lines:
        if not line.strip():
            continue
        stamp = _parse_log_timestamp(line)
        if since is not None and stamp is not None and stamp < since:
            continue
        fields = _parse_log_line(line)
        status = fields.get("status", "")
        event = fields.get("event", "")
        uuid = fields.get("uuid", "")
        path_ref = f"{log_path}:{index}"

        if status in FALLBACK_STATUSES:
            severity = "warning"
            code = "checkin_fallback_status"
            if status in {"fail", "error", "floor_fail"}:
                code = "checkin_delivery_problem"
            findings.append(Finding(
                severity,
                code,
                path_ref,
                f"check-in log status={status} event={event or '?'}",
            ))

        if uuid == "?" and not status.startswith("floor"):
            findings.append(Finding(
                "warning",
                "checkin_unknown_uuid",
                path_ref,
                "identity-bound check-in log line has unknown uuid marker",
            ))

    return findings


def _nonempty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _iter_json_objects(value: Any):
    """Yield all dicts inside a decoded response, including JSON strings."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_objects(child)
    elif isinstance(value, str):
        text = value.strip()
        if not (text.startswith("{") or text.startswith("[")):
            return
        try:
            decoded = json.loads(text)
        except Exception:
            return
        yield from _iter_json_objects(decoded)


def _response_paths(paths: list[str]) -> list[Path]:
    resolved: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            resolved.extend(sorted(p for p in path.rglob("*.json") if p.is_file()))
        else:
            resolved.append(path)
    return resolved


def _audit_identity_context(
    *,
    payload: dict[str, Any],
    context: dict[str, Any],
    path_ref: str,
) -> list[Finding]:
    findings: list[Finding] = []
    schema = context.get("schema")
    if schema != IDENTITY_RESPONSE_SCHEMA:
        findings.append(Finding(
            "error",
            "identity_context_wrong_schema",
            path_ref,
            f"identity_context.schema is {schema!r}, expected {IDENTITY_RESPONSE_SCHEMA}",
        ))
        return findings

    registry_uuid = None
    registry = context.get("registry")
    if isinstance(registry, dict):
        registry_uuid = _nonempty_text(registry.get("uuid"))
    payload_uuid = _nonempty_text(payload.get("uuid"))
    if payload_uuid and registry_uuid and payload_uuid != registry_uuid:
        findings.append(Finding(
            "error",
            "identity_context_uuid_mismatch",
            path_ref,
            "payload uuid does not match identity_context.registry.uuid",
        ))

    public_handle = None
    public = context.get("public_handle")
    if isinstance(public, dict):
        public_handle = _nonempty_text(public.get("agent_id"))
    payload_agent_id = _nonempty_text(payload.get("agent_id"))
    if payload_agent_id and public_handle and payload_agent_id != public_handle:
        findings.append(Finding(
            "error",
            "identity_context_agent_id_mismatch",
            path_ref,
            "payload agent_id does not match identity_context.public_handle.agent_id",
        ))

    label_display = None
    label = context.get("label")
    if isinstance(label, dict):
        label_display = _nonempty_text(label.get("display_name"))
    payload_display = _nonempty_text(payload.get("display_name"))
    if payload_display and label_display and payload_display != label_display:
        findings.append(Finding(
            "error",
            "identity_context_display_name_mismatch",
            path_ref,
            "payload display_name does not match identity_context.label.display_name",
        ))

    if context.get("agent_id_is") != "public_structured_handle":
        findings.append(Finding(
            "error",
            "identity_context_agent_id_role",
            path_ref,
            "identity_context.agent_id_is must be public_structured_handle",
        ))

    return findings


def _audit_agent_signature(signature: dict[str, Any], path_ref: str) -> list[Finding]:
    findings: list[Finding] = []
    uuid = _nonempty_text(signature.get("uuid"))
    agent_id = _nonempty_text(signature.get("agent_id"))
    structured_agent_id = _nonempty_text(signature.get("structured_agent_id"))
    display_name = _nonempty_text(signature.get("display_name"))
    label_source = _nonempty_text(signature.get("label_source"))

    if agent_id and structured_agent_id and agent_id != structured_agent_id:
        findings.append(Finding(
            "error",
            "agent_signature_competing_public_handles",
            path_ref,
            "agent_signature.agent_id and structured_agent_id differ; one public handle contract is broken",
        ))

    if agent_id and display_name and agent_id == display_name and label_source == "claimed":
        findings.append(Finding(
            "error",
            "agent_signature_label_in_agent_id",
            path_ref,
            "claimed display label appears in agent_signature.agent_id",
        ))

    context = signature.get("identity_context")
    if uuid and agent_id and not isinstance(context, dict):
        findings.append(Finding(
            "error",
            "agent_signature_missing_identity_context",
            path_ref,
            "bound agent_signature is missing s22.identity_response.v1 identity_context",
        ))
    elif isinstance(context, dict):
        findings.extend(_audit_identity_context(
            payload=signature,
            context=context,
            path_ref=path_ref,
        ))

    return findings


def audit_response_captures(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in _response_paths(paths):
        data, error = _read_json_any(path)
        if error:
            findings.append(Finding(
                "error",
                "response_capture_invalid_json",
                str(path),
                f"response capture is unreadable JSON: {error}",
            ))
            continue

        assert data is not None
        for index, obj in enumerate(_iter_json_objects(data), start=1):
            path_ref = f"{path}#object{index}"
            signature = obj.get("agent_signature")
            if isinstance(signature, dict):
                findings.extend(_audit_agent_signature(signature, path_ref))
            context = obj.get("identity_context")
            if isinstance(context, dict):
                findings.extend(_audit_identity_context(
                    payload=obj,
                    context=context,
                    path_ref=path_ref,
                ))

    return findings


def _default_log_path() -> Path:
    raw = os.environ.get("UNITARES_CHECKIN_LOG", "~/.unitares/checkins.log")
    return Path(raw).expanduser()


def _print_human(findings: list[Finding]) -> None:
    if not findings:
        print("identity-contract audit OK")
        return
    for severity in ("error", "warning"):
        group = [f for f in findings if f.severity == severity]
        if not group:
            continue
        print(f"{severity.upper()}S ({len(group)}):")
        for finding in group:
            print(f"- {finding.code}: {finding.path} — {finding.message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit local UNITARES slot-cache and check-in-log contract surfaces."
    )
    parser.add_argument("--workspace", default=None, help="workspace root to inspect; default: cwd")
    parser.add_argument("--log", default=None, help="check-in log path; default: UNITARES_CHECKIN_LOG or ~/.unitares/checkins.log")
    parser.add_argument("--log-tail", type=int, default=None, help="inspect only the last N check-in log lines")
    parser.add_argument("--since", default=None, help="inspect log lines at or after ISO timestamp or shorthand duration such as 24h, 30m, 7d")
    parser.add_argument("--response", action="append", default=[], help="captured MCP response JSON file or directory to audit; may be repeated")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--fail-on-warning", action="store_true", help="exit 1 when warnings are present")
    args = parser.parse_args(argv)

    workspace = _workspace_path(args.workspace)
    log_path = Path(args.log).expanduser() if args.log else _default_log_path()
    try:
        since = parse_since(args.since)
    except ValueError as exc:
        print(f"audit_identity_contract.py: {exc}", file=sys.stderr)
        return 2

    findings = audit_session_caches(workspace) + audit_checkin_log(
        log_path,
        log_tail=args.log_tail,
        since=since,
    ) + audit_response_captures(args.response)
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]

    if args.json:
        print(json.dumps({
            "workspace": str(workspace),
            "log": str(log_path),
            "log_tail": args.log_tail,
            "since": since.isoformat() if since else None,
            "responses": [str(path) for path in _response_paths(args.response)],
            "errors": len(errors),
            "warnings": len(warnings),
            "findings": [f.as_dict() for f in findings],
        }, indent=2, sort_keys=True))
    else:
        _print_human(findings)

    if errors:
        return 2
    if warnings and args.fail_on_warning:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
