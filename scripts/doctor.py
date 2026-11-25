#!/usr/bin/env python3
"""Read-only client-side setup check for the UNITARES Governance plugin.

Checks the local host prerequisites, plugin manifests, server reachability,
file-lease plane status, host hook wiring, workspace continuity cache, and
Codex hook receipts without mutating state or printing credentials.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _http_auth  # noqa: E402
import file_lease_hook  # noqa: E402
import hook_invocations  # noqa: E402
import session_cache  # noqa: E402


TRUTHY = {"1", "true", "on", "yes"}
DEFAULT_SERVER_URL = "http://localhost:8767"
DEFAULT_LEASE_URL = "http://127.0.0.1:8788"
MAX_HEALTH_BYTES = 65536
KNOWN_ONBOARDING_KEYS = ("harness", "basis", "source", "policy")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _clean_text(value: Any, *, max_len: int = 80) -> str:
    cleaned = _CONTROL_CHARS_RE.sub(" ", str(value)).strip()
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_len:
        return cleaned[: max_len - 1] + "…"
    return cleaned


def _clamp_timeout(timeout: float) -> float:
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
        return 2.0
    return min(max(0.1, float(timeout)), 30.0)


def _strip_mcp_suffix(path: str) -> str:
    trimmed = path.rstrip("/")
    if trimmed.endswith("/mcp"):
        trimmed = trimmed[:-4].rstrip("/")
    return trimmed


def _redact_url(raw: str) -> str:
    """Return scheme://host[:port][path], stripping userinfo/query/fragment."""
    trimmed = raw.strip()
    if not trimmed:
        return "<empty-url>"
    if trimmed.startswith("unix://"):
        path_part = trimmed.split("?", 1)[0].split("#", 1)[0]
        return _clean_text(path_part, max_len=200)
    try:
        parsed = urllib.parse.urlsplit(trimmed)
    except ValueError:
        return "<invalid-url>"
    scheme = parsed.scheme.lower()
    if not scheme:
        return "<invalid-url>"
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return f"{scheme}://<invalid-host>"
    if not host:
        return f"{scheme}://<missing-host>"
    host_display = f"[{host}]" if ":" in host else host
    port_suffix = f":{port}" if port is not None else ""
    path = _strip_mcp_suffix(parsed.path)
    return f"{scheme}://{host_display}{port_suffix}{path}"


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY


def _check(
    name: str,
    status: str,
    summary: str,
    *,
    detail: dict[str, Any] | None = None,
    fix: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "status": status, "summary": summary}
    if detail:
        item["detail"] = detail
    if fix:
        item["fix"] = fix
    return item


