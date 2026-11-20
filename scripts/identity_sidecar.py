#!/usr/bin/env python3
"""Local UNITARES identity sidecar.

This is a thin REST sidecar for clients that do not have lifecycle hooks. It
does not implement governance policy and does not replace the UNITARES server.
It wraps the server's `/v1/tools/call` surface with local lifecycle help:

* lazy onboard when a slot has no cached `client_session_id`
* inject `client_session_id` into attribution-relevant governance calls
* force fresh posture for bare `onboard` / `start_session` calls
* keep a bound slot's identity on tool calls: refuse foreign or missing
  proof, mints and identity tools sent through use_tool before the upstream
  call, and persist only successful responses for the bound uuid (a binding
  marked `prior_process` is never used as proof). `POST /session/start` is
  a separate re-onboard route and is not covered by this rule.
* stamp the slot cache after successful check-ins
* expose the local identity-contract audit
* proxy minimal JSON-RPC MCP `tools/call` requests through `/mcp/`

Phase 1 intentionally stays dependency-free: REST plus minimal JSON-RPC MCP.
A full streamable-MCP/SSE proxy can reuse the same core behavior once that
transport boundary is worth owning directly.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audit_identity_contract import audit_checkin_log, audit_session_caches, parse_since  # noqa: E402
from checkin import _plugin_version, submit_checkin  # noqa: E402
from governance_call_inject import (  # noqa: E402
    GATEWAY_SUFFIXES,
    INJECT_SUFFIXES,
    PROOF_FIELDS,
    SLOT_BOUND_PROOF_FIELDS,
    _truthy,
)
from governance_tool_name import parse_mcp_tool_name  # noqa: E402
from _http_auth import authorization_safe_urlopen, read_limited_response  # noqa: E402
from _redact import sanitize_model_visible_payload  # noqa: E402
from onboard_helper import (  # noqa: E402
    CacheGenerationToken,
    DEFAULT_SERVER_URL,
    _cache_path,
    _read_cache,
    _read_cache_snapshot,
    _update_cache,
    _update_cache_transaction,
    _write_cache,
    run_onboard,
    unwrap_tool_response,
)
from _session_cache_io import session_cache_lock  # noqa: E402
from _session_lookup import load_authoritative_session_for_hook  # noqa: E402


IDENTITY_TOOL_NAMES = {"onboard", "start_session", "identity", "bind_session"}
START_TOOL_NAMES = {"onboard", "start_session"}
CHECKIN_TOOL_NAMES = {"process_agent_update", "sync_state"}
DEFAULT_PORT = 8768
DEFAULT_TIMEOUT = 20.0
MAX_REQUEST_BYTES = 1024 * 1024
CLIENT_SOCKET_TIMEOUT = 5.0
MAX_REQUEST_WORKERS = 32


class RequestBodyError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


#: use_tool layers the sidecar unwraps before it refuses the call outright.
MAX_GATEWAY_DEPTH = 4


def _tool_key(value: object) -> str:
    """Normalized tool name: trimmed, casefolded, MCP prefix removed."""
    if not isinstance(value, str):
        return ""
    stripped = value.strip()
    parsed = parse_mcp_tool_name(stripped)
    return parsed[1] if parsed else stripped.casefold()


class ForeignSlotProofError(ValueError):
    """A caller tried to route a slot-bound sidecar through another identity."""


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threaded loopback server with a hard cap on active handlers."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, *args: Any, max_workers: int = MAX_REQUEST_WORKERS, **kwargs: Any) -> None:
        self._request_slots = threading.BoundedSemaphore(max(1, max_workers))
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            try:
                try:
                    request.sendall(
                        b"HTTP/1.1 503 Service Unavailable\r\n"
                        b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                    )
                except OSError:
                    pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_hash(workspace: Path) -> str:
    return hashlib.md5(str(workspace).encode("utf-8")).hexdigest()[:8]


def _json_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    payload: Any,
    *,
    sanitize: bool = True,
) -> None:
    visible = sanitize_model_visible_payload(payload) if sanitize else payload
    body = (json.dumps(visible, indent=2, sort_keys=True) + "\n").encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_request_json(handler: BaseHTTPRequestHandler) -> Any:
    raw_len = handler.headers.get("Content-Length")
    if raw_len is None:
        raise RequestBodyError(411, "Content-Length required")
    try:
        length = int(raw_len)
    except ValueError:
        raise RequestBodyError(400, "invalid Content-Length") from None
    if length < 0:
        raise RequestBodyError(400, "invalid Content-Length")
    if length > MAX_REQUEST_BYTES:
        raise RequestBodyError(413, "request body too large")
    try:
        raw = handler.rfile.read(length) if length else b""
    except (TimeoutError, socket.timeout):
        raise RequestBodyError(408, "request body timed out") from None
    if len(raw) != length:
        raise RequestBodyError(400, "incomplete request body")
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RequestBodyError(400, "request body must be valid UTF-8 JSON") from None
    return data


def _handler_base_url(handler: BaseHTTPRequestHandler) -> str:
    forwarded_proto = handler.headers.get("X-Forwarded-Proto", "")
    scheme = (forwarded_proto.split(",")[0].strip() if forwarded_proto else "") or "http"
    forwarded_host = handler.headers.get("X-Forwarded-Host", "")
    host = (forwarded_host.split(",")[0].strip() if forwarded_host else "") or handler.headers.get("Host")
    if not host:
        address = handler.server.server_address
        host = f"{address[0]}:{address[1]}"
    return f"{scheme}://{host}".rstrip("/")


def _post_json(url: str, payload: dict[str, Any], timeout: float, token: str | None) -> tuple[dict[str, Any], int, str | None]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    started = time.monotonic()
    try:
        with authorization_safe_urlopen(req, timeout=timeout) as resp:
            raw = read_limited_response(resp).decode("utf-8")
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
        return {}, int((time.monotonic() - started) * 1000), str(getattr(exc, "reason", exc))
    try:
        data = json.loads(raw)
    except Exception as exc:
        return {}, int((time.monotonic() - started) * 1000), f"invalid json: {exc}"
    return data if isinstance(data, dict) else {}, int((time.monotonic() - started) * 1000), None


def _post_json_any(
    url: str,
    payload: Any,
    timeout: float,
    token: str | None,
) -> tuple[Any, int, int, str | None]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    started = time.monotonic()
    try:
        with authorization_safe_urlopen(req, timeout=timeout) as resp:
            raw = read_limited_response(resp).decode("utf-8")
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        raw = read_limited_response(exc).decode("utf-8", errors="replace")
        status = int(exc.code)
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
        return None, 502, int((time.monotonic() - started) * 1000), str(getattr(exc, "reason", exc))
    try:
        data = json.loads(raw) if raw else {}
    except Exception as exc:
        return None, status, int((time.monotonic() - started) * 1000), f"invalid json: {exc}"
    return data, status, int((time.monotonic() - started) * 1000), None


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
        capability_token: str | None = None,
        startup_secret: str | None = None,
        log_path: Path | None = None,
        audit_log_tail: int | None = 200,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.workspace = workspace
        self.agent_name = agent_name
        self.model_type = model_type
        self.default_slot = default_slot
        self.timeout = timeout
        self.auth_token = auth_token
        self.capability_token = capability_token or secrets.token_urlsafe(32)
        self.startup_secret = startup_secret
        self.log_path = log_path or Path(os.environ.get("UNITARES_CHECKIN_LOG", "~/.unitares/checkins.log")).expanduser()
        self.audit_log_tail = audit_log_tail

    def slot_from(self, body: dict[str, Any], headers: Any) -> str:
        # The capability is minted for exactly one server-assigned slot.
        # Caller-controlled headers and JSON can no longer select another
        # session cache or borrow its governance authority.
        del body, headers
        return self.default_slot or f"sidecar-{_workspace_hash(self.workspace)}"

    def authorized(self, headers: Any) -> bool:
        raw = str(headers.get("Authorization") or "") if headers else ""
        prefix = "Bearer "
        if not raw.startswith(prefix):
            return False
        supplied = raw[len(prefix):]
        return bool(supplied) and hmac.compare_digest(supplied, self.capability_token)

    def startup_proof(self, challenge: str) -> str | None:
        if not self.startup_secret:
            return None
        return hmac.new(
            self.startup_secret.encode("utf-8"),
            challenge.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def read_session(self, slot: str) -> dict[str, Any]:
        return load_authoritative_session_for_hook(
            self.workspace,
            json.dumps({"session_id": slot}),
        )

    def write_session(self, slot: str, payload: dict[str, Any]) -> None:
        clean = {k: v for k, v in payload.items() if k != "continuity_token"}
        clean.setdefault("schema_version", 2)
        clean["slot"] = slot
        clean["updated_at"] = _now_iso()
        _write_cache(self.workspace, clean, slot)

    def stamp_session(self, slot: str) -> None:
        def stamp(session: dict[str, Any]) -> dict[str, Any] | None:
            if not session:
                return None
            session.pop("continuity_token", None)
            session.setdefault("schema_version", 2)
            session["last_checkin_ts"] = int(time.time())
            session["updated_at"] = _now_iso()
            return session

        _update_cache(self.workspace, stamp, slot)

    def ensure_session(self, slot: str) -> dict[str, Any]:
        # The HTTP server is threaded. A distinct stable lock prevents two
        # processes or threads from both minting an upstream identity for the
        # same empty slot while leaving ordinary cache/clear transactions free.
        onboard_claim = _cache_path(self.workspace, slot).with_name(
            f"{_cache_path(self.workspace, slot).name}.onboard"
        )
        with ExitStack() as stack:
            try:
                stack.enter_context(session_cache_lock(onboard_claim))
            except TimeoutError:
                return {
                    "status": "onboard_in_progress",
                    "error": "another sidecar request is onboarding this slot",
                }
            session = self.read_session(slot)
            if session.get("prior_process") is True:
                # The binding belongs to an earlier process (a resume marked
                # it inherited). It is no proof for this process's calls.
                return {
                    "status": "inherited_binding",
                    "error": (
                        "protected HOME session binding belongs to a prior "
                        "process; clear it explicitly before onboarding a replacement"
                    ),
                }
            sid = session.get("client_session_id")
            if isinstance(sid, str) and sid.strip():
                # The workspace cache is a compatibility mirror only.  A
                # protected HOME binding remains authoritative and repairs
                # a missing or attacker-modified workspace copy.
                workspace_session = _read_cache(self.workspace, slot)
                if workspace_session != session:
                    try:
                        _write_cache(self.workspace, session, slot)
                    except TimeoutError:
                        return {
                            "status": "cache_lock_timeout",
                            "error": "session cache lock timed out while repairing workspace mirror",
                        }
                return {
                    "status": "cached",
                    "uuid": session.get("uuid", ""),
                    "client_session_id": sid.strip(),
                    "session": session,
                }
            protected_path = _cache_path(Path.home(), slot)
            if protected_path.exists() or protected_path.is_symlink():
                return {
                    "status": "invalid_protected_binding",
                    "error": (
                        "protected HOME session binding is invalid; "
                        "clear it explicitly before onboarding a replacement"
                    ),
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
            # run_onboard writes the cache; add schema/stamp fields for the
            # sidecar contract without persisting the transient
            # continuity_token. Keep this post-onboard stamp inside
            # onboard_claim so a concurrent ensure_session caller cannot
            # observe the un-stamped cache or the transient
            # mirror_valid=False window during _update_cache.
            def stamp_schema(session: dict[str, Any]) -> dict[str, Any] | None:
                if not session:
                    return None
                session.setdefault("schema_version", 2)
                session["slot"] = slot
                session["updated_at"] = _now_iso()
                return session

            try:
                result["session"] = _update_cache(self.workspace, stamp_schema, slot)
            except TimeoutError:
                return {
                    "status": "cache_lock_timeout",
                    "error": "session cache lock timed out after onboarding",
                }
            current_sid = str(result["session"].get("client_session_id") or "").strip()
            if not current_sid or current_sid != str(result.get("client_session_id") or "").strip():
                return {
                    "status": "onboard_superseded",
                    "error": "session cache changed after onboarding; newer local state preserved",
                }
            return result

    def update_cache_from_identity_response(
        self,
        slot: str,
        parsed: dict[str, Any],
        *,
        expected_generation: CacheGenerationToken | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Persist an identity response for this slot, or say why not.

        Owner rule: a slot that already names an identity keeps it. Only a
        response for that same uuid (a resume refreshing its session id) is
        merged; the sidecar has no lineage support, so any other uuid is
        refused whatever the caller declared. Failed responses, responses
        without a uuid and declared subagent mints are never persisted.
        """
        if not isinstance(parsed, dict):
            return "no_identity"
        canonical = parsed.get("raw_governance")
        if not isinstance(canonical, dict):
            canonical = {}
        if parsed.get("success") is False or canonical.get("success") is False:
            return "refused_failed_response"
        if parsed.get("bound") is False or canonical.get("bound") is False:
            # bind_session resolved the agent but declined the bind.
            return "refused_failed_response"
        uuid = (
            parsed.get("uuid")
            or parsed.get("agent_uuid")
            or canonical.get("uuid")
            or canonical.get("agent_uuid")
        )
        sid = parsed.get("client_session_id") or canonical.get("client_session_id")
        if not uuid and not sid:
            return "no_identity"
        if not isinstance(uuid, str) or not uuid.strip():
            return "refused_no_uuid"
        uuid = uuid.strip()
        spawn_reason = str((arguments or {}).get("spawn_reason") or "").strip().lower()
        if spawn_reason == "subagent":
            return "refused_subagent"
        decision = "superseded"

        def merge_identity(existing: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal decision
            bound_uuid = str(existing.get("uuid") or "").strip()
            if bound_uuid and bound_uuid != uuid:
                decision = "refused_owner_mismatch"
                return None
            if not (isinstance(sid, str) and sid.strip()) and not str(
                existing.get("client_session_id") or ""
            ).strip():
                # A bind_session reply names the agent but not the session;
                # a binding without one would be unusable and block the slot.
                decision = "refused_no_session_id"
                return None
            decision = "updated"
            payload: dict[str, Any] = {
                "server_url": self.server_url,
                "agent_name": self.agent_name,
                "slot": slot,
                "uuid": uuid or existing.get("uuid", ""),
                "agent_id": parsed.get("agent_id")
                or parsed.get("resolved_agent_id")
                or canonical.get("agent_id")
                or canonical.get("resolved_agent_id")
                or existing.get("agent_id", ""),
                "client_session_id": sid or existing.get("client_session_id", ""),
                "session_resolution_source": parsed.get("session_resolution_source")
                or canonical.get("session_resolution_source")
                or existing.get("session_resolution_source", ""),
                "display_name": parsed.get("display_name")
                or canonical.get("display_name")
                or existing.get("display_name", ""),
                "schema_version": 2,
                "updated_at": _now_iso(),
            }
            # Same identity, so who minted it and which process owns it stay.
            for key in ("onboard_origin", "onboard_origin_basis", "prior_process"):
                if key in existing:
                    payload[key] = existing[key]
            return payload

        _authoritative, committed = _update_cache_transaction(
            self.workspace,
            merge_identity,
            slot,
            expected_generation=expected_generation,
        )
        if decision == "updated" and not committed:
            return "superseded"
        return decision

    def refuse_identity_call_over_bound_slot(
        self,
        key: str,
        arguments: dict[str, Any],
        slot: str,
    ) -> None:
        """Refuse, before any upstream call, an identity call that would
        bind or mint another identity into a slot that already names one.

        On a bound slot an identity call must prove the bound identity, and
        anything the server would turn into a mint is refused: foreign slot
        proof, any force_new mint, a declared parent_agent_id (the server
        promotes it to a fresh mint) and an onboard, start_session or
        identity without session, token, uuid or agent_id proof (a name
        alone is not proof; the server mints for it). Unlike the
        pre-governance-call hook, declared lineage does not exempt a mint:
        the sidecar has no lineage support, so the owner rule would never
        cache the new identity and the caller never sees its session id. A
        declared subagent mint is refused even into an empty slot for the
        same reason. A binding marked prior_process belongs to an earlier
        process and a protected binding the sidecar cannot read is not
        empty; both refuse every identity call until the slot is cleared.
        The response owner rule stays the authority; this keeps the call
        from reaching the server under the sidecar's credential and minting
        an identity nobody can use.
        """
        if str(arguments.get("spawn_reason") or "").strip().lower() == "subagent":
            raise ForeignSlotProofError(
                "sidecar refuses spawn_reason=subagent: its slot never holds a "
                "subagent identity, so the new session would be unusable. Run the "
                "subagent through its own sidecar slot."
            )
        binding = self.read_session(slot)
        if not binding:
            protected_path = _cache_path(Path.home(), slot)
            if protected_path.exists() or protected_path.is_symlink():
                raise ForeignSlotProofError(
                    "sidecar slot's protected HOME session binding is invalid; "
                    "clear it explicitly before binding an identity"
                )
        bound_uuid = str(binding.get("uuid") or "").strip()
        if not bound_uuid:
            return
        switch_hint = (
            "To switch identity, clear the slot (session_cache.py clear) or "
            "run a separate sidecar on another slot."
        )
        if binding.get("prior_process") is True:
            raise ForeignSlotProofError(
                f"sidecar slot binding {bound_uuid} belongs to a prior process "
                "and is no proof for this one. " + switch_hint
            )

        def provided(field: str) -> bool:
            value = arguments.get(field)
            return value is not None and not (isinstance(value, str) and not value.strip())

        mismatched = []
        for field in (*SLOT_BOUND_PROOF_FIELDS, "agent_uuid"):
            if not provided(field):
                continue
            value = arguments.get(field)
            expected = bound_uuid if field == "agent_uuid" else binding.get(field)
            if (
                not isinstance(value, str)
                or not isinstance(expected, str)
                or value.strip() != expected.strip()
            ):
                mismatched.append(field)
        agent_id = arguments.get("agent_id")
        # bind_session resolves the agent from client_session_id, checked
        # above, and the server compares agent_id against every alias of that
        # agent. Elsewhere agent_id is identity proof; it may name the bound
        # identity by its label or by its uuid.
        pinned_by_session = key == "bind_session" and provided("client_session_id")
        if provided("agent_id") and not pinned_by_session:
            accepted = {bound_uuid, str(binding.get("agent_id") or "").strip()} - {""}
            if not isinstance(agent_id, str) or agent_id.strip() not in accepted:
                mismatched.append("agent_id")
        if mismatched:
            raise ForeignSlotProofError(
                "sidecar identity proof does not match its configured slot binding: "
                + ", ".join(sorted(mismatched))
            )
        if key == "bind_session":
            # bind_session never mints; without client_session_id the server
            # refuses it.
            return
        if _truthy(arguments.get("force_new")):
            raise ForeignSlotProofError(
                f"sidecar slot is already bound to agent_uuid {bound_uuid}; "
                "force_new would mint a second identity the sidecar cannot hold. "
                + switch_hint
            )
        if provided("parent_agent_id"):
            raise ForeignSlotProofError(
                f"sidecar slot is already bound to agent_uuid {bound_uuid}; "
                "a declared parent_agent_id makes the server mint a new identity "
                "the sidecar cannot hold. " + switch_hint
            )
        if not any(provided(field) for field in PROOF_FIELDS):
            raise ForeignSlotProofError(
                f"sidecar slot is already bound to agent_uuid {bound_uuid}; "
                f"a {key} call without proof of that identity would mint or "
                "resolve another one. Pass the bound client_session_id."
            )

    def refuse_identity_tool_through_gateway(self, key: str, arguments: dict[str, Any]) -> None:
        """Refuse an identity tool dispatched through use_tool.

        The gateway would carry it upstream under the sidecar's credential
        while its reply is processed as a use_tool result, so neither the
        owner rule above nor the response owner rule would apply.
        """
        layer_name, layer = key, arguments
        for _ in range(MAX_GATEWAY_DEPTH + 1):
            if layer_name not in GATEWAY_SUFFIXES:
                return
            target = _tool_key(layer.get("tool_name"))
            if target in IDENTITY_TOOL_NAMES:
                raise ForeignSlotProofError(
                    f"sidecar refuses {target} through use_tool: call it directly "
                    "so the slot owner rule applies"
                )
            nested = layer.get("arguments")
            if not isinstance(nested, dict):
                return
            layer_name, layer = target, nested

    def prepare_tool_arguments(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        slot: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        arguments = dict(arguments)
        lifecycle: dict[str, Any] = {"slot": slot, "session_proof_injected": False}

        key = _tool_key(name)
        if key in IDENTITY_TOOL_NAMES:
            if key in START_TOOL_NAMES:
                arguments.setdefault("force_new", True)
                arguments.setdefault("name", self.agent_name)
                arguments.setdefault("model_type", self.model_type)
            self.refuse_identity_call_over_bound_slot(key, arguments, slot)
        elif key in INJECT_SUFFIXES:
            self.refuse_identity_tool_through_gateway(key, arguments)
            ensured = self.ensure_session(slot)
            lifecycle["ensure_session"] = {k: v for k, v in ensured.items() if k != "session"}
            sid = ensured.get("client_session_id")
            authority = ensured.get("session")
            if not isinstance(authority, dict):
                authority = ensured
            # Top level: every proof field. Inside use_tool layers only the
            # slot-bound proofs, as the PreToolUse hook does: agent_id and
            # agent_uuid there commonly name the agent a tool should look up.
            proof_sources = [("", arguments, PROOF_FIELDS)]
            gateway_name = _tool_key(name)
            gateway_arguments = arguments
            gateway_prefix = ""
            depth = 0
            while gateway_name in GATEWAY_SUFFIXES:
                nested = gateway_arguments.get("arguments")
                if nested is None:
                    break
                if not isinstance(nested, dict):
                    raise ForeignSlotProofError(
                        "sidecar refuses use_tool arguments that are not an object: "
                        + gateway_prefix + "arguments"
                    )
                depth += 1
                if depth > MAX_GATEWAY_DEPTH:
                    raise ForeignSlotProofError(
                        f"sidecar refuses use_tool nested more than {MAX_GATEWAY_DEPTH} deep"
                    )
                gateway_prefix += "arguments."
                proof_sources.append((gateway_prefix, nested, SLOT_BOUND_PROOF_FIELDS))
                gateway_name = _tool_key(gateway_arguments.get("tool_name"))
                gateway_arguments = nested
            provided = [
                (prefix, field, value)
                for prefix, source, fields in proof_sources
                for field in fields
                if (value := source.get(field)) is not None
                and (not isinstance(value, str) or value.strip())
            ]
            mismatched = [
                f"{prefix}{field}"
                for prefix, field, value in provided
                if not isinstance(value, str)
                or not isinstance(authority.get(field), str)
                or value.strip() != str(authority.get(field)).strip()
            ]
            if mismatched:
                raise ForeignSlotProofError(
                    "sidecar proof does not match its configured slot binding: "
                    + ", ".join(sorted(mismatched))
                )
            top_level_provided = any(not prefix for prefix, _field, _value in provided)
            if isinstance(sid, str) and sid.strip() and not top_level_provided:
                arguments["client_session_id"] = sid.strip()
                lifecycle["session_proof_injected"] = True
        return arguments, lifecycle

    def process_tool_response(
        self,
        *,
        name: str,
        slot: str,
        raw: Any,
        identity_generation: CacheGenerationToken | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> str | None:
        if not isinstance(raw, dict):
            return None
        parsed = unwrap_tool_response(raw)
        decision = None
        if _tool_key(name) in IDENTITY_TOOL_NAMES:
            result = raw.get("result")
            if isinstance(result, dict) and result.get("isError") is True:
                decision = "refused_failed_response"
            else:
                decision = self.update_cache_from_identity_response(
                    slot,
                    parsed,
                    expected_generation=identity_generation,
                    arguments=arguments,
                )
        if name in CHECKIN_TOOL_NAMES:
            self.stamp_session(slot)
        return decision

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
        slot = self.slot_from(body, headers)
        try:
            arguments, lifecycle = self.prepare_tool_arguments(
                name=name,
                arguments=arguments,
                slot=slot,
            )
        except ForeignSlotProofError as exc:
            return 409, {"success": False, "error": str(exc)}
        identity_generation = None
        if _tool_key(name) in IDENTITY_TOOL_NAMES:
            _session, identity_generation = _read_cache_snapshot(self.workspace, slot)

        upstream_payload = {"name": name, "arguments": arguments}
        raw, latency_ms, error = _post_json(
            f"{self.server_url}/v1/tools/call",
            upstream_payload,
            self.timeout,
            self.auth_token,
        )
        if error:
            return 502, sanitize_model_visible_payload({
                "success": False,
                "error": error,
                "sidecar": lifecycle | {"upstream_latency_ms": latency_ms},
            })

        cache_decision = self.process_tool_response(
            name=name,
            slot=slot,
            raw=raw,
            identity_generation=identity_generation,
            arguments=arguments,
        )
        if cache_decision is not None:
            lifecycle["identity_cache_update"] = cache_decision

        raw.setdefault("sidecar", {})
        if isinstance(raw["sidecar"], dict):
            raw["sidecar"].update(lifecycle | {"upstream_latency_ms": latency_ms})
        return 200, sanitize_model_visible_payload(raw)

    def mcp_proxy(self, body: Any, headers: Any, path: str) -> tuple[int, Any]:
        """Proxy a minimal JSON-RPC MCP request, intercepting tools/call.

        This intentionally covers JSON request/response MCP traffic. It is not
        an SSE stream implementation; callers needing full streamable transport
        should continue to use the upstream MCP endpoint directly until that
        path is explicitly implemented here.
        """
        slot = self.slot_from(body if isinstance(body, dict) else {}, headers)
        requests = body if isinstance(body, list) else [body]
        if not all(isinstance(item, dict) for item in requests):
            return 400, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "invalid JSON-RPC request"},
            }

        metadata_by_id: dict[Any, tuple[str, dict[str, Any], int | None]] = {}
        mutated: list[dict[str, Any]] = []
        for item in requests:
            req = dict(item)
            if req.get("method") == "tools/call":
                params = req.get("params")
                if isinstance(params, dict):
                    name = params.get("name")
                    arguments = params.get("arguments")
                    if isinstance(name, str):
                        try:
                            prepared, lifecycle = self.prepare_tool_arguments(
                                name=name,
                                arguments=arguments if isinstance(arguments, dict) else {},
                                slot=slot,
                            )
                        except ForeignSlotProofError as exc:
                            return 409, {
                                "jsonrpc": "2.0",
                                "id": req.get("id"),
                                "error": {"code": -32001, "message": str(exc)},
                            }
                        new_params = dict(params)
                        new_params["arguments"] = prepared
                        req["params"] = new_params
                        identity_generation = None
                        if _tool_key(name) in IDENTITY_TOOL_NAMES:
                            _session, identity_generation = _read_cache_snapshot(
                                self.workspace,
                                slot,
                            )
                        metadata_by_id[req.get("id")] = (
                            name,
                            lifecycle,
                            identity_generation,
                            prepared,
                        )
            mutated.append(req)

        outbound: Any = mutated if isinstance(body, list) else mutated[0]
        upstream_path = path if path.startswith("/") else f"/{path}"
        upstream, status, _latency_ms, error = _post_json_any(
            f"{self.server_url}{upstream_path}",
            outbound,
            self.timeout,
            self.auth_token,
        )
        if error:
            first_id = requests[0].get("id") if requests and isinstance(requests[0], dict) else None
            return 502, sanitize_model_visible_payload({
                "jsonrpc": "2.0",
                "id": first_id,
                "error": {"code": -32000, "message": error},
            })

        responses = upstream if isinstance(upstream, list) else [upstream]
        for response in responses:
            if not isinstance(response, dict):
                continue
            if "error" in response:
                continue
            meta = metadata_by_id.get(response.get("id"))
            if not meta:
                continue
            name, _lifecycle, identity_generation, prepared = meta
            self.process_tool_response(
                name=name,
                slot=slot,
                raw=response,
                identity_generation=identity_generation,
                arguments=prepared,
            )
        return status, sanitize_model_visible_payload(upstream)

    def turn_checkin(self, body: dict[str, Any], headers: Any) -> tuple[int, dict[str, Any]]:
        slot = self.slot_from(body, headers)
        ensured = self.ensure_session(slot)
        sid = ensured.get("client_session_id")
        if not isinstance(sid, str) or not sid.strip():
            return 502, sanitize_model_visible_payload({
                "success": False,
                "error": "sidecar could not establish client_session_id",
                "onboard": ensured,
            })
        session = self.read_session(slot)
        event = str(body.get("event") or "turn_stop")
        status = submit_checkin(
            event=event,
            response_text=str(body.get("response_text") or body.get("summary") or "Sidecar turn check-in"),
            complexity=float(body.get("complexity", 0.3)),
            # Pass through only what the caller actually stated. Defaulting a
            # missing confidence mints a tactical prediction the caller never
            # made; None omits the field and mints nothing.
            confidence=(
                float(body["confidence"])
                if body.get("confidence") is not None
                else None
            ),
            client_session_id=sid.strip(),
            slot=slot,
            uuid=str(session.get("uuid", "")),
            server_url=self.server_url,
            plugin_version=_plugin_version(),
            epistemic_class=body.get("epistemic_class") or "agent_report",
        )
        if status == "sent":
            self.stamp_session(slot)
        return 200 if status in {"sent", "skip_kill_switch"} else 502, sanitize_model_visible_payload({
            "success": status in {"sent", "skip_kill_switch"},
            "status": status,
            "slot": slot,
            "session_bound": True,
            "client_session_id": sid.strip(),
            "uuid": session.get("uuid", ""),
        })

    def audit(self, *, log_tail: int | None = None, since: str | None = None) -> dict[str, Any]:
        since_dt = parse_since(since)
        tail = self.audit_log_tail if log_tail is None else log_tail
        findings = audit_session_caches(self.workspace) + audit_checkin_log(
            self.log_path,
            log_tail=tail,
            since=since_dt,
        )
        return {
            "success": True,
            "workspace": str(self.workspace),
            "log": str(self.log_path),
            "log_tail": tail,
            "since": since_dt.isoformat() if since_dt else None,
            "errors": sum(1 for f in findings if f.severity == "error"),
            "warnings": sum(1 for f in findings if f.severity == "warning"),
            "findings": [f.as_dict() for f in findings],
        }

    def client_config(self, *, sidecar_url: str, slot: str) -> dict[str, Any]:
        base = sidecar_url.rstrip("/")
        slot = slot.strip() or self.default_slot
        mcp_url = f"{base}/mcp/"
        return {
            "success": True,
            "sidecar_url": base,
            "server_url": self.server_url,
            "workspace": str(self.workspace),
            "default_slot": self.default_slot,
            "slot": slot,
            "transport": {
                "mcp": {
                    "mode": "jsonrpc-http",
                    "url": mcp_url,
                    "slot_header": "X-UNITARES-Slot",
                    "intercepts": ["tools/call"],
                    "passes_through": ["tools/list", "initialize", "notifications/*"],
                },
                "rest_tool_call": f"{base}/v1/tools/call",
                "turn_checkin": f"{base}/turn/checkin",
                "turn_stop": f"{base}/turn/stop",
                "session": f"{base}/session",
                "audit": f"{base}/audit?log_tail=200",
            },
            "mcp_config": {
                "mcpServers": {
                    "unitares-governance-sidecar": {
                        "type": "url",
                        "url": mcp_url,
                        "headers": {
                            "Authorization": f"Bearer {self.capability_token}",
                            "X-UNITARES-Slot": slot,
                        },
                    }
                }
            },
            "environment": {
                "UNITARES_SIDECAR_URL": base,
                "UNITARES_SIDECAR_SLOT": slot,
                "UNITARES_SIDECAR_CAPABILITY": self.capability_token,
                "UNITARES_SERVER_URL": self.server_url,
            },
            "notes": [
                "Use this sidecar MCP URL only for clients that send JSON request/response MCP over HTTP.",
                "Clients requiring streamable HTTP or SSE should use the upstream governance MCP endpoint until sidecar support is added.",
                "The sidecar stores slot-scoped client_session_id continuity and never persists continuity_token.",
            ],
        }


def make_handler(sidecar: IdentitySidecar) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "UNITARESIdentitySidecar/0.1"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(CLIENT_SOCKET_TIMEOUT)

        def log_message(self, fmt: str, *args: Any) -> None:  # keep stdout clean for agents
            if os.environ.get("UNITARES_SIDECAR_ACCESS_LOG", "off").lower() in {"1", "on", "true"}:
                super().log_message(fmt, *args)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            path = parsed.path
            if path == "/startup-proof":
                challenge = (query.get("challenge") or [""])[0]
                proof = sidecar.startup_proof(challenge)
                if not challenge or proof is None:
                    _json_response(self, 404, {"success": False, "error": "not found"})
                else:
                    _json_response(self, 200, {"success": True, "proof": proof})
                return
            if not sidecar.authorized(self.headers):
                _json_response(self, 401, {"success": False, "error": "unauthorized"})
                return
            if path == "/health":
                _json_response(self, 200, {
                    "success": True,
                    "capability_id": hashlib.sha256(
                        sidecar.capability_token.encode("utf-8")
                    ).hexdigest(),
                    "server_url": sidecar.server_url,
                    "workspace": str(sidecar.workspace),
                    "default_slot": sidecar.default_slot,
                    "client_config": f"{_handler_base_url(self)}/client-config",
                    "mcp": f"{_handler_base_url(self)}/mcp/",
                })
                return
            if path in {"/client-config", "/config"}:
                requested_slot = (query.get("slot") or [""])[0]
                body = {"slot": requested_slot} if requested_slot else {}
                slot = sidecar.slot_from(body, self.headers)
                _json_response(
                    self,
                    200,
                    sidecar.client_config(sidecar_url=_handler_base_url(self), slot=slot),
                    sanitize=False,
                )
                return
            if path == "/session":
                slot = sidecar.slot_from({}, self.headers)
                session = dict(sidecar.read_session(slot))
                session.pop("continuity_token", None)
                _json_response(self, 200, {"success": True, "slot": slot, "session": session})
                return
            if path == "/audit":
                raw_tail = (query.get("log_tail") or [None])[0]
                try:
                    log_tail = int(raw_tail) if raw_tail else None
                    _json_response(
                        self,
                        200,
                        sidecar.audit(
                            log_tail=log_tail,
                            since=(query.get("since") or [None])[0],
                        ),
                    )
                except ValueError as exc:
                    _json_response(self, 400, {"success": False, "error": str(exc)})
                return
            _json_response(self, 404, {"success": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not sidecar.authorized(self.headers):
                _json_response(self, 401, {"success": False, "error": "unauthorized"})
                return
            try:
                body = _read_request_json(self)
            except RequestBodyError as exc:
                _json_response(self, exc.status, {"success": False, "error": str(exc)})
                return
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path not in {"/mcp", "/mcp/"} and not isinstance(body, dict):
                _json_response(self, 400, {"success": False, "error": "JSON object required"})
                return
            if path == "/v1/tools/call":
                status, payload = sidecar.tool_call(body, self.headers)
                _json_response(self, status, payload)
                return
            if path in {"/mcp", "/mcp/"}:
                status, payload = sidecar.mcp_proxy(body, self.headers, path or "/mcp/")
                _json_response(self, status, payload)
                return
            if path in {"/turn/checkin", "/turn/stop"}:
                if path == "/turn/stop":
                    body.setdefault("event", "turn_stop")
                    body.setdefault("epistemic_class", "substrate_interpretation")
                status, payload = sidecar.turn_checkin(body, self.headers)
                _json_response(self, status, payload)
                return
            if path == "/session/start":
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
    parser.add_argument("--audit-log-tail", type=int, default=int(os.environ.get("UNITARES_SIDECAR_AUDIT_LOG_TAIL", "200")),
                        help="default number of check-in log lines included by GET /audit")
    args = parser.parse_args(argv)

    raw_host = args.host.strip()
    try:
        address = ipaddress.ip_address(raw_host.strip("[]"))
    except ValueError:
        if raw_host.lower() != "localhost":
            parser.error("--host must be localhost or a loopback IP address")
    else:
        if not address.is_loopback:
            parser.error("--host must be localhost or a loopback IP address")

    workspace = Path(args.workspace).expanduser().resolve()
    default_slot = args.slot.strip() or f"sidecar-{_workspace_hash(workspace)}"
    capability_token = (os.environ.get("UNITARES_SIDECAR_CAPABILITY") or "").strip()
    if not capability_token:
        parser.error("UNITARES_SIDECAR_CAPABILITY is required")
    sidecar = IdentitySidecar(
        server_url=args.server_url,
        workspace=workspace,
        agent_name=args.name.strip() or workspace.name,
        model_type=args.model_type,
        default_slot=default_slot,
        timeout=args.timeout,
        auth_token=os.environ.get("UNITARES_HTTP_API_TOKEN") or None,
        capability_token=capability_token,
        startup_secret=(
            os.environ.get("UNITARES_SIDECAR_STARTUP_SECRET") or ""
        ).strip() or None,
        audit_log_tail=args.audit_log_tail,
    )
    httpd = BoundedThreadingHTTPServer((args.host, args.port), make_handler(sidecar))
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
