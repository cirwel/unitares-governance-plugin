#!/usr/bin/env python3
"""Local, per-session receipts for Codex plugin hook invocations.

Only an invoked runner can write a receipt. Host-side trust skips, unmatched
events, and failures before the runner starts are outside this report. Never
store hook input, output, environment values, or raw session IDs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SCHEMA = 1
RESULTS = ("succeeded", "skipped", "timed_out", "failed", "incomplete")


def report_dir() -> Path:
    override = os.environ.get("UNITARES_HOOK_REPORT_DIR")
    return Path(override).expanduser() if override else Path.home() / ".unitares" / "hook-reports"


def session_path(session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return report_dir() / f"codex-{digest}.jsonl"


def _append(path: Path, record: dict[str, Any]) -> None:
    """One small O_APPEND write per receipt; reporting failure cannot break a hook."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def _version(root: Path, hook: str) -> tuple[str, str, str]:
    try:
        manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text())
        version = str(manifest.get("version") or "unknown")[:80]
    except (OSError, ValueError, TypeError, AttributeError):
        version = "unknown"
    try:
        definition = hashlib.sha256((root / "hooks" / "codex-hooks.json").read_bytes()).hexdigest()[:16]
    except OSError:
        definition = "unknown"
    code = hashlib.sha256()
    try:
        for path in (root / "hooks" / "run-hook.cmd", root / "hooks" / hook,
                     root / "scripts" / "hook_invocations.py"):
            code.update(path.read_bytes())
        code_hash = code.hexdigest()[:16]
    except OSError:
        code_hash = "unknown"
    return version, definition, code_hash


def _configured_timeout(root: Path, hook: str) -> float | None:
    """Read the host timeout from the loaded plugin manifest, not a second map."""
    try:
        config = json.loads((root / "hooks" / "codex-hooks.json").read_text())
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    times: list[float] = []
    for groups in config.get("hooks", {}).values():
        for group in groups:
            for handler in group.get("hooks", []):
                command = handler.get("command", "")
                # Match the executable argument, not e.g. --event session-start
                # in the watcher-context command.
                if (not isinstance(command, str)
                        or f'"$_unitares_runner" {hook} ' not in command
                        or " --host codex" not in command):
                    continue
                try:
                    times.append(float(handler["timeout"]))
                except (KeyError, TypeError, ValueError):
                    pass
    return min(times) if times else None


def _label(hook: str, arguments: list[str]) -> str:
    try:
        event = arguments[arguments.index("--event") + 1]
    except (ValueError, IndexError):
        return hook
    return f"{hook}:{event[:50]}"


def _session_id(raw: bytes) -> str:
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return ""
    value = payload.get("session_id") if isinstance(payload, dict) else None
    return value.strip() if isinstance(value, str) else ""


def _kill_process(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "nt":
            system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
            taskkill = ntpath.join(system_root, "System32", "taskkill.exe")
            subprocess.run([taskkill, "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=0.25, check=False)
        else:
            # The child stays in Codex's process group so a host-level kill can
            # reach it. On our own earlier deadline, kill its process tree
            # without signaling the host's group.
            result = subprocess.run(["ps", "-axo", "pid=,ppid="],
                                    text=True, capture_output=True, timeout=0.25,
                                    check=False)
            children: dict[int, list[int]] = defaultdict(list)
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) == 2 and all(field.isdecimal() for field in fields):
                    children[int(fields[1])].append(int(fields[0]))
            descendants: list[int] = []
            pending = [proc.pid]
            seen = {proc.pid}
            while pending:
                parent = pending.pop()
                for child in children.get(parent, []):
                    if child not in seen:
                        seen.add(child)
                        descendants.append(child)
                        pending.append(child)
            for pid in reversed(descendants):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if proc.poll() is None:
            proc.kill()
    except ProcessLookupError:
        pass


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    _kill_process(proc)
    proc.communicate()


def run(root: Path, hook: str, command: list[str]) -> int:
    if not command:
        return 2
    raw = sys.stdin.buffer.read()
    session_id = _session_id(raw)
    path = session_path(session_id) if session_id else None
    version, definition, code_hash = _version(root, hook)
    hook_name = _label(hook, command)
    timeout = _configured_timeout(root, hook)
    # Let this runner record its own deadline before Codex's outer deadline.
    budget = max(0.25, timeout - min(0.5, timeout * 0.125)) if timeout else None
    invocation = uuid.uuid4().hex
    started = time.monotonic()
    base = {
        "schema": SCHEMA,
        "invocation": invocation,
        "hook": hook_name,
        "version": version,
        "definition": definition,
        "code": code_hash,
    }
    if path:
        _append(path, {**base, "phase": "start", "at": time.time()})

    status_path: Path | None = None
    child_env = os.environ.copy()
    if path:
        try:
            with tempfile.NamedTemporaryFile(prefix="hook-status-", dir=path.parent, delete=False) as temp:
                status_path = Path(temp.name)
            child_env["UNITARES_HOOK_STATUS_FILE"] = str(status_path)
        except OSError:
            pass

    code = 1
    outcome = "failed"
    active_proc: subprocess.Popen[bytes] | None = None

    def _on_host_signal(signum: int, _frame: Any) -> None:
        # Keep the hook in the host's process group. If only the reporter gets
        # a catchable cancellation signal, stop its child before exiting.
        if active_proc is not None:
            _kill_process(active_proc)
        os._exit(128 + signum)

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, _on_host_signal)
    try:
        active_proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            env=child_env,
        )
        try:
            active_proc.communicate(input=raw, timeout=budget)
            code = active_proc.returncode
            outcome = "succeeded" if code == 0 else "failed"
        except subprocess.TimeoutExpired:
            _stop_process(active_proc)
            code = 124
            outcome = "timed_out"
    except OSError as exc:
        print(f"run-hook.cmd: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        if status_path:
            try:
                if outcome == "succeeded" and status_path.read_text().strip() == "skipped":
                    outcome = "skipped"
                status_path.unlink(missing_ok=True)
            except (OSError, UnicodeError):
                pass
        if path:
            _append(path, {
                **base,
                "phase": "end",
                "at": time.time(),
                "duration_ms": round((time.monotonic() - started) * 1000),
                "status": outcome,
                "exit_code": code,
            })
    return code


def _load(path: Path) -> list[dict[str, Any]]:
    records = []
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeError):
        return records
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("schema") == SCHEMA:
            records.append(item)
    return records


