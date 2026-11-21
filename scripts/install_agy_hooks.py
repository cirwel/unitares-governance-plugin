#!/usr/bin/env python3
"""Install or verify UNITARES Antigravity hooks in ~/.gemini/config/hooks.json.

Why hooks/agy-hooks.json instead of hooks/hooks.json:
Claude Code auto-discovers hooks/hooks.json at the plugin root, which would
collide with .claude-plugin/plugin.json's explicit ./hooks/claude-hooks.json
manifest (enforced by tests/test_codex_plugin_contract.py). Antigravity user
hooks instead live in ~/.gemini/config/hooks.json, where PLUGIN_ROOT is not set
and hook commands execute with cwd=~/.gemini/config.

This helper reads hooks/agy-hooks.json, resolves ${PLUGIN_ROOT:-.} to the
checkout's absolute path, and merges only the "unitares-governance" entry into
the target hooks.json while preserving all other hook groups intact.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

HOOK_GROUP_KEY = "unitares-governance"
DEFAULT_TARGET = Path.home() / ".gemini" / "config" / "hooks.json"


def _render_command(
    command: str,
    *,
    plugin_root: Path,
    keep_plugin_root_var: bool,
    auto_onboard: bool,
    is_lifecycle_event: bool,
) -> str:
    rendered = command
    if not keep_plugin_root_var:
        root_str = str(plugin_root.resolve())
        rendered = rendered.replace("${PLUGIN_ROOT:-.}", root_str).replace(
            "${PLUGIN_ROOT}", root_str
        )
    if auto_onboard and is_lifecycle_event and "UNITARES_AUTO_ONBOARD=" not in rendered:
        rendered = "UNITARES_AUTO_ONBOARD=on " + rendered
    return rendered


def render_agy_hook_entry(
    plugin_root: Path,
    *,
    keep_plugin_root_var: bool = False,
    auto_onboard: bool = False,
) -> dict[str, Any]:
    """Load hooks/agy-hooks.json and render its unitares-governance entry."""
    manifest_path = plugin_root / "hooks" / "agy-hooks.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or HOOK_GROUP_KEY not in data:
        raise ValueError(f"{manifest_path} missing top-level {HOOK_GROUP_KEY!r} key")
    entry = copy.deepcopy(data[HOOK_GROUP_KEY])
    if not isinstance(entry, dict):
        raise ValueError(f"{manifest_path} {HOOK_GROUP_KEY!r} entry must be an object")

    for event_name in ("SessionStart", "Stop", "PreInvocation", "PostInvocation"):
        handlers = entry.get(event_name)
        if isinstance(handlers, list):
            for handler in handlers:
                if isinstance(handler, dict) and isinstance(handler.get("command"), str):
                    handler["command"] = _render_command(
                        handler["command"],
                        plugin_root=plugin_root,
                        keep_plugin_root_var=keep_plugin_root_var,
                        auto_onboard=auto_onboard,
                        is_lifecycle_event=event_name in ("SessionStart", "Stop"),
                    )

    for event_name in ("PreToolUse", "PostToolUse"):
        groups = entry.get(event_name)
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                handlers = group.get("hooks")
                if isinstance(handlers, list):
                    for handler in handlers:
                        if isinstance(handler, dict) and isinstance(handler.get("command"), str):
                            handler["command"] = _render_command(
                                handler["command"],
                                plugin_root=plugin_root,
                                keep_plugin_root_var=keep_plugin_root_var,
                                auto_onboard=auto_onboard,
                                is_lifecycle_event=False,
                            )
    return entry


def merge_agy_hooks(
    existing: dict[str, Any],
    *,
    plugin_root: Path,
    keep_plugin_root_var: bool = False,
    auto_onboard: bool = False,
) -> dict[str, Any]:
    """Return a copy of ``existing`` with ``unitares-governance`` updated."""
    merged = copy.deepcopy(existing)
    merged[HOOK_GROUP_KEY] = render_agy_hook_entry(
        plugin_root,
        keep_plugin_root_var=keep_plugin_root_var,
        auto_onboard=auto_onboard,
    )
    return merged


def _load_target(target: Path) -> dict[str, Any]:
    if not target.exists():
        return {}
    raw = target.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{target} must contain a JSON object")
    return data


def _resolve_write_target(target: Path) -> Path:
    """Resolve a symlink target so atomic replace updates the linked file instead of clobbering the symlink."""
    if target.is_symlink():
        return target.resolve()
    return target


@contextmanager
def _target_lock(target: Path) -> Iterator[None]:
    """Serialize read-modify-write updates to ``target`` via a sibling advisory flock."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / f".{target.name}.lock"
    fd = os.open(
        str(lock_path),
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _atomic_write_json(target: Path, data: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        existing_mode = None
    rendered = json.dumps(data, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        if existing_mode is not None:
            os.chmod(tmp_path, existing_mode)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help="Target Antigravity hooks.json path (default: ~/.gemini/config/hooks.json).",
    )
    parser.add_argument(
        "--plugin-root",
        type=Path,
        default=default_root,
        help="Path to the unitares-governance-plugin checkout.",
    )
    parser.add_argument(
        "--auto-onboard",
        action="store_true",
        help="Prefix UNITARES_AUTO_ONBOARD=on on SessionStart and Stop commands.",
    )
    parser.add_argument(
        "--keep-plugin-root-var",
        action="store_true",
        help="Keep ${PLUGIN_ROOT:-.} instead of substituting the absolute plugin root.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 0 if target's unitares-governance entry already matches, else 1.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print merged JSON to stdout without modifying the target file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = args.target.expanduser()
    write_target = _resolve_write_target(target)
    plugin_root = args.plugin_root.expanduser().resolve()

    if args.check or args.dry_run:
        try:
            existing = _load_target(write_target)
            merged = merge_agy_hooks(
                existing,
                plugin_root=plugin_root,
                keep_plugin_root_var=args.keep_plugin_root_var,
                auto_onboard=args.auto_onboard,
            )
        except Exception as exc:
            print(f"install_agy_hooks.py: {exc}", file=sys.stderr)
            return 2

        if args.check:
            if existing.get(HOOK_GROUP_KEY) == merged[HOOK_GROUP_KEY]:
                print(f"[agy-hooks] OK — {target} matches {plugin_root / 'hooks' / 'agy-hooks.json'}")
                return 0
            print(
                f"[agy-hooks] drift — {target} does not match rendered {plugin_root / 'hooks' / 'agy-hooks.json'}",
                file=sys.stderr,
            )
            return 1

        print(json.dumps(merged, indent=2))
        return 0

    try:
        with _target_lock(write_target):
            existing = _load_target(write_target)
            merged = merge_agy_hooks(
                existing,
                plugin_root=plugin_root,
                keep_plugin_root_var=args.keep_plugin_root_var,
                auto_onboard=args.auto_onboard,
            )
            _atomic_write_json(write_target, merged)
    except Exception as exc:
        print(f"install_agy_hooks.py: {exc}", file=sys.stderr)
        return 2

    print(f"[agy-hooks] installed {HOOK_GROUP_KEY} hooks into {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
