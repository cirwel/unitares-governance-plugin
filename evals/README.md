# Plugin evals

Behavioural evals for the skills and commands, run with `claude plugin eval`.
The pytest suite covers the hooks. These cases cover what a model does once
the skills are loaded, with a no-plugin baseline arm for comparison.

```bash
claude plugin eval . --trust-plugin -j 4
claude plugin eval . --case identity-fresh-session --runs 1   # one case
```

`--case` keeps only its last value; use `--tag` to select a group.

## Live-server safety

The plugin's hooks run inside the eval child, outside the sandbox, and the
eval strips every environment variable except `EVAL_*`. Without a guard the
hooks would reach `localhost:8767` and lazily onboard a new identity on every
run. Each case therefore sets:

```yaml
env:
  EVAL_UNITARES_OFFLINE: "1"
```

`hooks/run-hook.cmd` maps that flag to a closed port for the governance
server, lease plane and sidecar, and disables auto-onboard. **Every new case
must carry it.** The plugin's MCP server is not started either: under the
default `--mocks record` a server with no mock under `evals/mocks/` stays off.
The cases therefore ask the model to write the call out rather than make it.

## Cases

| Case | Checks |
|---|---|
| `identity-fresh-session` | `start_session(force_new=true)` with no parent when a neighbour's slot file exists |
| `identity-deliberate-handoff` | lineage from an exited predecessor uses `spawn_reason="explicit"` |
| `refusal-is-success-shaped` | a client treats `success: true` + `status: identity_required` as not gone through |
| `checkin-confidence-optional` | `confidence` is omitted rather than filled with a habitual number |
| `verdict-cold-start-reading` | a provisional `proceed` is not read as a quality verdict |

`checkin-confidence-optional` and `verdict-cold-start-reading` also pass at or
near 1.0 without the plugin, so they guard against regressions but do not
show the skills helping.
