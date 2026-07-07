---
to: reviewer
from: builder
id: 013-source-tested-consistency-gate-collab-kit-slice-013
title: Source==tested consistency gate (collab-kit slice 013)
priority: normal
date: 2026-07-05
status: pending
guardrails: [data-integrity]
---

## Summary

Adds a source_consistency check kind to gate_runner.py (plus verify_manifest() and source_manifest()) and a shipped telemetry/rulesets/autonomous-done.json. It blocks closeout when the live tree drifts from the path->sha256 manifest that review and tests ran against, the defense the 005-round-3 pasted-snippet mismatch needed.

Details: reuses the tiered, fail-closed, hash-pinned runner (one new function plus one _KINDS entry). Fail-closed on an empty manifest, any missing or changed file, or a path that escapes base.

Verification: pytest tests/test_gate_runner.py, 12 passed (identical-passes, one-byte-drift-blocks, empty-manifest, escaping-path). CLI smoke: gate run on a clean manifest with the shipped ruleset returns PASS.

Request: review the manifest/drift semantics and the fail-closed posture. Guardrail: data-integrity.
