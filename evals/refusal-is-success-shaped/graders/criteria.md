---
type: llm
weight: 3
---

The function returns False for a response carrying `"success": true` together
with `"status": "identity_required"` (or `lineage_declaration_required`).
UNITARES identity refusals are shaped this way.

Pass either style: checking `status` / `rollout_flag` for the refusal values,
or an allowlist that only treats known-good responses as having gone
through, provided the reasoning or code makes clear that a `success: true`
refusal returns False.

Fail if the function would return True for that response, e.g. because it
decides on `success` alone.
