#!/usr/bin/env python3
"""Validate executable helper command snippets in agent-facing docs.

The goal is not to run documented commands. Many examples write local cache
state. Instead, this lints whether snippets that mention repo helper CLIs parse
against the helper's own argparse contract.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import shlex
import sys
from pathlib import Path


sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
DOC_PATHS = [
    ROOT / "README.md",
    ROOT / "CODEX_START.md",
    *sorted((ROOT / "commands").glob("*.md")),
]


def _load_session_cache_parser():
    script = ROOT / "scripts" / "session_cache.py"
    spec = importlib.util.spec_from_file_location("session_cache_for_lint", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_parser()


PARSERS = {
    "scripts/session_cache.py": _load_session_cache_parser,
}


def _code_spans(text: str) -> list[str]:
    """Return single-backtick spans from markdown text."""
    spans: list[str] = []
    for match in re.finditer(r"(?<!`)`([^`\n]+)`(?!`)", text):
        spans.append(match.group(1).strip())
    return spans


def _normal_script_name(token: str) -> str | None:
    token = token.removeprefix("./")
    if token in PARSERS:
        return token
    return None


def _validate_snippet(snippet: str) -> str | None:
    try:
        parts = shlex.split(snippet)
    except ValueError as exc:
        return f"shell parse failed: {exc}"
    if not parts:
        return None

    script_name = _normal_script_name(parts[0])
    if script_name is None:
        return None

    # A bare helper reference is not a command example.
    if len(parts) == 1:
        return None

    parser = PARSERS[script_name]()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            parser.parse_args(parts[1:])
    except SystemExit as exc:
        return (
            f"{script_name} argparse rejected snippet with exit {exc.code}: "
            f"{stderr.getvalue().strip()}"
        )
    return None


def main() -> int:
    failures: list[str] = []
    for path in DOC_PATHS:
        text = path.read_text(encoding="utf-8")
        for snippet in _code_spans(text):
            error = _validate_snippet(snippet)
            if error:
                rel = path.relative_to(ROOT)
                failures.append(f"{rel}: `{snippet}`\n  {error}")

    if failures:
        print("[doc-cmd] documented helper command drift found:")
        for failure in failures:
            print(failure)
        return 1

    print("[doc-cmd] OK — documented helper commands parse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
