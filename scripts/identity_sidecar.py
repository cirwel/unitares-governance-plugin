#!/usr/bin/env python3
"""Local UNITARES identity sidecar.

This is a thin REST sidecar for clients that do not have lifecycle hooks. It
does not implement governance policy and does not replace the UNITARES server.
It wraps the server's `/v1/tools/call` surface with local lifecycle help:

* lazy onboard when a slot has no cached `client_session_id`
* inject `client_session_id` into attribution-relevant governance calls
* force fresh posture for bare `onboard` / `start_session` calls
* stamp the slot cache after successful check-ins
* expose the local identity-contract audit

Phase 1 intentionally stays REST-only. A full streamable-MCP proxy can reuse the
same core behavior once the transport boundary is worth owning directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_identity_contract import audit_checkin_log, audit_session_caches  # noqa: E402
from checkin import _plugin_version, submit_checkin  # noqa: E402
from governance_call_inject import INJECT_SUFFIXES, PROOF_FIELDS  # noqa: E402
from onboard_helper import (  # noqa: E402
    DEFAULT_SERVER_URL,
    _read_cache,
    _write_cache,
    run_onboard,
    unwrap_tool_response,
)


IDENTITY_TOOL_NAMES = {"onboard", "start_session", "identity", "bind_session"}
START_TOOL_NAMES = {"onboard", "start_session"}
CHECKIN_TOOL_NAMES = {"process_agent_update", "sync_state"}
DEFAULT_PORT = 8768
DEFAULT_TIMEOUT = 20.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_hash(workspace: Path) -> str:
    return hashlib.md5(str(workspace).encode("utf-8")).hexdigest()[:8]


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_request_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw_len = handler.headers.get("Content-Length") or "0"
    try:
        length = max(0, int(raw_len))
    except ValueError:
        length = 0
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _has_proof_field(arguments: dict[str, Any]) -> bool:
    for field in PROOF_FIELDS:
        value = arguments.get(field)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False


def _post_json(url: str, payload: dict[str, Any], timeout: float, token: str | None) -> tuple[dict[str, Any], int, str | None]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return {}, int((time.monotonic() - started) * 1000), str(getattr(exc, "reason", exc))
    try:
        data = json.loads(raw)
    except Exception as exc:
        return {}, int((time.monotonic() - started) * 1000), f"invalid json: {exc}"
    return data if isinstance(data, dict) else {}, int((time.monotonic() - started) * 1000), None


class IdentitySidecar:
    def __init__(
        self,
        *,
        server_url: str,
        workspace: Path,
        agent_name: str,
        model_type: str,
        default_slot: str,
        timeout: float = DEFAULT_TIMEOUT,
        auth_token: str | None = None,
        log_path: Path | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.workspace = workspace
        self.agent_name = agent_name
        self.model_type = model_type
        self.default_slot = default_slot
        self.timeout = timeout
        self.auth_token = auth_token
        self.log_path = log_path or Path(os.environ.get("UNITARES_CHECKIN_LOG", "~/.unitares/checkins.log")).expanduser()

    def slot_from(self, body: dict[str, Any], headers: Any) -> str:
        header_slot = headers.get("X-UNITARES-Slot") if headers else ""
        sidecar = body.get("sidecar")
        body_slot = ""
        if isinstance(sidecar, dict):
            raw = sidecar.get("slot")
            body_slot = raw if isinstance(raw, str) else ""
        raw_top = body.get("slot")
        if isinstance(raw_top, str) and raw_top.strip():
            body_slot = raw_top
        slot = (header_slot or body_slot or self.default_slot or "").strip()
        return slot or f"sidecar-{_workspace_hash(self.workspace)}"

    def read_session(self, slot: str) -> dict[str, Any]:
        return _read_cache(self.workspace, slot)

    def write_session(self, slot: str, payload: dict[str, Any]) -> None:
        clean = {k: v for k, v in payload.items() if k != "continuity_token"}
        clean.setdefault("schema_version", 2)
        clean["updated_at"] = _now_iso()
        _write_cache(self.workspace, clean, slot)

    def stamp_session(self, slot: str) -> None:
        session = self.read_session(slot)
        if not session:
            return
        session.pop("continuity_token", None)
        session.setdefault("schema_version", 2)
        session["last_checkin_ts"] = int(time.time())
        session["updated_at"] = _now_iso()
        _write_cache(self.workspace, session, slot)

    def ensure_session(self, slot: str) -> dict[str, Any]:
        session = self.read_session(slot)
        sid = session.get("client_session_id")
        if isinstance(sid, str) and sid.strip():
            return {
                "status": "cached",
                "uuid": session.get("uuid", ""),
                "client_session_id": sid.strip(),
                "session": session,
            }
        result = run_onboard(
            server_url=self.server_url,
            agent_name=self.agent_name,
            model_type=self.model_type,
            workspace=self.workspace,
            slot=slot,
            force_new=False,
            auth_token=self.auth_token,
            timeout=self.timeout,
        )
        if result.get("status") != "ok":
            return result
        # run_onboard writes the cache; add schema/stamp fields for the sidecar
        # contract without persisting the transient continuity_token.
        session = self.read_session(slot)
        if session:
            session.setdefault("schema_version", 2)
            session["updated_at"] = _now_iso()
            _write_cache(self.workspace, session, slot)
        result["session"] = self.read_session(slot)
        return result

    def update_cache_from_identity_response(self, slot: str, parsed: dict[str, Any]) -> None:
        if not isinstance(parsed, dict):
            return
        uuid = parsed.get("uuid") or parsed.get("agent_uuid")
        sid = parsed.get("client_session_id")
        if not uuid and not sid:
            return
        existing = self.read_session(slot)
        payload: dict[str, Any] = {
            "server_url": self.server_url,
            "agent_name": self.agent_name,
            "slot": slot,
            "uuid": uuid or existing.get("uuid", ""),
            "agent_id": parsed.get("agent_id") or parsed.get("resolved_agent_id") or existing.get("agent_id", ""),
            "client_session_id": sid or existing.get("client_session_id", ""),
            "session_resolution_source": parsed.get("session_resolution_source") or existing.get("session_resolution_source", ""),
            "display_name": parsed.get("display_name") or existing.get("display_name", ""),
            "schema_version": 2,
        }
        self.write_session(slot, payload)

    def tool_call(self, body: dict[str, Any], headers: Any) -> tuple[int, dict[str, Any]]:
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return 400, {"success": False, "error": "missing tool name"}
        name = name.strip()
        arguments = body.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return 400, {"success": False, "error": "arguments must be an object"}
        arguments = dict(arguments)
        slot = self.slot_from(body, headers)

        lifecycle: dict[str, Any] = {"slot": slot, "injected_client_session_id": False}

        if name in START_TOOL_NAMES:
            arguments.setdefault("force_new", True)
            arguments.setdefault("name", self.agent_name)
            arguments.setdefault("model_type", self.model_type)
        elif name in INJECT_SUFFIXES and not _has_proof_field(arguments):
            ensured = self.ensure_session(slot)
            lifecycle["ensure_session"] = {k: v for k, v in ensured.items() if k != "session"}
            sid = ensured.get("client_session_id")
            if isinstance(sid, str) and sid.strip():
                arguments["client_session_id"] = sid.strip()
                lifecycle["injected_client_session_id"] = True

        upstream_payload = {"name": name, "arguments": arguments}
        raw, latency_ms, error = _post_json(
            f"{self.server_url}/v1/tools/call",
            upstream_payload,
            self.timeout,
            self.auth_token,
        )
        if error:
            return 502, {
                "success": False,
                "error": error,
                "sidecar": lifecycle | {"upstream_latency_ms": latency_ms},
            }

        parsed = unwrap_tool_response(raw)
        if name in IDENTITY_TOOL_NAMES:
            self.update_cache_from_identity_response(slot, parsed)
        if name in CHECKIN_TOOL_NAMES:
            self.stamp_session(slot)

        raw.setdefault("sidecar", {})
        if isinstance(raw["sidecar"], dict):
            raw["sidecar"].update(lifecycle | {"upstream_latency_ms": latency_ms})
        return 200, raw

    def turn_checkin(self, body: dict[str, Any], headers: Any) -> tuple[int, dict[str, Any]]:
        slot = self.slot_from(body, headers)
        ensured = self.ensure_session(slot)
        sid = ensured.get("client_session_id")
        if not isinstance(sid, str) or not sid.strip():
            return 502, {
                "success": False,
                "error": "sidecar could not establish client_session_id",
                "onboard": ensured,
            }
        session = self.read_session(slot)
        event = str(body.get("event") or "turn_stop")
        status = submit_checkin(
            event=event,
            response_text=str(body.get("response_text") or body.get("summary") or "Sidecar turn check-in"),
            complexity=float(body.get("complexity", 0.3)),
            confidence=float(body.get("confidence", 0.7)),
            client_session_id=sid.strip(),
            slot=slot,
            uuid=str(session.get("uuid", "")),
            server_url=self.server_url,
            plugin_version=_plugin_version(),
            epistemic_class=body.get("epistemic_class") or "agent_report",
        )
        if status == "sent":
            self.stamp_session(slot)
        return 200 if status in {"sent", "skip_kill_switch"} else 502, {
            "success": status in {"sent", "skip_kill_switch"},
            "status": status,
            "slot": slot,
            "client_session_id": sid.strip(),
            "uuid": session.get("uuid", ""),
        }

    def audit(self) -> dict[str, Any]:
        findings = audit_session_caches(self.workspace) + audit_checkin_log(self.log_path)
        return {
            "success": True,
            "workspace": str(self.workspace),
            "log": str(self.log_path),
            "errors": sum(1 for f in findings if f.severity == "error"),
            "warnings": sum(1 for f in findings if f.severity == "warning"),
            "findings": [f.as_dict() for f in findings],
        }


def make_handler(sidecar: IdentitySidecar) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "UNITARESIdentitySidecar/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep stdout clean for agents
            if os.environ.get("UNITARES_SIDECAR_ACCESS_LOG", "off").lower() in {"1", "on", "true"}:
                super().log_message(fmt, *args)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                _json_response(self, 200, {
                    "success": True,
                    "server_url": sidecar.server_url,
                    "workspace": str(sidecar.workspace),
                    "default_slot": sidecar.default_slot,
                })
                return
            if self.path == "/session":
                slot = sidecar.slot_from({}, self.headers)
                session = dict(sidecar.read_session(slot))
                session.pop("continuity_token", None)
                _json_response(self, 200, {"success": True, "slot": slot, "session": session})
                return
            if self.path == "/audit":
                _json_response(self, 200, sidecar.audit())
                return
            _json_response(self, 404, {"success": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            body = _read_request_json(self)
            if self.path == "/v1/tools/call":
                status, payload = sidecar.tool_call(body, self.headers)
                _json_response(self, status, payload)
                return
            if self.path in {"/turn/checkin", "/turn/stop"}:
                if self.path == "/turn/stop":
                    body.setdefault("event", "turn_stop")
                    body.setdefault("epistemic_class", "substrate_interpretation")
                status, payload = sidecar.turn_checkin(body, self.headers)
                _json_response(self, status, payload)
                return
            if self.path == "/session/start":
                slot = sidecar.slot_from(body, self.headers)
                result = run_onboard(
                    server_url=sidecar.server_url,
                    agent_name=str(body.get("agent_name") or sidecar.agent_name),
                    model_type=str(body.get("model_type") or sidecar.model_type),
                    workspace=sidecar.workspace,
                    slot=slot,
                    force_new=bool(body.get("ignore_lineage", False)),
                    auth_token=sidecar.auth_token,
                    timeout=sidecar.timeout,
                )
                status = 200 if result.get("status") == "ok" else 502
                _json_response(self, status, {"success": status == 200, "slot": slot, "result": result})
                return
            _json_response(self, 404, {"success": False, "error": "not found"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("UNITARES_SIDECAR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("UNITARES_SIDECAR_PORT", DEFAULT_PORT)))
    parser.add_argument("--server-url", default=os.environ.get("UNITARES_SERVER_URL", DEFAULT_SERVER_URL))
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--slot", default=os.environ.get("UNITARES_SIDECAR_SLOT", ""))
    parser.add_argument("--name", default=os.environ.get("UNITARES_AGENT_NAME", ""))
    parser.add_argument("--model-type", default=os.environ.get("UNITARES_MODEL_TYPE", "sidecar"))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("UNITARES_SIDECAR_TIMEOUT", DEFAULT_TIMEOUT)))
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    default_slot = args.slot.strip() or f"sidecar-{_workspace_hash(workspace)}"
    sidecar = IdentitySidecar(
        server_url=args.server_url,
        workspace=workspace,
        agent_name=args.name.strip() or workspace.name,
        model_type=args.model_type,
        default_slot=default_slot,
        timeout=args.timeout,
        auth_token=os.environ.get("UNITARES_HTTP_API_TOKEN") or None,
    )
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(sidecar))
    print(json.dumps({
        "success": True,
        "sidecar_url": f"http://{args.host}:{args.port}",
        "server_url": sidecar.server_url,
        "workspace": str(workspace),
        "default_slot": default_slot,
    }), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
