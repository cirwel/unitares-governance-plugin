#!/usr/bin/env python3
"""Fan one normalized edit event out to an explicitly trusted watcher hook."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def fan_out(hook: Path, paths: list[str], *, host: str) -> int:
    if host not in {"claude", "codex"} or not hook.is_file() or not os.access(hook, os.X_OK):
        return 0

    started = 0
    for file_path in paths:
        if not isinstance(file_path, str) or not file_path:
            continue
        payload = json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": file_path},
                "source_host": host,
            },
            separators=(",", ":"),
        ).encode()
        try:
            subprocess.run(
                [str(hook)],
                input=payload,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        started += 1
    return started


def start_worker(hook: Path, paths_json: str, *, host: str) -> bool:
    """Detach one worker so synchronous host hooks stay locally bounded."""
    try:
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--hook",
                str(hook),
                "--paths-json",
                paths_json,
                "--host",
                host,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hook", required=True, type=Path)
    parser.add_argument("--paths-json", required=True)
    parser.add_argument("--host", required=True, choices=("claude", "codex"))
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    try:
        paths = json.loads(args.paths_json)
    except json.JSONDecodeError:
        return 0
    if not isinstance(paths, list):
        return 0
    if args.worker:
        fan_out(args.hook, paths, host=args.host)
    else:
        start_worker(args.hook, args.paths_json, host=args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
