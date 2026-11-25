from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import doctor  # noqa: E402
import install_agy_hooks  # noqa: E402


@pytest.fixture(autouse=True)
def clean_lease_and_server_env(monkeypatch, tmp_path: Path):
    """Isolate tests from the developer's shell environment and secrets.env."""
    for key in (
        "UNITARES_SERVER_URL",
        "LEASE_PLANE_BASE_URL",
        "UNITARES_LEASE_PLANE_URL",
        "UNITARES_LEASE_PLANE_UNIX_SOCKET",
        "LEASE_PLANE_BEARER_TOKEN",
        "GOVERNANCE_TOKEN",
        "UNITARES_ALLOW_INSECURE_LOOPBACK_LEASE_HTTP",
        "UNITARES_FILE_LEASES_ENABLED",
        "UNITARES_FILE_LEASES_REQUIRED",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(tmp_path / "nonexistent-secrets.env"))


class _MockServer:
    def __init__(self, routes: dict[str, tuple[int, dict[str, Any]]]) -> None:
        self.routes = routes

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any, **kwargs: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                status, body = routes.get(self.path, (404, {"error": "not found"}))
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=2)


@pytest.fixture
def live_servers():
    gov = _MockServer({
        "/health": (
            200,
            {
                "status": "ok",
                "version": "4.1.0",
                "onboarding": {
                    "harness": "welcome",
                    "basis": "default_loopback",
                    "unexpected_extra": "must-not-copy",
                },
            },
        )
    })
    lease = _MockServer({
        "/v1/health": (200, {"ok": True, "protocol_version": "v1.0"}),
    })
    try:
        yield gov, lease
    finally:
        gov.close()
        lease.close()


def test_doctor_happy_path_with_live_mock_servers(tmp_path: Path, live_servers, monkeypatch):
    gov, lease = live_servers
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "test-bearer-secret")
    monkeypatch.setenv("UNITARES_ALLOW_INSECURE_LOOPBACK_LEASE_HTTP", "1")
    monkeypatch.setenv("UNITARES_FILE_LEASES_ENABLED", "1")
    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "0")

    ws = tmp_path / "ws"
    cache_dir = ws / ".unitares"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".gitignore").write_text("*\n", encoding="utf-8")
    (cache_dir / "session-slot1.json").write_text(
        json.dumps({
            "uuid": "12345678-aaaa-bbbb-cccc-1234567890ab",
            "client_session_id": "agent-12345678-secret-session-token",
            "continuity_token": "super-secret-continuity-token",
            "updated_at": "2026-11-25T16:00:00+00:00",
        }),
        encoding="utf-8",
    )

    agy_target = tmp_path / "hooks.json"
    agy_target.write_text(
        json.dumps(install_agy_hooks.merge_agy_hooks({}, plugin_root=ROOT, auto_onboard=True)),
        encoding="utf-8",
    )

    report = doctor.run_doctor(
        plugin_root=ROOT,
        workspace=ws,
        host="auto",
        server_url=gov.url + "/mcp/",
        lease_url=lease.url,
        timeout=2.0,
        python_path=Path(sys.executable),
        agy_hooks_path=agy_target,
        codex_report_dir=tmp_path / "no-reports",
    )

    assert report["status"] == "ok"
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["system_python"]["status"] == "ok"
    assert by_name["plugin_manifests"]["status"] == "ok"
    assert by_name["agy_hooks"]["status"] == "ok"
    assert by_name["agy_hooks"]["detail"]["auto_onboard"] is True
    assert by_name["server_health"]["status"] == "ok"
    assert "v4.1.0" in by_name["server_health"]["summary"]
    assert "unexpected_extra" not in by_name["server_health"]["detail"]["onboarding"]
    assert by_name["lease_plane"]["status"] == "ok"
    assert by_name["workspace_cache"]["status"] == "ok"
    assert by_name["workspace_cache"]["detail"]["bound_uuid_prefixes"] == ["12345678"]

    # Secrets must never appear anywhere in the JSON or text output.
    serialized = json.dumps(report) + doctor._format_text(report)
    assert "test-bearer-secret" not in serialized
    assert "secret-session-token" not in serialized
    assert "super-secret-continuity-token" not in serialized


