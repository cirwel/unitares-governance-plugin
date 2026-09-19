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

`hooks/run-hook.cmd` maps that flag, on both its Unix and Windows branches,
to the plugin's kill switches (`UNITARES_CHECKINS=off`, auto-onboard off,
`UNITARES_FILE_LEASES_ENABLED=0`) plus a closed port for every endpoint
(governance server, lease plane under both variable names, sidecar).
`tests/test_run_hook_eval_offline.py` checks the values the Python helpers
actually resolve after `config/defaults.env` is sourced, that every case
carries the flag, and that every Claude hook goes through the dispatcher.
The plugin's MCP server is not started either: under the default
`--mocks record` a server with no mock under `evals/mocks/` stays off. The
cases therefore ask the model to write the call out rather than make it.

The gate has a cost: session-start takes its offline branch, so the
hook-delivered guidance (the workspace lineage hint, KG recall) never reaches
the model. These cases test the skills, not the hook context.

## Cases

| Case | Checks |
|---|---|
| `identity-fresh-session` | `start_session(force_new=true)` with no parent when a neighbour's slot file exists |
| `identity-deliberate-handoff` | lineage from an exited predecessor uses `spawn_reason="explicit"` |
| `refusal-is-success-shaped` | a client treats `success: true` + `status: identity_required` as not gone through |
| `checkin-confidence-optional` | `confidence` is omitted rather than filled with a habitual number |
| `verdict-cold-start-reading` | a provisional `proceed` is not read as a quality verdict |

`checkin-confidence-optional` and `verdict-cold-start-reading` also pass at or
near 1.0 without the plugin. The model's own prior carries them, so as written
they would not catch a skill regression either; both need rewriting (the
prompts give away the answer).

The scores in PR #140 were recorded against the skills mirror before #141
re-synced it, and some prompts and graders were edited after their last run.
Re-run on current master with `--runs 5` or more before quoting a delta.
