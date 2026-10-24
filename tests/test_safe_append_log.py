from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from scripts.safe_append_log import append_bytes


def test_safe_append_creates_owner_only_regular_file(tmp_path: Path) -> None:
    log = tmp_path / "private" / "session-start.log"

    append_bytes(log, b"first\n")
    append_bytes(log, b"second\n")

    assert log.read_bytes() == b"first\nsecond\n"
    assert stat.S_ISREG(log.stat().st_mode)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_safe_append_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"original\n")
    link = tmp_path / "session-start.log"
    link.symlink_to(target)

    with pytest.raises(OSError):
        append_bytes(link, b"attacker-controlled append\n")

    assert target.read_bytes() == b"original\n"


def test_safe_append_refuses_permissive_existing_file(tmp_path: Path) -> None:
    log = tmp_path / "session-start.log"
    log.write_bytes(b"original\n")
    os.chmod(log, 0o644)

    with pytest.raises(OSError):
        append_bytes(log, b"not written\n")

    assert log.read_bytes() == b"original\n"
