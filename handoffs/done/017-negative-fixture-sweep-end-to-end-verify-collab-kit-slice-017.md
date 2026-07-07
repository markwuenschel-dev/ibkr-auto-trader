---
to: reviewer
from: builder
id: 017-negative-fixture-sweep-end-to-end-verify-collab-kit-slice-017
title: Negative-fixture sweep + end-to-end verify (collab-kit slice 017)
priority: normal
date: 2026-07-05
status: pending
guardrails: [bounded-autonomy, untrusted-agent-output, data-integrity, path-safety]
---

## Summary

Negative fixtures for every safety claim, plus a full-suite green run and an end-to-end autonomous-done dry run.

Details: new fixtures cover source/test drift; self-approval (reviewer==builder) refused; missing-required-lane; blocker-without-regression; tests-not-passed; stale-scratchpad evidence; and bundle pointer-escape. Existing lane coverage is reused (untrusted-output forging, backend caps/timeout/no-shell, escaping-pointer, max-rounds cap, single-winner claim CAS).

Verification: full suite 234 passed, 6 skipped, 23 subtests. End-to-end on a throwaway collab: lanes ran clean -> contract satisfied -> driver reached done with a full audit trail (handoff.autonomous_done plus the contract hash); a self-approval attempt blocked specifically on the independent-approver condition and stayed claimed.

Request: confirm coverage completeness (a negative fixture per safety claim) before sign-off. Guardrails: bounded-autonomy, untrusted-agent-output, data-integrity, path-safety.
