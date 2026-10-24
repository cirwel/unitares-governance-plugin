# ChatGPT package

UNITARES can run in ChatGPT through a hook-free portable plugin. The generated
package reuses the repository's canonical skills and either references an MCP
app already registered in ChatGPT or connects directly to a hosted UNITARES
endpoint with OAuth Dynamic Client Registration (DCR).

The generated package never contains credentials, lifecycle hooks, hook
scripts, or skill attestations. Web installations therefore use UNITARES's
explicit workflow: `start_session(force_new=true)` once for a fresh
conversation process, then meaningful milestone check-ins with the returned
`client_session_id`.

## Registered app package

Use this route for a personal or workspace plugin:

1. In ChatGPT Plugins, select **Add custom MCP server** and connect the
   UNITARES endpoint.
2. Copy the connection's technical app ID from its browser URL. It begins with
   `plugin_asdk_app_`.
3. Build the portable package outside the checkout:

   ```bash
   python3 scripts/dev/build-chatgpt-plugin.py \
     --app-id plugin_asdk_app_REPLACE_ME \
     --output /absolute/path/to/unitares-chatgpt \
     --archive /absolute/path/to/unitares-chatgpt.zip
   ```

The result contains a portable `plugin.json`, `.app.json`, the setup workflow,
and the canonical UNITARES skills. Install or upload it through the ChatGPT
plugin flow appropriate to the workspace.

## Direct hosted package

Use this route when the package itself should declare the remote MCP server:

```bash
python3 scripts/dev/build-chatgpt-plugin.py \
  --server-url https://gov.example.org/mcp/ \
  --output /absolute/path/to/unitares-chatgpt \
  --archive /absolute/path/to/unitares-chatgpt.zip
```

The endpoint must be HTTPS, must end in `/mcp` or `/mcp/`, and must expose the
UNITARES OAuth protected-resource discovery and DCR flow. The generated
`mcp.json` contains only the schema-valid streamable-HTTP transport and URL;
ChatGPT discovers authentication from the server, and no credential enters the
package. See the UNITARES server's
[`MCP_CLIENTS.md`](https://github.com/cirwel/unitares/blob/master/docs/integration/MCP_CLIENTS.md)
for the OAuth listener, Host/Origin allowlist, and registration-closure
posture.

A Secure MCP Tunnel is suitable for private testing. Public plugin submission
requires a stable, publicly reachable HTTPS MCP endpoint and the normal OpenAI
review process.

## Validate

Run the package contract tests before installing a generated bundle:

```bash
python3 -m pytest tests/test_chatgpt_plugin_package.py
```

The tests cover both connection modes, reject insecure URLs and credentials in
URLs, preserve existing output directories, and verify that the bundle remains
hook-free.
