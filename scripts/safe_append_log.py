#!/usr/bin/env python3
"""Append stdin to an owner-only regular log without following symlinks."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path


def append_bytes(path: Path, payload: bytes) -> None:
    path = path.expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("diagnostic log is not a regular file")
        getuid = getattr(os, "getuid", None)
        if callable(getuid) and info.st_uid != getuid():
            raise OSError("diagnostic log is not owned by the current user")
        if info.st_mode & 0o077:
            raise OSError("diagnostic log permissions are not owner-only")
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("diagnostic log append made no progress")
            remaining = remaining[written:]
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True)
    args = parser.parse_args(argv)
    try:
        append_bytes(Path(args.path), sys.stdin.buffer.read())
    except OSError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
