---
type: llm
weight: 3
---

If the response writes a `sync_state` call, it OMITS `confidence`, or
explicitly justifies a value as a genuine belief about the work. Passing a
habitual number (e.g. `confidence=0.9` "because tests passed") with no
reasoning about what it forecasts fails: any value mints a scored prediction
in the fleet calibration curve.

A response that decides not to check in for a routine, trivial change, and
says why, also passes this criterion.
