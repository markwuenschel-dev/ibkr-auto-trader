---
to: reviewer
from: builder
id: 015-adversarial-lane-runner-verification-ledger-collab-kit-slice-015
title: Adversarial lane runner + verification ledger (collab-kit slice 015)
priority: normal
date: 2026-07-05
status: pending
guardrails: [data-integrity, concurrency, untrusted-agent-output]
---

## Summary

Adds tools/lib/lanes.py and telemetry/lanes.json: the breaker -> independent-verifier pipeline that architecture section 10 described but was never built (the workflow.js never shipped). It writes a verification ledger to <collab>/autopilot/verification/<hid>.ledger.json.

Details: reuses the driver's hardened backend substrate (ap._cli_runner, _sanitize, _write_reply) so there is no new subprocess or isolation code. The verifier defaults to REJECTED unless it cites an exact path and a concrete trigger. Independence is structural: builder (from), breaker, and verifier must be three distinct seats. Confirmed findings become ledger blockers (fixed=false until a builder round resolves them).

Verification: pytest tests/test_lanes.py, 9 passed (clean/confirmed/refuted lanes, independence-violation raises, ledger written, manifest attached); no circular import.

Request: review the independence enforcement and the default-REJECTED verifier. Guardrails: data-integrity, concurrency, untrusted-agent-output.
