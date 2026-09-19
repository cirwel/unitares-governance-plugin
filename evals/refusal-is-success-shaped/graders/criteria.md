---
type: llm
weight: 3
---

The function treats a typed identity refusal as NOT going through: it checks
`status` for `identity_required` / `lineage_declaration_required` (and/or
`rollout_flag`), not only `success`.

Fail if the function would return True for a response with
`"success": true, "status": "identity_required"` — e.g. it decides only on
`success`. UNITARES identity refusals carry `success: true`.
