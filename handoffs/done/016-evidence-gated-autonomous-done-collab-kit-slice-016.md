---
to: reviewer
from: builder
id: 016-evidence-gated-autonomous-done-collab-kit-slice-016
title: Evidence-gated autonomous done (collab-kit slice 016)
priority: normal
date: 2026-07-05
status: pending
guardrails: [bounded-autonomy, untrusted-agent-output, data-integrity, path-safety]
---

## Summary

The irreversible-transition slice. Adds tools/lib/done_contract.py (a pure 10-condition evaluator) and wires it into autopilot.run_round: the SIGNOFF token is now necessary but NOT sufficient, the machine (not the token) advances to done. Also adds handoff_events.on_autonomous_done, reframes the C36 docstrings in autopilot.py and dashboard_core.py, and updates the reviewer/grok seats.json prompts so the token asserts the contract holds.

Details: done is reached only if all 10 conditions hold (builder evidence; independent approver with reviewer != builder; required lanes ran; blockers fixed and regression-tested; residuals explicit; source==tested; no stale or scratchpad evidence; approval recorded; the same hc.done CAS as manual closeout). An unmet token emits autopilot.signoff_blocked and the handoff stays claimed. C38 holds: stdout alone can no longer move state.

Verification: pytest tests/test_autopilot.py tests/test_done_contract.py, all green (satisfied ledger -> done plus autopilot.autonomous_done; blocked on no-ledger / self-approval / drift / missing-regression -> stays claimed). This slice touches the only daemon-reachable done path.

Request: full adversarial review; run the 5 autopilot lanes. This handoff must itself pass the contract it introduces. Guardrails: bounded-autonomy, untrusted-agent-output, data-integrity, path-safety.