def check_system_python(*, python_path: Path | None = None) -> dict[str, Any]:
    if os.name == "nt" and python_path is None:
        return _check(
            "system_python",
            "warn",
            "Windows hooks require Git Bash with python3 reachable under /usr/bin",
            fix="Verify `/usr/bin/python3 --version` inside Git Bash.",
        )
    target = python_path or Path("/usr/bin/python3")
    if not target.is_file() or not os.access(target, os.X_OK):
        return _check(
            "system_python",
            "fail",
            f"{target} not found or not executable (hooks reset PATH to /usr/bin:/bin:/usr/sbin:/sbin)",
            detail={"path": str(target)},
            fix="On macOS run `xcode-select --install`; on Linux install system python3.",
        )
    try:
        proc = subprocess.run(
            [str(target), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _check(
            "system_python",
            "fail",
            f"failed to execute {target}: {type(exc).__name__}",
            detail={"path": str(target)},
        )
    version = _clean_text(proc.stdout or proc.stderr, max_len=64)
    if proc.returncode != 0 or not version:
        return _check(
            "system_python",
            "fail",
            f"{target} exited {proc.returncode}",
            detail={"path": str(target)},
            fix="On macOS run `xcode-select --install` to complete Command Line Tools setup.",
        )
    return _check(
        "system_python",
        "ok",
        f"{target} ({version})",
        detail={"path": str(target), "version": version},
    )


def check_plugin_manifests(plugin_root: Path) -> dict[str, Any]:
    required = [
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        "hooks/claude-hooks.json",
        "hooks/codex-hooks.json",
        "hooks/agy-hooks.json",
        "hooks/run-hook.cmd",
        "skills/SKILLS_MANIFEST.sha256",
    ]
    missing = [rel for rel in required if not (plugin_root / rel).is_file()]
    collision = (plugin_root / "hooks" / "hooks.json").exists()
    if missing or collision:
        reasons: list[str] = []
        if missing:
            reasons.append(f"missing {', '.join(missing)}")
        if collision:
            reasons.append("hooks/hooks.json exists (collides with Claude Code hook discovery)")
        return _check(
            "plugin_manifests",
            "fail",
            "; ".join(reasons),
            detail={"missing": missing, "hooks_json_collision": collision},
            fix="Reinstall or sync a clean unitares-governance-plugin checkout.",
        )
    version = "unknown"
    try:
        data = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        if isinstance(data, dict):
            version = _clean_text(data.get("version") or "unknown", max_len=40)
    except (OSError, ValueError):
        pass
    skills_count = len(list((plugin_root / "skills").glob("*/SKILL.md")))
    return _check(
        "plugin_manifests",
        "ok",
        f"plugin v{version}, {skills_count} skill(s), all host manifests present",
        detail={"version": version, "skills_count": skills_count},
    )


def check_agy_hooks(
    plugin_root: Path,
    *,
    host: str,
    target_path: Path | None = None,
) -> dict[str, Any]:
    if host not in ("auto", "agy"):
        return _check("agy_hooks", "skip", f"skipped for --host {host}")
    try:
        import install_agy_hooks  # lazy import: install_agy_hooks imports fcntl (POSIX-only)
    except ImportError as exc:
        status = "warn" if host == "agy" else "skip"
        return _check(
            "agy_hooks",
            status,
            f"Antigravity hook helper unavailable on this platform ({type(exc).__name__})",
        )
    target = target_path or install_agy_hooks.DEFAULT_TARGET
    if not target.is_file():
        if host == "agy":
            return _check(
                "agy_hooks",
                "fail",
                f"{target} does not exist",
                detail={"path": str(target)},
                fix="Run `python3 scripts/install_agy_hooks.py` (add `--auto-onboard` for lazy Stop onboarding).",
            )
        return _check(
            "agy_hooks",
            "skip",
            f"Antigravity hooks file not present ({target})",
            detail={"path": str(target)},
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _check(
            "agy_hooks",
            "fail",
            f"could not parse {target}: {type(exc).__name__}",
            detail={"path": str(target)},
        )
    current = data.get(install_agy_hooks.HOOK_GROUP_KEY) if isinstance(data, dict) else None
    if current is None:
        status = "fail" if host == "agy" else "warn"
        return _check(
            "agy_hooks",
            status,
            f"{install_agy_hooks.HOOK_GROUP_KEY!r} group missing from {target}",
            detail={"path": str(target)},
            fix="Run `python3 scripts/install_agy_hooks.py`.",
        )
    try:
        variants = [
            (
                auto_onboard,
                keep_var,
                install_agy_hooks.render_agy_hook_entry(
                    plugin_root,
                    auto_onboard=auto_onboard,
                    keep_plugin_root_var=keep_var,
                ),
            )
            for auto_onboard in (True, False)
            for keep_var in (False, True)
        ]
    except (OSError, ValueError) as exc:
        return _check(
            "agy_hooks",
            "fail",
            f"failed to render hooks/agy-hooks.json: {type(exc).__name__}",
        )
    for auto_onboard, keep_var, expected in variants:
        if current == expected:
            mode = "auto_onboard=on" if auto_onboard else "auto_onboard=off"
            root_note = ", keep_plugin_root_var=true" if keep_var else ""
            return _check(
                "agy_hooks",
                "ok",
                f"synced in {target} ({mode}{root_note})",
                detail={
                    "path": str(target),
                    "auto_onboard": auto_onboard,
                    "keep_plugin_root_var": keep_var,
                },
            )
    return _check(
        "agy_hooks",
        "warn",
        f"{target} differs from this checkout's hooks/agy-hooks.json",
        detail={"path": str(target)},
        fix="Run `python3 scripts/install_agy_hooks.py` (or `--auto-onboard`) to resync.",
    )


def check_server_health(
    server_url: str,
    *,
    timeout: float,
    no_network: bool,
) -> dict[str, Any]:
    clamped = _clamp_timeout(timeout)
    raw_url = server_url.strip()
    safe_url = _redact_url(raw_url)
    if no_network:
        return _check(
            "server_health",
            "skip",
            f"skipped (--no-network); configured URL {safe_url}",
            detail={"url": safe_url},
        )
    try:
        parsed = urllib.parse.urlsplit(raw_url)
    except ValueError:
        return _check(
            "server_health",
            "fail",
            f"invalid server URL ({safe_url})",
            detail={"url": safe_url},
            fix="Pass a full http://localhost:<port> or https://<host> URL.",
        )
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return _check(
            "server_health",
            "fail",
            f"invalid server URL ({safe_url}): missing http:// or https:// scheme",
            detail={"url": safe_url},
            fix="Pass a full http://localhost:<port> or https://<host> URL.",
        )
    if parsed.username is not None or parsed.password is not None:
        return _check(
            "server_health",
            "fail",
            f"invalid server URL ({safe_url}): URL must not contain embedded userinfo",
            detail={"url": safe_url},
            fix="Remove embedded credentials (`user:pass@`) from UNITARES_SERVER_URL.",
        )
    normalized_path = _strip_mcp_suffix(parsed.path)
    base_no_query = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))
    health_url = f"{base_no_query}/health"
    safe_health_url = f"{safe_url}/health"
    try:
        req = urllib.request.Request(health_url, headers={"Accept": "application/json"}, method="GET")
        with _http_auth.authorization_safe_urlopen(req, timeout=clamped) as resp:
            code = getattr(resp, "status", 200)
            raw = _http_auth.read_limited_response(resp, max_bytes=MAX_HEALTH_BYTES)
    except _http_auth.InsecureBearerTransportError as exc:
        return _check(
            "server_health",
            "fail",
            f"{safe_url}: {_clean_text(exc, max_len=160)}",
            detail={"url": safe_url},
            fix="Use https:// for non-loopback servers or http://localhost:<port> locally.",
        )
    except urllib.error.HTTPError as exc:
        return _check(
            "server_health",
            "fail",
            f"{safe_health_url} returned HTTP {exc.code}",
            detail={"url": safe_url, "http_status": exc.code},
            fix="Check UNITARES_SERVER_URL and server logs.",
        )
    except (
        _http_auth.ResponseTooLargeError,
        http.client.HTTPException,
        OSError,
        urllib.error.URLError,
        ValueError,
    ) as exc:
        return _check(
            "server_health",
            "fail",
            f"cannot reach {safe_health_url} ({type(exc).__name__})",
            detail={"url": safe_url},
            fix="Start the UNITARES stack (`docker compose up -d --wait`) or verify UNITARES_SERVER_URL.",
        )
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        return _check(
            "server_health",
            "fail",
            f"{safe_health_url} returned non-JSON response (HTTP {code})",
            detail={"url": safe_url, "http_status": code},
            fix="Verify UNITARES_SERVER_URL points to a UNITARES governance server.",
        )
    if not isinstance(payload, dict) or "status" not in payload:
        return _check(
            "server_health",
            "fail",
            f"{safe_health_url} did not return a UNITARES health payload",
            detail={"url": safe_url, "http_status": code},
        )
    srv_status = _clean_text(payload.get("status"), max_len=32)
    version = _clean_text(payload["version"], max_len=40) if payload.get("version") is not None else None
    raw_onboarding = payload.get("onboarding")
    onboarding: dict[str, str] = {}
    if isinstance(raw_onboarding, dict):
        for key in KNOWN_ONBOARDING_KEYS:
            val = raw_onboarding.get(key)
            if val is not None:
                onboarding[key] = _clean_text(val, max_len=64)
    parts = [f"{safe_url} status={srv_status}"]
    if version:
        parts.append(f"v{version}")
    if onboarding.get("harness"):
        parts.append(f"harness_onboarding={onboarding['harness']}")
    detail: dict[str, Any] = {"url": safe_url, "server_status": srv_status}
    if version:
        detail["version"] = version
    if onboarding:
        detail["onboarding"] = onboarding
    status = "ok" if srv_status in ("ok", "healthy") else "warn"
    return _check("server_health", status, ", ".join(parts), detail=detail)


@contextmanager
def _isolated_env(overrides: dict[str, str | None]) -> Iterator[None]:
    """Apply env overrides and restore the full process environment on exit.

    Prevents lazy ``secrets.env`` loading inside ``file_lease_hook`` from
    mutating ``os.environ`` for subsequent in-process callers or tests.
    """
    saved = os.environ.copy()
    for key, val in overrides.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _required_lease_fix(reason: str, url: str) -> str:
    if reason == "bearer_rejected":
        return f"Lease plane at {url} rejected the bearer; update LEASE_PLANE_BEARER_TOKEN to match the running service."
    if reason == "no_bearer":
        return f"Configure LEASE_PLANE_BEARER_TOKEN for {url} (required mode denies edits without a valid bearer)."
    if reason == "insecure_transport":
        if url.startswith("unix://"):
            return "Ensure the Unix socket is a non-symlink AF_UNIX socket owned by the current user with 0600 permissions."
        return (
            "Use HTTPS or UNITARES_LEASE_PLANE_UNIX_SOCKET, or set "
            "UNITARES_ALLOW_INSECURE_LOOPBACK_LEASE_HTTP=1 for local loopback development."
        )
    if reason == "not_a_lease_plane":
        return f"Point LEASE_PLANE_BASE_URL at a v1 UNITARES lease plane ({url} did not answer as one)."
    return (
        f"Start or restore the UNITARES lease plane at {url} "
        "(UNITARES_FILE_LEASES_REQUIRED=1 is fail-closed and overrides UNITARES_FILE_LEASES_ENABLED=0)."
    )


def check_lease_plane(
    lease_url: str | None,
    *,
    timeout: float,
    no_network: bool,
) -> dict[str, Any]:
    clamped = _clamp_timeout(timeout)
    # Mirror file_lease_hook's exact policy order: read REQUIRED from process env
    # before any lazy secrets.env load, and use empty-string overrides for explicit
    # --lease-url so _load_env_file() does not repopulate socket/URL keys.
    required = _is_truthy(os.environ.get("UNITARES_FILE_LEASES_REQUIRED"))
    overrides: dict[str, str | None] = {}
    if lease_url:
        overrides["LEASE_PLANE_BASE_URL"] = lease_url.strip().rstrip("/")
        overrides["UNITARES_LEASE_PLANE_URL"] = ""
        overrides["UNITARES_LEASE_PLANE_UNIX_SOCKET"] = ""
    with _isolated_env(overrides):
        effective_raw = file_lease_hook._base_url()
        safe_url = _redact_url(effective_raw)
        if no_network:
            return _check(
                "lease_plane",
                "skip",
                f"skipped (--no-network); configured URL {safe_url}",
                detail={"url": safe_url, "required": required},
            )
        result = file_lease_hook.lease_status(
            timeout_s=clamped,
            deadline_s=min(max(clamped + 0.5, 1.5), 35.0),
        )
    state = result.get("state")
    reported_url = _redact_url(str(result.get("url") or effective_raw or DEFAULT_LEASE_URL))
    if state == "disabled":
        return _check(
            "lease_plane",
            "skip",
            "disabled via UNITARES_FILE_LEASES_ENABLED=0",
            detail={"url": reported_url, "state": state, "required": required},
        )
    if state == "on":
        proto = _clean_text(result.get("protocol_version") or "unknown", max_len=32)
        bearer = _clean_text(result.get("bearer") or "configured", max_len=32)
        return _check(
            "lease_plane",
            "ok",
            f"{reported_url} healthy (protocol={proto}, bearer={bearer})",
            detail={"url": reported_url, "protocol_version": proto, "bearer_source": bearer, "required": required},
        )
    reason = _clean_text(result.get("reason") or "unreachable", max_len=40)
    status = "fail" if required else "warn"
    mode_note = (
        "fail-closed: edits will be denied"
        if required
        else "fail-open: edits proceed without cross-agent file leases"
    )
    if required:
        fix = _required_lease_fix(reason, reported_url)
    else:
        sanitized_result = dict(result)
        sanitized_result["url"] = reported_url
        fix = file_lease_hook._status_line(sanitized_result) or (
            "Start the lease plane or check LEASE_PLANE_BASE_URL and LEASE_PLANE_BEARER_TOKEN."
        )
    return _check(
        "lease_plane",
        status,
        f"{reported_url} unavailable ({reason}; {mode_note})",
        detail={"url": reported_url, "reason": reason, "required": required},
        fix=fix,
    )


def _gitignore_ignores_cache(gitignore: Path) -> bool:
    if not gitignore.is_file() or gitignore.is_symlink():
        return False
    try:
        lines = gitignore.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    ignored = False
    for raw in lines:
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("!"):
            if ignored:
                return False
            continue
        if entry in ("*", "/*", "*.json", "session*.json", "session-*.json"):
            ignored = True
    return ignored


def check_workspace_cache(workspace: Path) -> dict[str, Any]:
    cache_dir = workspace / session_cache.CACHE_DIR
    if not cache_dir.exists() and not cache_dir.is_symlink():
        return _check(
            "workspace_cache",
            "ok",
            f"no {cache_dir} directory yet (hooks create it on first session)",
            detail={"path": str(cache_dir), "exists": False},
        )
    if cache_dir.is_symlink() or not cache_dir.is_dir():
        return _check(
            "workspace_cache",
            "warn",
            f"{cache_dir} is a symlink or non-directory (session cache ignores it)",
            detail={"path": str(cache_dir), "exists": True, "is_symlink": cache_dir.is_symlink()},
            fix=f"Replace {cache_dir} with a normal directory.",
        )

    is_home_mirror = workspace.resolve() == Path.home().resolve()
    gitignore = cache_dir / ".gitignore"
    has_gitignore = True if is_home_mirror else _gitignore_ignores_cache(gitignore)
    try:
        valid_slots = session_cache.list_session_slots(workspace)
        entries = sorted(cache_dir.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        return _check(
            "workspace_cache",
            "warn",
            f"could not inspect {cache_dir}: {type(exc).__name__}",
            detail={"path": str(cache_dir), "exists": True},
            fix=f"Verify directory permissions on {cache_dir}.",
        )

    corrupt_count = 0
    for path in entries:
        if path.is_symlink() or not path.is_file():
            continue
        if path.name != "session.json" and session_cache._parse_session_filename(path.name) is None:
            continue
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            corrupt_count += 1
            continue
        if not isinstance(parsed, dict):
            corrupt_count += 1

    bound_prefixes = [entry["parent_agent_id"][:8] for entry in valid_slots]
    detail: dict[str, Any] = {
        "path": str(cache_dir),
        "exists": True,
        "has_gitignore": has_gitignore,
        "slot_count": len(valid_slots),
        "bound_uuid_prefixes": bound_prefixes[:20],
        "corrupt_slots": corrupt_count,
    }
    if not has_gitignore or corrupt_count:
        issues: list[str] = []
        fixes: list[str] = []
        if not has_gitignore:
            issues.append(".unitares/.gitignore missing or ineffective")
            fixes.append("Add `*` to .unitares/.gitignore so local session caches are not committed.")
        if corrupt_count:
            issues.append(f"{corrupt_count} unreadable or invalid JSON file(s)")
            fixes.append(f"Inspect or remove corrupt session*.json file(s) in {cache_dir}.")
        return _check(
            "workspace_cache",
            "warn",
            f"{len(valid_slots)} bound session slot(s) in {cache_dir}; {', '.join(issues)}",
            detail=detail,
            fix=" ".join(fixes),
        )
    ignore_note = "home cache (no .gitignore needed)" if is_home_mirror else ".gitignore present"
    return _check(
        "workspace_cache",
        "ok",
        f"{len(valid_slots)} bound session slot(s) in {cache_dir}; {ignore_note}",
        detail=detail,
    )


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return -1.0


def check_codex_hook_reports(*, host: str, report_root: Path | None = None) -> dict[str, Any]:
    if host not in ("auto", "codex"):
        return _check("codex_hook_reports", "skip", f"skipped for --host {host}")
    root = report_root or hook_invocations.report_dir()
    files = sorted(root.glob("codex-*.jsonl"), key=_safe_mtime, reverse=True) if root.is_dir() else []
    if not files:
        return _check(
            "codex_hook_reports",
            "skip",
            f"no Codex hook receipts in {root}",
            detail={"path": str(root)},
        )
    latest = files[0]
    try:
        with latest.open("rb"):
            pass
    except OSError as exc:
        return _check(
            "codex_hook_reports",
            "warn",
            f"could not read {latest.name}: {type(exc).__name__}",
        )
    parsed = hook_invocations.summarize(latest)
    if parsed is None:
        empty_counts = {name: 0 for name in ("fired", *hook_invocations.RESULTS)}
        return _check(
            "codex_hook_reports",
            "warn",
            f"no schema-1 receipts readable in {latest.name}",
            detail={"file": latest.name, "counts": empty_counts},
            fix="Inspect the receipt file or run a fresh Codex session to verify hook receipt schema.",
        )
    counts = parsed["counts"]
    status = "warn" if (counts["failed"] > 0 or counts["timed_out"] > 0) else "ok"
    summary = (
        f"latest {latest.name}: {counts['fired']} fired ({counts['succeeded']} succeeded, "
        f"{counts['skipped']} skipped, {counts['failed']} failed, "
        f"{counts['timed_out']} timed_out, {counts['incomplete']} incomplete)"
    )
    fix = "Run `python3 scripts/hook_invocations.py report --latest` for per-hook breakdown." if status == "warn" else None
    return _check("codex_hook_reports", status, summary, detail={"file": latest.name, "counts": counts}, fix=fix)


def run_doctor(
    *,
    plugin_root: Path = PLUGIN_ROOT,
    workspace: Path | None = None,
    host: str = "auto",
    server_url: str | None = None,
    lease_url: str | None = None,
    timeout: float = 2.0,
    no_network: bool = False,
    python_path: Path | None = None,
    agy_hooks_path: Path | None = None,
    codex_report_dir: Path | None = None,
) -> dict[str, Any]:
    ws = (workspace or Path.cwd()).resolve()
    effective_server = server_url or os.environ.get("UNITARES_SERVER_URL") or DEFAULT_SERVER_URL
    checks = [
        check_system_python(python_path=python_path),
        check_plugin_manifests(plugin_root),
        check_agy_hooks(plugin_root, host=host, target_path=agy_hooks_path),
        check_server_health(effective_server, timeout=timeout, no_network=no_network),
        check_lease_plane(lease_url, timeout=timeout, no_network=no_network),
        check_workspace_cache(ws),
        check_codex_hook_reports(host=host, report_root=codex_report_dir),
    ]
    summary_counts = {s: sum(1 for c in checks if c["status"] == s) for s in ("ok", "warn", "fail", "skip")}
    overall = "fail" if summary_counts["fail"] > 0 else ("warn" if summary_counts["warn"] > 0 else "ok")
    return {
        "status": overall,
        "host": host,
        "plugin_root": str(plugin_root),
        "workspace": str(ws),
        "counts": summary_counts,
        "checks": checks,
    }


def _format_text(report: dict[str, Any]) -> str:
    tags = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
    lines = [f"UNITARES plugin doctor ({report['status'].upper()}) — host={report['host']}"]
    for item in report["checks"]:
        lines.append(f"  [{tags[item['status']]}] {item['name']}: {item['summary']}")
        if item.get("fix"):
            lines.append(f"         fix: {item['fix']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only setup and wiring diagnostics for unitares-governance-plugin.",
    )
    parser.add_argument(
        "--host",
        choices=("auto", "claude", "codex", "agy"),
        default="auto",
        help="Host surface to check (default: auto).",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Workspace directory to inspect for .unitares/ session caches (default: current directory).",
    )
    parser.add_argument(
        "--plugin-root",
        type=Path,
        default=PLUGIN_ROOT,
        help="Plugin checkout root (default: parent of scripts/).",
    )
    parser.add_argument(
        "--server-url",
        default=None,
        help="Override UNITARES_SERVER_URL (default: env or http://localhost:8767).",
    )
    parser.add_argument(
        "--lease-url",
        default=None,
        help=(
            "Override lease plane URL (takes precedence over UNITARES_LEASE_PLANE_UNIX_SOCKET "
            "and probes with the configured lease bearer token; default: env or http://127.0.0.1:8788)."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="HTTP probe timeout in seconds (default: 2.0).",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Skip live HTTP probes to the governance server and lease plane.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_doctor(
        plugin_root=args.plugin_root.resolve(),
        workspace=args.workspace,
        host=args.host,
        server_url=args.server_url,
        lease_url=args.lease_url,
        timeout=args.timeout,
        no_network=args.no_network,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(_format_text(report))
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