def test_doctor_no_network_skips_http_probes_and_redacts_urls(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UNITARES_LEASE_PLANE_URL", "https://user:leasepass@lease.example.com:8788?token=ltok")
    report = doctor.run_doctor(
        plugin_root=ROOT,
        workspace=tmp_path,
        host="claude",
        server_url="https://user:srvpass@gov.example.com:8767/mcp/?api_key=stok",
        lease_url=None,
        no_network=True,
        python_path=Path(sys.executable),
    )
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["server_health"]["status"] == "skip"
    assert by_name["server_health"]["detail"]["url"] == "https://gov.example.com:8767"
    assert by_name["lease_plane"]["status"] == "skip"
    assert by_name["lease_plane"]["detail"]["url"] == "https://lease.example.com:8788"
    assert by_name["agy_hooks"]["status"] == "skip"
    assert by_name["codex_hook_reports"]["status"] == "skip"
    assert report["status"] == "ok"

    serialized = json.dumps(report) + doctor._format_text(report)
    for secret in ("srvpass", "stok", "leasepass", "ltok"):
        assert secret not in serialized


def test_doctor_flags_unreachable_lease_plane_fail_only_when_required(tmp_path: Path, live_servers, monkeypatch):
    gov, _ = live_servers
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "test-bearer")
    monkeypatch.setenv("UNITARES_ALLOW_INSECURE_LOOPBACK_LEASE_HTTP", "1")
    monkeypatch.setenv("UNITARES_FILE_LEASES_ENABLED", "1")

    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "0")
    fail_open = doctor.run_doctor(
        plugin_root=ROOT,
        workspace=tmp_path,
        host="claude",
        server_url=gov.url,
        lease_url="http://127.0.0.1:1",
        timeout=0.3,
        python_path=Path(sys.executable),
    )
    by_name = {c["name"]: c for c in fail_open["checks"]}
    assert by_name["lease_plane"]["status"] == "warn"
    assert by_name["lease_plane"]["detail"]["reason"] == "unreachable"
    assert fail_open["status"] == "warn"

    monkeypatch.setenv("UNITARES_FILE_LEASES_REQUIRED", "1")
    fail_closed = doctor.run_doctor(
        plugin_root=ROOT,
        workspace=tmp_path,
        host="claude",
        server_url=gov.url,
        lease_url="http://127.0.0.1:1",
        timeout=0.3,
        python_path=Path(sys.executable),
    )
    assert {c["name"]: c["status"] for c in fail_closed["checks"]}["lease_plane"] == "fail"
    assert fail_closed["status"] == "fail"


def test_doctor_reports_insecure_loopback_lease_transport_actionably(live_servers, monkeypatch):
    _, lease = live_servers
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "test-bearer")
    monkeypatch.setenv("UNITARES_FILE_LEASES_ENABLED", "1")
    # UNITARES_ALLOW_INSECURE_LOOPBACK_LEASE_HTTP intentionally unset
    res = doctor.check_lease_plane(lease.url, timeout=1.0, no_network=False)
    assert res["status"] == "warn"
    assert res["detail"]["reason"] == "insecure_transport"
    assert "UNITARES_ALLOW_INSECURE_LOOPBACK_LEASE_HTTP" in (res.get("fix") or "")


def test_doctor_handles_bad_scheme_and_non_loopback_http_server():
    no_scheme = doctor.check_server_health("localhost:8767", timeout=0.5, no_network=False)
    assert no_scheme["status"] == "fail"
    assert "scheme" in no_scheme["summary"]

    non_loopback = doctor.check_server_health("http://example.invalid:8767", timeout=0.5, no_network=False)
    assert non_loopback["status"] == "fail"
    assert "non-loopback HTTP" in non_loopback["summary"]


def test_doctor_workspace_cache_distinguishes_gitignore_and_corrupt_slots(tmp_path: Path):
    ws1 = tmp_path / "missing-ignore"
    (ws1 / ".unitares").mkdir(parents=True)
    ws1_check = doctor.check_workspace_cache(ws1)
    assert ws1_check["status"] == "warn"
    assert ".unitares/.gitignore missing" in ws1_check["summary"]
    assert ".gitignore" in (ws1_check.get("fix") or "")

    ws2 = tmp_path / "corrupt-only"
    cache2 = ws2 / ".unitares"
    cache2.mkdir(parents=True)
    (cache2 / ".gitignore").write_text("*\n", encoding="utf-8")
    # Auxiliary hook marker files (e.g. session-start-nudge-*.json) are valid JSON, not corrupt slots.
    (cache2 / "session-start-nudge-abc.json").write_text(json.dumps({"shown_at": 1700000000}), encoding="utf-8")
    assert doctor.check_workspace_cache(ws2)["status"] == "ok"

    (cache2 / "session-broken.json").write_text("{not json", encoding="utf-8")
    ws2_check = doctor.check_workspace_cache(ws2)
    assert ws2_check["status"] == "warn"
    assert ws2_check["detail"]["corrupt_slots"] == 1
    assert ".gitignore" not in (ws2_check.get("fix") or "")
    assert "corrupt session" in (ws2_check.get("fix") or "")


