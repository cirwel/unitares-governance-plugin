"""Server entry-point registration for governance plugin aliases."""

from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass


@dataclass
class _ToolAlias:
    old_name: str
    new_name: str
    reason: str
    migration_note: str | None = None
    inject_action: str | None = None
    param_normalizer: object | None = None
    experience: bool = False


def _install_fake_governance_modules(monkeypatch, existing=None):
    calls = []

    src = types.ModuleType("src")
    src.__path__ = []
    mcp_handlers = types.ModuleType("src.mcp_handlers")
    mcp_handlers.__path__ = []
    support = types.ModuleType("src.mcp_handlers.support")
    support.__path__ = []

    tool_stability = types.ModuleType("src.mcp_handlers.tool_stability")
    tool_stability.ToolAlias = _ToolAlias
    tool_stability.list_all_aliases = lambda: dict(existing or {})
    tool_stability.register_extra_aliases = lambda aliases: calls.append(aliases)

    param_normalization = types.ModuleType(
        "src.mcp_handlers.support.param_normalization"
    )
    param_normalization.normalize_unit_interval = lambda name: ("normalizer", name)

    for name, module in {
        "src": src,
        "src.mcp_handlers": mcp_handlers,
        "src.mcp_handlers.support": support,
        "src.mcp_handlers.tool_stability": tool_stability,
        "src.mcp_handlers.support.param_normalization": param_normalization,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.delitem(
        sys.modules, "unitares_governance_plugin.aliases", raising=False
    )
    return calls


def test_register_adds_sync_state_alias_when_server_lacks_it(monkeypatch):
    calls = _install_fake_governance_modules(monkeypatch)

    import unitares_governance_plugin

    importlib.reload(unitares_governance_plugin)
    unitares_governance_plugin.register()

    assert len(calls) == 1
    alias = calls[0]["sync_state"]
    assert alias.old_name == "sync_state"
    assert alias.new_name == "process_agent_update"
    assert alias.reason == "intuitive_alias"
    assert alias.param_normalizer == ("normalizer", "complexity")
    assert alias.experience is True


def test_register_skips_sync_state_when_server_fallback_exists(monkeypatch):
    calls = _install_fake_governance_modules(
        monkeypatch, existing={"sync_state": object()}
    )

    import unitares_governance_plugin

    importlib.reload(unitares_governance_plugin)
    unitares_governance_plugin.register()

    assert calls == []
