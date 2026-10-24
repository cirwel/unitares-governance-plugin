"""Tests for governance REST authentication headers."""

from __future__ import annotations

import ast
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _http_auth  # noqa: E402
from _http_auth import (  # noqa: E402
    _AuthorizationSafeRedirectHandler,
    InsecureBearerTransportError,
    ResponseTooLargeError,
    authorization_safe_urlopen,
    governance_json_headers,
    read_limited_response,
)


@pytest.mark.parametrize("url", ["http://192.0.2.1/v1", "http://example.test/v1"])
def test_bearer_request_rejects_non_loopback_cleartext_before_socket(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    request = urllib.request.Request(url, headers={"Authorization": "Bearer secret"})

    def unexpected_open(*_args, **_kwargs):
        pytest.fail("unsafe bearer request reached the network opener")

    monkeypatch.setattr("_http_auth._AUTHORIZATION_SAFE_OPENER.open", unexpected_open)
    with pytest.raises(InsecureBearerTransportError):
        authorization_safe_urlopen(request, timeout=1)


def test_governance_request_rejects_non_loopback_cleartext_without_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = urllib.request.Request("http://example.test/v1")

    def unexpected_open(*_args, **_kwargs):
        pytest.fail("cleartext governance request reached the network opener")

    monkeypatch.setattr("_http_auth._AUTHORIZATION_SAFE_OPENER.open", unexpected_open)
    with pytest.raises(InsecureBearerTransportError):
        authorization_safe_urlopen(request, timeout=1)


@pytest.mark.parametrize("url", ["http://localhost:8767/v1", "http://127.0.0.1:8767/v1"])
def test_bearer_request_allows_loopback_development_transport(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    sentinel = object()
    monkeypatch.setattr(
        "_http_auth._LOOPBACK_DIRECT_OPENER.open",
        lambda *_args, **_kwargs: sentinel,
    )

    request = urllib.request.Request(url, headers={"Authorization": "Bearer secret"})
    assert authorization_safe_urlopen(request, timeout=1) is sentinel


def test_loopback_request_bypasses_ambient_proxy_and_keeps_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_headers: list[str | None] = []
    proxy_headers: list[str | None] = []

    class Target(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):
            return

        def do_GET(self):  # noqa: N802
            target_headers.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):
            return

        def do_GET(self):  # noqa: N802
            proxy_headers.append(self.headers.get("Authorization"))
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    target_thread.start()
    proxy_thread.start()
    proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setattr(
        _http_auth,
        "_AUTHORIZATION_SAFE_OPENER",
        urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url}),
            _AuthorizationSafeRedirectHandler(),
        ),
    )
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{target.server_address[1]}/health",
            headers={"Authorization": "Bearer loopback-secret"},
        )
        with authorization_safe_urlopen(request, timeout=2) as response:
            assert response.status == 200
    finally:
        target.shutdown()
        proxy.shutdown()
        target_thread.join(timeout=2)
        proxy_thread.join(timeout=2)
        target.server_close()
        proxy.server_close()

    assert target_headers == ["Bearer loopback-secret"]
    assert proxy_headers == []


def test_json_headers_omit_authorization_without_token(monkeypatch):
    monkeypatch.delenv("UNITARES_HTTP_API_TOKEN", raising=False)

    assert governance_json_headers() == {"Content-Type": "application/json"}

    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "   ")
    assert governance_json_headers() == {"Content-Type": "application/json"}


def test_json_headers_read_bearer_token_for_each_request(monkeypatch):
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "first-token")
    assert governance_json_headers() == {
        "Content-Type": "application/json",
        "Authorization": "Bearer first-token",
    }

    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", " second-token ")
    assert governance_json_headers()["Authorization"] == "Bearer second-token"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("http://example.test/start", "http://example.test/next"),
        ("http://EXAMPLE.test:80/start", "http://example.TEST/next"),
        ("https://example.test/start", "https://example.test:443/next"),
    ],
)
def test_same_origin_redirect_retains_authorization(source: str, target: str):
    request = urllib.request.Request(
        source,
        headers={"Authorization": "Bearer secret"},
    )

    redirected = _AuthorizationSafeRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        target,
    )

    assert redirected is not None
    assert redirected.get_header("Authorization") == "Bearer secret"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("http://example.test/start", "http://other.test/next"),
        ("http://example.test/start", "http://sub.example.test/next"),
        ("http://example.test/start", "http://example.test:8080/next"),
        ("http://example.test/start", "https://example.test/next"),
        ("http://example.test:bad/start", "http://example.test/next"),
        ("http://example.test/start", "http://example.test:bad/next"),
    ],
)
def test_cross_origin_or_malformed_redirect_is_refused(
    source: str,
    target: str,
):
    request = urllib.request.Request(
        source,
        headers={"Authorization": "Bearer secret"},
    )

    redirected = _AuthorizationSafeRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        target,
    )

    assert redirected is None


def test_refused_cross_origin_redirect_cannot_start_a_chain():
    handler = _AuthorizationSafeRedirectHandler()
    original = urllib.request.Request(
        "https://example.test/start",
        headers={"Authorization": "Bearer secret"},
    )
    cross_origin = handler.redirect_request(
        original,
        None,
        302,
        "Found",
        {},
        "https://other.test/middle",
    )
    assert cross_origin is None


class _Response:
    def __init__(self, body: bytes, declared: str | None = None):
        self.body = body
        self.headers = {} if declared is None else {"Content-Length": declared}

    def read(self, size: int) -> bytes:
        return self.body[:size]


def test_limited_response_rejects_declared_oversize_without_reading():
    response = _Response(b"small", declared="999")
    with pytest.raises(ResponseTooLargeError):
        read_limited_response(response, max_bytes=8)


def test_limited_response_rejects_chunked_oversize():
    with pytest.raises(ResponseTooLargeError):
        read_limited_response(_Response(b"ninebytes"), max_bytes=8)


def test_limited_response_accepts_body_at_limit():
    assert read_limited_response(_Response(b"12345678"), max_bytes=8) == b"12345678"


def test_production_scripts_do_not_bypass_authorization_safe_opener():
    violations: list[str] = []
    for path in sorted(SCRIPTS.rglob("*.py")):
        if path.name == "_http_auth.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_urlopen: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "urllib.request":
                imported_urlopen.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "urlopen"
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            raw_attribute = (
                isinstance(function, ast.Attribute)
                and function.attr == "urlopen"
                and isinstance(function.value, ast.Attribute)
                and function.value.attr == "request"
                and isinstance(function.value.value, ast.Name)
                and function.value.value.id == "urllib"
            )
            raw_import = isinstance(function, ast.Name) and function.id in imported_urlopen
            if raw_attribute or raw_import:
                violations.append(f"{path.relative_to(SCRIPTS)}:{node.lineno}")

    assert violations == []


def test_production_http_clients_do_not_use_unbounded_response_reads():
    violations: list[str] = []
    response_names = {"response", "resp", "exc"}
    for path in sorted(SCRIPTS.rglob("*.py")):
        if path.name == "_http_auth.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or node.args or node.keywords:
                continue
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "read"
                and isinstance(function.value, ast.Name)
                and function.value.id in response_names
            ):
                violations.append(f"{path.relative_to(SCRIPTS)}:{node.lineno}")

    assert violations == []
