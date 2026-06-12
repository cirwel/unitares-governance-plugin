"""Server-side registration hooks for unitares-governance-plugin.

The Codex/Claude plugin assets in this repo are client-facing. When this
package is also installed into a governance-mcp server environment, the server
discovers ``register`` through the ``governance_mcp.plugins`` entry point.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register() -> None:
    """Register governance plugin aliases with the UNITARES server.

    Current servers still carry a built-in ``sync_state`` fallback. Skip aliases
    that are already present so this plugin can be installed before the final
    server-side relocation without causing an alias conflict.
    """
    try:
        from .aliases import GOVERNANCE_ALIASES
        from src.mcp_handlers.tool_stability import (
            list_all_aliases,
            register_extra_aliases,
        )
    except ImportError:
        logger.warning(
            "governance lacks alias registration hooks; governance plugin "
            "aliases will not be registered"
        )
        return

    existing = list_all_aliases()
    missing = {
        name: alias
        for name, alias in GOVERNANCE_ALIASES.items()
        if name not in existing
    }
    if not missing:
        logger.info("[GOVERNANCE-PLUGIN] Governance aliases already registered")
        return

    register_extra_aliases(missing)
    logger.info("[GOVERNANCE-PLUGIN] Registered %d governance aliases", len(missing))
