"""scripts/auto_onboard_decision.py: the one answer every hook uses."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from auto_onboard_decision import decide, endpoint_trusted, is_loopback_url  # noqa: E402

LOCAL = "http://localhost:8767"


def _decide(**kwargs):
    base = dict(mode=None, disable=None, checkins=None, owns_anchor=False,
                server_url=LOCAL, url_explicit=False)
    base.update(kwargs)
    return decide(**base)


@pytest.mark.parametrize("url", [
    "http://localhost:8767", "http://127.0.0.1:8767", "http://127.0.0.53", "http://[::1]:8767",
    "https://LOCALHOST/",
])
def test_loopback_urls(url):
    assert is_loopback_url(url)


@pytest.mark.parametrize("url", [
    "http://example.org", "https://example.org", "http://10.0.0.5:8767", "http://0.0.0.0:8767",
    "http://localhost.example.org", "http://127.0.0.1.example.org", "", "not a url", "http://",
])
def test_urls_that_are_not_loopback(url):
    assert not is_loopback_url(url)


@pytest.mark.parametrize("url,explicit,trusted", [
    (LOCAL, False, True),
    (LOCAL, True, True),
    ("https://governance.example.org", True, True),
    # HTTPS, but nobody on this machine named it.
    ("https://governance.example.org", False, False),
    # Named, but nothing says the answer comes from the server that was meant.
    ("http://governance.example.org", True, False),
    ("http://192.168.1.10:8767", True, False),
    ("", True, False),
])
def test_endpoint_trust(url, explicit, trusted):
    assert endpoint_trusted(url, explicit=explicit) is trusted


@pytest.mark.parametrize("mode", [None, "", "auto", " AUTO "])
def test_the_default_asks_a_local_server(mode):
    assert _decide(mode=mode) == (True, "auto")


def test_the_default_does_not_ask_a_remote_plain_http_server():
    assert _decide(server_url="http://governance.example.org", url_explicit=True) == (
        False, "untrusted_endpoint")


def test_the_default_asks_a_remote_https_server_the_operator_named():
    assert _decide(server_url="https://governance.example.org", url_explicit=True) == (True, "auto")


@pytest.mark.parametrize("mode", ["off", "0", "FALSE", " no "])
def test_off_never_asks(mode):
    assert _decide(mode=mode) == (False, "off")
    assert _decide(mode=mode, owns_anchor=True) == (False, "off")


@pytest.mark.parametrize("mode", ["on", "1", "true", "yes", "o f f", "yes please"])
def test_on_and_unknown_words_ask_even_a_remote_plain_http_server(mode):
    assert _decide(mode=mode, server_url="http://governance.example.org") == (True, "on")


@pytest.mark.parametrize("mode", ["anchored", " Anchored\n"])
def test_anchored_asks_only_for_the_anchor_owner(mode):
    assert _decide(mode=mode, owns_anchor=True) == (True, "anchored")
    assert _decide(mode=mode, owns_anchor=False) == (False, "not_anchor_owner")


@pytest.mark.parametrize("disable", ["1", "true", "ON", "yes"])
@pytest.mark.parametrize("mode", [None, "on", "anchored", "auto"])
def test_the_hard_disable_wins_over_every_mode(disable, mode):
    assert _decide(mode=mode, disable=disable, owns_anchor=True) == (False, "disabled")


@pytest.mark.parametrize("disable", ["0", "", "no", "off"])
def test_other_disable_values_do_not_disable(disable):
    assert _decide(mode="on", disable=disable)[0] is True


@pytest.mark.parametrize("mode", [None, "on", "anchored"])
def test_checkins_off_wins(mode):
    assert _decide(mode=mode, checkins="off", owns_anchor=True) == (False, "checkins_off")
    assert _decide(mode=mode, checkins="0", owns_anchor=True)[0] is True


def _cli(*args):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "auto_onboard_decision.py"), *args],
        text=True, capture_output=True, timeout=20,
    )


def test_cli_prints_one_word_and_never_fails():
    assert _cli("--server-url", LOCAL).stdout.strip() == "bind"
    assert _cli("--mode", "off", "--server-url", LOCAL).stdout.strip() == "skip:off"
    assert _cli("--mode", "anchored", "--owns-anchor", "1").stdout.strip() == "bind"
    assert _cli("--server-url", "http://example.org", "--url-explicit", "1").stdout.strip() == (
        "skip:untrusted_endpoint")
    bad = _cli("--no-such-flag")
    assert bad.returncode == 0 and bad.stdout.strip() == "skip:error"
