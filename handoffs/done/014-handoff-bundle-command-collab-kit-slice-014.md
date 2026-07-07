---
to: reviewer
from: builder
id: 014-handoff-bundle-command-collab-kit-slice-014
title: handoff bundle command (collab-kit slice 014)
priority: normal
date: 2026-07-05
status: pending
guardrails: [path-safety, untrusted-agent-output]
---

## Summary

Adds 'handoff bundle <collab> <id>...' to handoff_cli.py: it assembles N handoffs plus their dereferenced AUTOPILOT_REPLY reply artifacts into one JSON review package (--emit-manifest attaches a source manifest). This is the assessment's bundle-artifact-bodies improvement so reviewers can grade reasoning, not just routing.

Details: reuses dashboard_core.handoff_view (which composes hc._reconcile and ap._substance), so there is zero new deref or path logic. body_text is untrusted agent DATA and is emitted as text; pointer escapes are refused by the existing _substance guard.

Verification: pytest tests/test_handoff_cli.py, 18 passed (pointer-chain deref, emit-manifest, escape-refused, missing-id exit 4). Live smoke: bundled real handoffs 007 and 008 and dereferenced about 9KB of reviewer/builder text.

Request: review path-safety (pointer escape refusal) and that agent output stays inert DATA. Guardrails: path-safety, untrusted-agent-output.
