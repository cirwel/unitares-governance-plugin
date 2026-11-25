"""Hook Python must stay runnable on the interpreter hooks actually get.

`hooks/run-hook.cmd` resets PATH to /usr/bin:/bin:/usr/sbin:/sbin, so on macOS
every hook runs /usr/bin/python3 from the Command Line Tools, which is Python
3.9, whatever else is installed (#453). The test suite runs on 3.12, so
nothing caught syntax the hooks' interpreter cannot parse. These tests pin
Python 3.9 as the syntax floor for every script a hook can run and every
quoted Python heredoc in the hook scripts.

They check syntax, not runtime APIs: a 3.10+ standard-library call would still
pass here.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FLOOR = (3, 9)
SYSTEM_PYTHON = Path("/usr/bin/python3")

# A heredoc whose delimiter is quoted is passed to Python verbatim. A comment
# line that quotes the pattern does not start one.
_HEREDOC = re.compile(
    r"^(?![ \t]*#)[^\n]*python3\b[^\n]*<<\s*'(?P<tag>[A-Z_]+)'[^\n]*\n"
    r"(?P<body>.*?)\n(?P=tag)\n",
    re.S | re.M,
)


def _hook_scripts() -> list[Path]:
    scripts = sorted((ROOT / "scripts").glob("*.py"))
    scripts.append(ROOT / "scripts" / "u-run")
    return scripts


def _hook_heredocs() -> list[tuple[str, str]]:
    blocks = []
    for hook in sorted((ROOT / "hooks").iterdir()):
        if not hook.is_file() or hook.suffix == ".json":
            continue
        text = hook.read_text(encoding="utf-8")
        for n, match in enumerate(_HEREDOC.finditer(text)):
            blocks.append((f"{hook.name}#{n}", match.group("body")))
    return blocks


def test_the_heredoc_scan_finds_the_hook_python() -> None:
    names = {name.split("#")[0] for name, _ in _hook_heredocs()}
    assert {"session-start", "post-identity", "post-checkin"} <= names


@pytest.mark.parametrize("path", _hook_scripts(), ids=lambda p: p.name)
def test_hook_script_parses_at_the_floor(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=FLOOR)


@pytest.mark.parametrize(
    "name,body", _hook_heredocs(), ids=[name for name, _ in _hook_heredocs()]
)
def test_hook_heredoc_parses_at_the_floor(name: str, body: str) -> None:
    ast.parse(body, filename=name, feature_version=FLOOR)


def _system_python_version() -> tuple[int, int] | None:
    if not SYSTEM_PYTHON.is_file():
        return None
    try:
        out = subprocess.run(
            [str(SYSTEM_PYTHON), "-c", "import sys; print(*sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    parts = out.stdout.split()
    if out.returncode != 0 or len(parts) != 2:
        return None
    return int(parts[0]), int(parts[1])


def test_scripts_compile_under_the_system_python(tmp_path: Path) -> None:
    """Where /usr/bin/python3 is older than the suite's interpreter (macOS),
    compile each hook script with it as well, since ast's feature_version is
    best effort."""
    version = _system_python_version()
    if version is None or version >= sys.version_info[:2]:
        pytest.skip("no older /usr/bin/python3 to compile with")
    failures = []
    for n, path in enumerate(_hook_scripts()):
        result = subprocess.run(
            [
                str(SYSTEM_PYTHON),
                "-c",
                "import py_compile, sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)",
                str(path),
                str(tmp_path / f"{n}.pyc"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            failures.append(f"{path.name}: {result.stderr.strip().splitlines()[-1:]}")
    assert failures == [], failures

