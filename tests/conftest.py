"""Test-wide state isolation: sandbox HOME and the governance log paths.

Several hook / session-cache tests invoke the hook scripts as subprocesses
*without* an explicit ``env=``, so the scripts inherit the test process's HOME
and write their ``$HOME``-derived artifacts into the developer's REAL
``~/.unitares/``: ``hook-skips.log`` (``${HOME}/.unitares/hook-skips.log``),
``checkins.log``, and the slotted HOME session mirror
(``$HOME/.unitares/session-<slot>.json``).

Confirmed 2026-06-02: running the hook-subprocess tests appended skip lines to
the live ``hook-skips.log`` and dropped ``session-*.json`` fixtures into the
real directory. Beyond being dirty, that pollution can *mask the Vigil
plugin-hook-liveness canary*, which reads ``hook-skips.log`` mtime to decide
whether the hook chain is alive — a test run would keep the artifact looking
fresh.

This autouse fixture redirects HOME (and the explicit log-path env overrides
the hooks honor) to a per-test temp dir, so no test can touch real state
regardless of whether the individual test remembers to isolate. In-process
``Path.home()`` honors ``$HOME`` and subprocesses inherit it, so both paths are
covered. Tests that set their own HOME (e.g. test_session_cache builds an
explicit subprocess env) override this within the test and are unaffected.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    (home / ".unitares").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("UNITARES_CHECKIN_LOG", str(home / ".unitares" / "checkins.log"))
    monkeypatch.setenv(
        "UNITARES_HOOK_DEBUG_LOG", str(home / ".unitares" / "hook-skips.log")
    )
    yield
