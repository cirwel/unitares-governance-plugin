"""Tool aliases contributed by unitares-governance-plugin."""

from __future__ import annotations

from src.mcp_handlers.support.param_normalization import normalize_unit_interval
from src.mcp_handlers.tool_stability import ToolAlias


_CHECKIN_COMPLEXITY_NORMALIZER = normalize_unit_interval("complexity")

GOVERNANCE_ALIASES = {
    "sync_state": ToolAlias(
        old_name="sync_state",
        new_name="process_agent_update",
        reason="intuitive_alias",
        migration_note=(
            "Resolves to process_agent_update() - check in your working state"
        ),
        param_normalizer=_CHECKIN_COMPLEXITY_NORMALIZER,
        experience=True,
    ),
}
