"""The command cache-contract lint keeps workspace caches out of proof guidance.

Workspace slot caches are lineage hints any process can write. Agent-facing
docs (commands/*.md and CODEX_START.md) must not teach an agent to take a
`client_session_id` from them, to pick a slot by recency, or to reset a
milestone without naming the slot.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LINT = ROOT / "scripts" / "dev" / "lint-command-cache-contract.sh"

CLEAN_COMMAND = (
    '- `scripts/session_cache.py list --workspace "$PWD"` inventories slots.\n'
    "- `scripts/session_cache.py get session --slot=<slot>` reads one slot as "
    "lineage context only.\n"
)


def _run_in_tree(
    tmp_path: Path, command_text: str, codex_start: str
) -> subprocess.CompletedProcess:
    lint = tmp_path / "scripts" / "dev" / LINT.name
    lint.parent.mkdir(parents=True)
    shutil.copy2(LINT, lint)
    (tmp_path / "commands").mkdir()
    (tmp_path / "commands" / "sample.md").write_text(command_text, encoding="utf-8")
    (tmp_path / "CODEX_START.md").write_text(codex_start, encoding="utf-8")
    return subprocess.run(
        ["bash", str(lint)], capture_output=True, text=True, cwd=tmp_path, check=False
    )


def test_repository_docs_pass_the_lint():
    result = subprocess.run(
        ["bash", str(LINT)], capture_output=True, text=True, cwd=ROOT, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_clean_tree_passes(tmp_path):
    result = _run_in_tree(tmp_path, CLEAN_COMMAND, "No cache guidance here.\n")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "stale",
    [
        "It is the first place to look for:\n\n- `client_session_id`\n",
        "Read a cache with `scripts/session_cache.py get session --slot=<slot>`.\n",
        "Otherwise glob `.unitares/session-*.json` and pick the most recent by `updated_at`.\n",
        "Then reset it with `scripts/session_cache.py reset-milestone`.\n",
    ],
    ids=[
        "first-place-to-look",
        "slot-read-not-lineage",
        "pick-most-recent",
        "slotless-reset",
    ],
)
def test_codex_start_is_covered_and_stale_guidance_fails(tmp_path, stale):
    result = _run_in_tree(tmp_path, CLEAN_COMMAND, stale)
    assert result.returncode == 1, result.stdout
    assert "CODEX_START.md" in result.stdout


def test_stale_guidance_in_command_file_fails(tmp_path):
    result = _run_in_tree(
        tmp_path,
        CLEAN_COMMAND + "Reset with `scripts/session_cache.py reset-milestone`.\n",
        "No cache guidance here.\n",
    )
    assert result.returncode == 1, result.stdout
    assert "commands/sample.md" in result.stdout


def test_slotted_reset_is_allowed(tmp_path):
    result = _run_in_tree(
        tmp_path,
        CLEAN_COMMAND
        + "Hook runs `scripts/session_cache.py reset-milestone --slot <slot>`.\n",
        "No cache guidance here.\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_single_file_run_parses_file_and_line(tmp_path):
    # grep omits the file name when given one file; the lint must still parse
    # `file:line:content` and apply the lineage rule to that line.
    doc = tmp_path / "one.md"
    doc.write_text(
        "- `scripts/session_cache.py get session --slot=<slot>`: reads your session.\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(LINT), str(doc)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 1, result.stdout
    assert (
        f"{doc}:1 — `session_cache.py get session --slot` not marked lineage-only"
        in result.stdout
    )
    assert "without --slot" not in result.stdout


@pytest.mark.parametrize(
    "unsafe",
    [
        "Read `scripts/session_cache.py get session --slot=<slot>` and use the lineage "
        "cache's `client_session_id` for the next call.\n",
        "`scripts/session_cache.py get session --slot=<slot>` reads one slot as lineage "
        "context only; pass its `client_session_id` on later calls.\n",
    ],
    ids=["lineage-word-only", "marker-plus-session-id"],
)
def test_slot_read_naming_session_id_fails_even_with_lineage(tmp_path, unsafe):
    result = _run_in_tree(tmp_path, CLEAN_COMMAND, unsafe)
    assert result.returncode == 1, result.stdout
    assert (
        "CODEX_START.md:1 — `session_cache.py get session --slot` not marked lineage-only"
        in result.stdout
    )
