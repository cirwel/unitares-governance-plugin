#!/usr/bin/env python3
"""Audit local UNITARES identity-contract surfaces.

This is an operator/CI guardrail for the shared client contract. It does not
prove identity and does not contact the governance server. It checks the local
surfaces that thin clients can actually corrupt:

* slot-scoped session caches under .unitares/
* the optional legacy flat session.json
* hook diagnostic lines in checkins.log

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


def audit_checkin_log(log_path: Path) -> list[Finding]:
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

    for index, line in enumerate(lines, start=1):
        if not line.strip():
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
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--fail-on-warning", action="store_true", help="exit 1 when warnings are present")
    args = parser.parse_args(argv)

    workspace = _workspace_path(args.workspace)
    log_path = Path(args.log).expanduser() if args.log else _default_log_path()

    findings = audit_session_caches(workspace) + audit_checkin_log(log_path)
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]

    if args.json:
        print(json.dumps({
            "workspace": str(workspace),
            "log": str(log_path),
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