def report(path: Path, *, json_output: bool) -> int:
    records = _load(path)
    if not records:
        print("No Codex plugin hook invocations were observed for this session.")
        return 1
    starts = {r["invocation"]: r for r in records if r.get("phase") == "start" and r.get("invocation")}
    ends = {r["invocation"]: r for r in records if r.get("phase") == "end" and r.get("invocation")}
    counts: Counter[str] = Counter()
    by_hook: dict[str, Counter[str]] = defaultdict(Counter)
    versions: set[str] = set()
    for invocation, start in starts.items():
        end = ends.get(invocation)
        status = end.get("status") if end else "incomplete"
        if status not in RESULTS:
            status = "incomplete"
        hook = str(start.get("hook") or "unknown")
        counts["fired"] += 1
        counts[status] += 1
        by_hook[hook][status] += 1
        versions.add(
            f"{start.get('version', 'unknown')} "
            f"(definition {start.get('definition', 'unknown')}, code {start.get('code', 'unknown')})"
        )
    summary = {
        "session": path.stem.removeprefix("codex-"),
        "versions": sorted(versions),
        "counts": {name: counts[name] for name in ("fired", *RESULTS)},
        "hooks": {hook: dict(by_hook[hook]) for hook in sorted(by_hook)},
        "limits": "Only plugin hooks whose reporting runner started are observed. Skipped means an explicit hook no-op; Codex trust or matcher skips cannot be counted. Incomplete means no end was recorded yet; an active hook, host timeout, and crash are indistinguishable.",
    }
    if json_output:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"Codex hook report: session {summary['session']}")
        print("Plugin version / hook definition / direct code: " + ", ".join(summary["versions"]))
        print("  " + "  ".join(f"{name}={counts[name]}" for name in ("fired", *RESULTS)))
        for hook, hook_counts in summary["hooks"].items():
            detail = ", ".join(f"{name}={hook_counts[name]}" for name in RESULTS if hook_counts[name])
            print(f"  {hook}: {detail}")
        print(summary["limits"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    runner = sub.add_parser("run", help="run a Codex hook and record its outcome")
    runner.add_argument("--plugin-root", type=Path, required=True)
    runner.add_argument("--hook", required=True)
    runner.add_argument("command", nargs=argparse.REMAINDER)
    viewer = sub.add_parser("report", help="show an observed session report")
    select = viewer.add_mutually_exclusive_group(required=True)
    select.add_argument("--session-id")
    select.add_argument("--latest", action="store_true")
    viewer.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        return run(args.plugin_root.resolve(), args.hook, command)
    if args.latest:
        paths = list(report_dir().glob("codex-*.jsonl"))
        if not paths:
            print("No Codex plugin hook reports found.")
            return 1
        path = max(paths, key=lambda item: item.stat().st_mtime)
    else:
        path = session_path(args.session_id)
    return report(path, json_output=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
