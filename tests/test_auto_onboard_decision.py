"""scripts/auto_onboard_decision.py: the one answer every hook uses."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from auto_onboard_decision import decide, transport_allows  # noqa: E402
from harness_decline_marker import (  # noqa: E402
    DECLINE_TTL_SECONDS,
    record_decline,
    recently_declined,
)

LOCAL = "http://localhost:8767"
never = lambda slot, url: False  # noqa: E731


def _decide(**kwargs):
    base = dict(mode=None, disable=None, checkins=None, owns_anchor=False,
                server_url=LOCAL, slot="slot-1", declined=never)
    base.update(kwargs)
    return decide(**base)


@pytest.mark.parametrize("url", [
    "http://localhost:8767", "http://127.0.0.1:8767", "http://[::1]:8767",
    "https://governance.example.org", "https://governance.example.org:8443/base",
])
def test_urls_the_transport_sends_to(url):
    assert transport_allows(url)


@pytest.mark.parametrize("url", [
    "http://governance.example.org", "http://192.168.1.10:8767", "http://0.0.0.0:8767",
    "http://localhost.example.org", "ftp://localhost", "https://", "https:", "",
    "not a url", "http://user@localhost:8767", "https://user:pw@governance.example.org",
    "http://localhost:99999", "http://local host", "http://localhost\\@example.org",
])
def test_urls_the_transport_refuses(url):
    assert not transport_allows(url)


def test_the_rule_is_the_transports_own():
    """Asked through the same functions the request is held to, so the two
    cannot drift apart."""
    import urllib.request
    from _http_auth import InsecureBearerTransportError, _validate_governance_transport

    for url in ("http://localhost:8767", "https://example.org", "http://example.org",
                "http://user@localhost", "ftp://localhost", "https://"):
        try:
            _validate_governance_transport(urllib.request.Request(url + "/v1/tools/call"))
            sent = True
        except (InsecureBearerTransportError, ValueError):
            sent = False
        assert transport_allows(url) is sent, url


@pytest.mark.parametrize("mode", [None, "", "on", " ON ", "1", "true", "yes", "auto", "o f f"])
def test_the_default_and_every_unknown_word_ask(mode):
    assert _decide(mode=mode) == (True, "on")


@pytest.mark.parametrize("mode", ["off", "0", "FALSE", " no "])
def test_off_never_asks(mode):
    assert _decide(mode=mode) == (False, "off")
    assert _decide(mode=mode, owns_anchor=True) == (False, "off")


@pytest.mark.parametrize("mode", ["anchored", " Anchored\n"])
def test_anchored_asks_only_for_the_anchor_owner(mode):
    assert _decide(mode=mode, owns_anchor=True) == (True, "anchored")
    assert _decide(mode=mode, owns_anchor=False) == (False, "not_anchor_owner")


@pytest.mark.parametrize("mode", [None, "on", "anchored"])
def test_no_mode_asks_a_server_the_transport_refuses(mode):
    assert _decide(mode=mode, owns_anchor=True, server_url="http://governance.example.org") == (
        False, "insecure_transport")


@pytest.mark.parametrize("disable", ["1", "true", "ON", "yes"])
@pytest.mark.parametrize("mode", [None, "on", "anchored"])
def test_the_hard_disable_wins_over_every_mode(disable, mode):
    assert _decide(mode=mode, disable=disable, owns_anchor=True) == (False, "disabled")


@pytest.mark.parametrize("disable", ["0", "", "no", "off"])
def test_other_disable_values_do_not_disable(disable):
    assert _decide(mode="on", disable=disable)[0] is True


@pytest.mark.parametrize("mode", [None, "on", "anchored"])
def test_checkins_off_wins(mode):
    assert _decide(mode=mode, checkins="off", owns_anchor=True) == (False, "checkins_off")
    assert _decide(mode=mode, checkins="0", owns_anchor=True)[0] is True


def test_a_slot_the_server_declined_is_not_asked_again():
    declined = lambda slot, url: (slot, url) == ("slot-1", LOCAL)  # noqa: E731
    assert _decide(declined=declined) == (False, "server_declined")
    assert _decide(declined=declined, slot="slot-2")[0] is True
    # An anchor is asked every time: its agent may have claimed an identity
    # since, and the hook must ask again to resume it.
    assert _decide(declined=declined, owns_anchor=True)[0] is True


def test_decline_marker_is_per_slot_per_server_and_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert not recently_declined("slot-1", LOCAL)
    record_decline("slot-1", LOCAL)
    assert recently_declined("slot-1", LOCAL)
    assert not recently_declined("slot-2", LOCAL)
    assert not recently_declined("slot-1", "https://other.example.org")
    assert not recently_declined("slot-1", LOCAL, now=time.time() + DECLINE_TTL_SECONDS + 1)
    assert not recently_declined(None, LOCAL)
    record_decline(None, LOCAL)  # nothing to key on: nothing written
    files = list((tmp_path / ".unitares" / "harness-declined").iterdir())
    assert len(files) == 1 and oct(files[0].stat().st_mode & 0o777) == "0o600"
    # The marker is read from disk by default.
    assert decide(mode=None, disable=None, checkins=None, owns_anchor=False,
                  server_url=LOCAL, slot="slot-1") == (False, "server_declined")


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"at": "soon"}', '{"server_url": 5}'])
def test_a_damaged_marker_reads_as_no_decline(tmp_path, monkeypatch, content):
    monkeypatch.setenv("HOME", str(tmp_path))
    record_decline("slot-1", LOCAL)
    next((tmp_path / ".unitares" / "harness-declined").iterdir()).write_text(content)
    assert not recently_declined("slot-1", LOCAL)


def _cli(*args, home):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "auto_onboard_decision.py"), *args],
        text=True, capture_output=True, timeout=20, env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )


def test_cli_prints_one_word_and_never_fails(tmp_path):
    assert _cli("--server-url", LOCAL, home=tmp_path).stdout.strip() == "bind"
    assert _cli("--mode", "off", "--server-url", LOCAL, home=tmp_path).stdout.strip() == "skip:off"
    assert _cli("--mode", "anchored", "--owns-anchor", "1", "--server-url", LOCAL,
                home=tmp_path).stdout.strip() == "bind"
    assert _cli("--server-url", "http://example.org", home=tmp_path).stdout.strip() == (
        "skip:insecure_transport")
    assert _cli(home=tmp_path).stdout.strip() == "skip:insecure_transport"
    bad = _cli("--no-such-flag", home=tmp_path)
    assert bad.returncode == 0 and bad.stdout.strip() == "skip:error"
