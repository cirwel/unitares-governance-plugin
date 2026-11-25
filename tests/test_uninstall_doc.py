"""docs/uninstall.md must name what the manifests and hooks actually install.

#453 found no uninstall or cleanup section. The doc names plugin and
marketplace ids, the Codex transport table from docs/configuration.md, the
Antigravity hook group and the state paths the hooks use, so each is pinned to
its source here. Only path names are compared; no file they name is read.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "uninstall.md"


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def test_docs_index_links_the_uninstall_doc() -> None:
    assert "(./uninstall.md)" in (ROOT / "docs" / "README.md").read_text(encoding="utf-8")


def test_plugin_and_marketplace_ids_match_the_manifests() -> None:
    claude = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    codex = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
    text = _doc()
    for market in (claude, codex):
        plugin = market["plugins"][0]["name"]
        assert f"{plugin}@{market['name']}" in text
        assert f"marketplace remove {market['name']}" in text


def test_codex_transport_table_matches_server_connections() -> None:
    table = (
        '[plugins."unitares-governance@unitares-governance".mcp_servers.'
        "unitares-governance]"
    )
    assert table in (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    assert table in _doc()


def test_antigravity_hook_group_matches_the_installer() -> None:
    spec = importlib.util.spec_from_file_location(
        "install_agy_hooks_for_doc", ROOT / "scripts" / "install_agy_hooks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert f'"{module.HOOK_GROUP_KEY}"' in _doc()
    assert "~/.gemini/config/hooks.json" in _doc()


def test_named_state_paths_are_ones_the_hooks_use() -> None:
    sources = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in [*(ROOT / "scripts").glob("*.py"), *(ROOT / "hooks").iterdir()]
        if p.is_file()
    )
    text = _doc()
    for name in (
        "checkins.log",
        "hook-skips.log",
        "session-start.log",
        "hook-reports",
        "skills-cache",
        "file-lease-state",
        "UNITARES_HOOK_REPORT_DIR",
        ".config/unitares/secrets.env",
        ".config/cirwel/secrets.env",
    ):
        assert name in text, name
        assert name in sources, f"{name} is not used by any hook or helper"