def test_doctor_codex_hook_reports_schema1_and_ignores_unknown(tmp_path: Path):
    reports = tmp_path / "hook-reports"
    reports.mkdir()
    (reports / "codex-abc123.jsonl").write_text(
        "\n".join([
            json.dumps({"schema": 1, "phase": "start", "invocation": "inv-1", "hook": "post-stop"}),
            json.dumps({"schema": 1, "phase": "end", "invocation": "inv-1", "status": "failed"}),
            json.dumps({"schema": 1, "phase": "start", "invocation": "inv-2", "hook": "session-start"}),
            json.dumps({"schema": 1, "phase": "end", "invocation": "inv-2", "status": "succeeded"}),
            json.dumps({"schema": 1, "phase": "start", "invocation": "inv-3", "hook": "post-edit"}),
            json.dumps({"schema": 99, "phase": "start", "invocation": "ignored", "hook": "future"}),
            "{not valid json",
        ]) + "\n",
        encoding="utf-8",
    )
    codex_check = doctor.check_codex_hook_reports(host="codex", report_root=reports)
    assert codex_check["status"] == "warn"
    assert codex_check["detail"]["counts"] == {
        "fired": 3,
        "succeeded": 1,
        "skipped": 0,
        "timed_out": 0,
        "failed": 1,
        "incomplete": 1,
    }


def test_doctor_agy_hooks_supports_keep_plugin_root_var_and_missing(tmp_path: Path):
    missing = doctor.check_agy_hooks(ROOT, host="agy", target_path=tmp_path / "missing-hooks.json")
    assert missing["status"] == "fail"
    assert "install_agy_hooks.py" in (missing.get("fix") or "")

    var_target = tmp_path / "var-hooks.json"
    var_target.write_text(
        json.dumps(
            install_agy_hooks.merge_agy_hooks(
                {},
                plugin_root=ROOT,
                keep_plugin_root_var=True,
                auto_onboard=False,
            )
        ),
        encoding="utf-8",
    )
    synced = doctor.check_agy_hooks(ROOT, host="agy", target_path=var_target)
    assert synced["status"] == "ok"
    assert synced["detail"]["keep_plugin_root_var"] is True


def test_doctor_server_health_strips_mcp_before_query_and_clamps_timeout(live_servers):
    gov, _ = live_servers
    res = doctor.check_server_health(
        f"{gov.url}/mcp/?api_key=top-secret-query",
        timeout=float("inf"),
        no_network=False,
    )
    assert res["status"] == "ok"
    assert res["detail"]["url"] == gov.url
    assert "top-secret-query" not in json.dumps(res)


def test_doctor_lease_url_override_beats_secrets_env_socket_and_reads_required(tmp_path: Path, live_servers, monkeypatch):
    _, lease = live_servers
    secrets = tmp_path / "secrets.env"
    secrets.write_text(
        "\n".join([
            "LEASE_PLANE_BEARER_TOKEN=from-secrets-file",
            "UNITARES_LEASE_PLANE_UNIX_SOCKET=/nonexistent/should-be-overridden.sock",
            "UNITARES_FILE_LEASES_REQUIRED=1",
            "UNITARES_ALLOW_INSECURE_LOOPBACK_LEASE_HTTP=1",
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UNITARES_SECRETS_ENV", str(secrets))
    monkeypatch.setenv("UNITARES_FILE_LEASES_ENABLED", "1")

    ok = doctor.check_lease_plane(lease.url, timeout=1.0, no_network=False)
    assert ok["status"] == "ok"
    assert ok["detail"]["url"] == lease.url
    assert ok["detail"]["required"] is True
    assert "from-secrets-file" not in json.dumps(ok)

    fail = doctor.check_lease_plane("http://127.0.0.1:1", timeout=0.3, no_network=False)
    assert fail["status"] == "fail"
    assert fail["detail"]["required"] is True


def test_doctor_codex_empty_or_unreadable_receipt_warns_and_text_report_works(tmp_path: Path, capsys):
    import hook_invocations

    reports = tmp_path / "hook-reports"
    reports.mkdir()
    bad = reports / "codex-empty.jsonl"
    bad.write_text("{broken json\n", encoding="utf-8")
    res = doctor.check_codex_hook_reports(host="codex", report_root=reports)
    assert res["status"] == "warn"
    assert "no schema-1 receipts readable" in res["summary"]

    good = tmp_path / "codex-good.jsonl"
    good.write_text(
        "\n".join([
            json.dumps({"schema": 1, "phase": "start", "invocation": "i1", "hook": "post-stop"}),
            json.dumps({"schema": 1, "phase": "end", "invocation": "i1", "status": "succeeded"}),
        ]) + "\n",
        encoding="utf-8",
    )
    assert hook_invocations.report(good, json_output=False) == 0
    assert "post-stop: succeeded=1" in capsys.readouterr().out


def test_doctor_cli_json_exit_code(tmp_path: Path, capsys):
    rc = doctor.main(["--host", "claude", "--workspace", str(tmp_path), "--no-network", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "checks" in payload
    assert rc in (0, 1)
    if Path("/usr/bin/python3").is_file() and os.access("/usr/bin/python3", os.X_OK):
        assert rc == 0
