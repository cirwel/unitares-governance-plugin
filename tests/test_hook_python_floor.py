"""Hook Python must stay runnable on the interpreter hooks actually get.

`hooks/run-hook.cmd` resets PATH to /usr/bin:/bin:/usr/sbin:/sbin, so on macOS
every hook runs /usr/bin/python3 from the Command Line Tools, which is Python
3.9, whatever else is installed (#453). The test suite runs on 3.12, so
nothing caught syntax the hooks' interpreter cannot parse. These tests pin
Python 3.9 as the syntax floor for every script a hook can run and every
quoted Python heredoc in the hook scripts, and flag a python3 heredoc the scan
cannot read.

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

# A heredoc whose delimiter is quoted ('PY' or "PY") is passed to Python
# verbatim. With `<<-` the shell also strips leading tabs from each body line
# and from the terminator, so the scan strips them too before parsing. A
# comment line that quotes the pattern does not start one.
#
# Shell forms the scan reads: one python3 heredoc per command, whose opener may
# span physical lines joined by backslash-newline (the body starts after the
# last of them). `<<` is matched only with no `<` on either side, so a `<<<`
# here-string is not taken for a heredoc. Not modelled: several heredocs in one
# command, a python3 command continued across a line ending in `|` or `&&`
# without a backslash, and backslash-newline inside single quotes (where the
# shell keeps it literally).
_HEREDOC_OP = r"(?<!<)<<(?!<)"
_COMMENT_LINE = re.compile(r"^[ \t]*#")
_HEREDOC_OPEN = re.compile(
    r"^(?![ \t]*#).*\bpython3\b.*?" + _HEREDOC_OP
    + r"""(?P<dash>-?)[ \t]*(?P<q>['"])(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=q)"""
)
# Any python3 heredoc at all, quoted or not, so an opener the scan above would
# miss (an unquoted delimiter lets the shell expand the body) is flagged.
_ANY_PYTHON_HEREDOC = re.compile(r"^(?![ \t]*#).*\bpython3\b.*?" + _HEREDOC_OP)


def _continues(line: str) -> bool:
    """A line ending in an odd number of backslashes continues the command."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _extract_heredocs(text: str) -> tuple[list[str], list[str]]:
    """Return (bodies, problems) for the python3 heredocs in a shell script."""
    lines = text.split("\n")
    bodies: list[str] = []
    problems: list[str] = []
    i = 0
    while i < len(lines):
        # A full-line comment ends at its newline, even after a trailing
        # backslash, so it is skipped before any continuation join.
        if _COMMENT_LINE.match(lines[i]):
            i += 1
            continue
        # Join backslash-newline continuations into one logical command line.
        first = i
        logical = lines[i]
        while _continues(logical) and i + 1 < len(lines):
            i += 1
            logical = logical[:-1] + lines[i]
        opener = _HEREDOC_OPEN.match(logical)
        if opener is None:
            if _ANY_PYTHON_HEREDOC.match(logical):
                problems.append(
                    f"line {first + 1}: python3 heredoc without a quoted delimiter"
                )
            i += 1
            continue
        strip_tabs = opener.group("dash") == "-"
        tag = opener.group("tag")
        body: list[str] = []
        j = i + 1
        while j < len(lines):
            candidate = lines[j].lstrip("\t") if strip_tabs else lines[j]
            if candidate == tag:
                break
            body.append(candidate)
            j += 1
        else:
            problems.append(f"line {first + 1}: heredoc {tag!r} is never terminated")
        bodies.append("\n".join(body) + "\n")
        i = j + 1
    return bodies, problems


def _hook_scripts() -> list[Path]:
    scripts = sorted((ROOT / "scripts").glob("*.py"))
    scripts.append(ROOT / "scripts" / "u-run")
    return scripts


def _hook_files() -> list[Path]:
    return [
        hook
        for hook in sorted((ROOT / "hooks").iterdir())
        if hook.is_file() and hook.suffix != ".json"
    ]


def _hook_heredocs() -> list[tuple[str, str]]:
    blocks = []
    for hook in _hook_files():
        bodies, _ = _extract_heredocs(hook.read_text(encoding="utf-8"))
        blocks.extend((f"{hook.name}#{n}", body) for n, body in enumerate(bodies))
    return blocks


def test_the_heredoc_scan_finds_the_hook_python() -> None:
    names = {name.split("#")[0] for name, _ in _hook_heredocs()}
    assert {"session-start", "post-identity", "post-checkin"} <= names


def test_every_python_heredoc_in_the_hooks_is_scanned() -> None:
    problems = []
    for hook in _hook_files():
        _, found = _extract_heredocs(hook.read_text(encoding="utf-8"))
        problems.extend(f"{hook.name} {p}" for p in found)
    assert problems == [], problems


def test_the_scan_reads_quoted_tab_stripping_heredocs() -> None:
    text = (
        "x=$(python3 - <<-'PY'\n"
        "\timport sys\n"
        "\tif True:\n"
        "\t    print(sys.argv)\n"
        "\tPY\n"
        ")\n"
        'y=$(python3 - <<"END"\n'
        "print(1)\n"
        "END\n"
        ")\n"
    )
    bodies, problems = _extract_heredocs(text)
    assert problems == []
    assert bodies == ["import sys\nif True:\n    print(sys.argv)\n", "print(1)\n"]
    for body in bodies:
        ast.parse(body, feature_version=FLOOR)


def test_the_scan_follows_backslash_newline_continuations() -> None:
    text = (
        'out=$(python3 - "${A}" \\\n'
        '  "${B}" \\\n'
        "  <<'PY' 2>/dev/null || true\n"
        "import sys\n"
        "PY\n"
        ")\n"
        "python3 - \\\n"
        "  <<PY\n"
        "print(1)\n"
        "PY\n"
    )
    bodies, problems = _extract_heredocs(text)
    assert bodies == ["import sys\n"]
    assert problems == ["line 7: python3 heredoc without a quoted delimiter"]


def test_an_escaped_backslash_does_not_continue_the_line() -> None:
    bodies, problems = _extract_heredocs("python3 - x\\\\\n<<'PY'\nprint(1)\nPY\n")
    assert bodies == [] and problems == []


def test_a_comment_ending_in_a_backslash_does_not_continue() -> None:
    text = "# see notes \\\npython3 - <<'PY'\nprint(1)\nPY\n"
    bodies, problems = _extract_heredocs(text)
    assert problems == []
    assert bodies == ["print(1)\n"]


def test_a_here_string_is_not_a_heredoc() -> None:
    text = (
        "python3 - <<<\"${x}\"\n"
        "python3 - <<<'print(1)'\n"
        "read -r a b <<<\"${line}\"\n"
        "python3 - <<<\"${x}\" <<'PY'\n"
        "print(2)\n"
        "PY\n"
    )
    bodies, problems = _extract_heredocs(text)
    assert problems == []
    assert bodies == ["print(2)\n"]


def test_the_scan_flags_unquoted_and_unterminated_heredocs() -> None:
    _, problems = _extract_heredocs("python3 - <<PY\nprint(1)\nPY\n")
    assert problems and "without a quoted delimiter" in problems[0]
    _, problems = _extract_heredocs("python3 - <<'PY'\nprint(1)\n")
    assert problems and "never terminated" in problems[0]


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


def _system_python_failures(sources: list[tuple[str, Path]], tmp_path: Path) -> list[str]:
    failures = []
    for n, (name, path) in enumerate(sources):
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
            failures.append(f"{name}: {result.stderr.strip().splitlines()[-1:]}")
    return failures


def _require_older_system_python() -> None:
    version = _system_python_version()
    if version is None or version >= sys.version_info[:2]:
        pytest.skip("no older /usr/bin/python3 to compile with")


def test_scripts_compile_under_the_system_python(tmp_path: Path) -> None:
    """Where /usr/bin/python3 is older than the suite's interpreter (macOS),
    compile each hook script with it as well, since ast's feature_version is
    best effort."""
    _require_older_system_python()
    sources = [(path.name, path) for path in _hook_scripts()]
    assert _system_python_failures(sources, tmp_path) == []


def test_heredocs_compile_under_the_system_python(tmp_path: Path) -> None:
    """The same check for the Python each hook passes on stdin."""
    _require_older_system_python()
    sources = []
    for n, (name, body) in enumerate(_hook_heredocs()):
        path = tmp_path / f"heredoc_{n}.py"
        path.write_text(body, encoding="utf-8")
        sources.append((name, path))
    assert sources
    assert _system_python_failures(sources, tmp_path) == []
