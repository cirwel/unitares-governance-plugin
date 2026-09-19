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
| `identity-fresh-session` | a same-repo, same-branch neighbour of unknown liveness is not declared as parent |
| `identity-deliberate-handoff` | lineage from an exited predecessor uses `spawn_reason="explicit"` |
| `identity-live-predecessor` | a still-running session is a sibling, never a parent |
| `governance-start-command` | `/governance-start` with another process's slot cache mints fresh (scaffolded; needs `--scaffold`) |
| `checkin-command-confidence` | `/checkin` does not fill `confidence` with a habitual number (regex grader) |
| `refusal-is-success-shaped` | a client treats `success: true` + `status: identity_required` as not gone through |
| `verdict-cold-start-reading` | an unbaselined `proceed` is not read as a quality verdict |

The two command cases were checked against the commands they replaced: the
old `/governance-start` scored 0.11 and the old `/checkin` 0.08, against 1.00
for the current text. Both identity prompts give both arms the call
signatures, so the no-plugin arm is judged on reasoning, not vocabulary.

Run the full suite with `--scaffold` so the command case gets its slot cache:

```bash
claude plugin eval . --trust-plugin --scaffold -j 4 --runs 5
```
