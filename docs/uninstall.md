# Uninstall and clean up

Removing the plugin from a host stops its hooks and its bundled MCP transport.
It does not touch the governance server: identities, check-ins and knowledge
graph entries the plugin recorded stay in the server's records. Files the hooks
wrote on your machine also stay until you remove them.

## Remove the plugin from each host

**Claude Code.** Run inside Claude Code:

```text
/plugin uninstall unitares-governance@unitares-governance
/plugin marketplace remove unitares-governance
```

From a terminal, `claude plugin uninstall unitares-governance@unitares-governance`
does the same. Start a new session afterwards.

If you followed [Server connections](configuration.md#server-connections) and
registered a separate authenticated server with Claude, uninstalling the plugin
leaves that registration and its bearer header in place. Remove it under the
name you registered it with, which `claude mcp list` shows:

```bash
claude mcp list
claude mcp remove <name>            # add --scope local|user|project if it is in several
```

Then unset `UNITARES_HTTP_API_TOKEN` if nothing else uses it (see
[Remove local state](#remove-local-state)).

**Codex.**

```bash
codex plugin remove unitares-governance@unitares-governance
codex plugin marketplace remove unitares-governance
```

If you followed [Server connections](configuration.md#server-connections) and
registered your own server, remove that too, along with the table that
disabled the bundled transport in `~/.codex/config.toml`:

```bash
codex mcp remove unitares-governance
```

```toml
# delete from ~/.codex/config.toml
[plugins."unitares-governance@unitares-governance".mcp_servers.unitares-governance]
enabled = false
```

**Antigravity.** `scripts/install_agy_hooks.py` has no uninstall mode. Delete
the top-level `"unitares-governance"` entry from `~/.gemini/config/hooks.json`
and leave the other hook groups in place. Then remove the plugin checkout the
entries pointed at, if you no longer need it.

## Remove local state

These are left behind on purpose, because the plugin cannot tell whether
another host or another UNITARES tool still uses them.

- **`<project>/.unitares/`** in each project where a host ran with the plugin.
  It holds session caches and markers, and carries `client_session_id` values.
  It ignores itself through its own `.gitignore`; delete the directory when the
  plugin is gone. If you committed it before that `.gitignore` existed, also
  remove it from the index with `git rm -r --cached .unitares`.
- **`~/.unitares/`** holds the hook logs (`checkins.log`, `hook-skips.log`,
  `session-start.log`), `hook-reports/` (or `UNITARES_HOOK_REPORT_DIR`),
  `skills-cache/` and `file-lease-state/`. Other UNITARES tools, including a
  server run on the same machine, can write here too. Delete the whole
  directory only if this plugin was the only UNITARES software on the machine;
  otherwise remove just the entries named above.
- **`~/.config/unitares/secrets.env`** (or the older
  `~/.config/cirwel/secrets.env`), if you created it only for the plugin's
  lease-plane bearer.
- **Environment variables** you exported for the plugin, such as
  `UNITARES_SERVER_URL`, `UNITARES_HTTP_API_TOKEN`, `LEASE_PLANE_BASE_URL`,
  `LEASE_PLANE_BEARER_TOKEN` and
  `UNITARES_FILE_LEASES_REQUIRED`, in your shell profile.

A file lease an edit held when you uninstalled expires on its own after
`UNITARES_FILE_LEASE_TTL_S` (30 seconds by default), so nothing needs to be
released by hand.
