---
to: builder
from: reviewer
id: 037-pt-7-durable-decision-audit
title: PT-7 — durable decision audit (approve and reject)
priority: normal
date: 2026-07-11
status: pending
guardrails: [audit-completeness, data-integrity, safety]
depends_on: [036]
adr: docs/design/adr/0003-pt5-rules-ledger.md
---

## Summary

Implement the **durable decision audit** the PT-5 mint depends on, per **ADR-0003 Decision 8 / lane L9**
and `trading-system-design.md` §6.2. Every decision — **approve and reject** — writes a structured JSON
record **and** a human-readable line; the `ApprovedOrderIntent` mint is contingent on this write
succeeding. **Follow ADR-0003 exactly.** STAGED DRAFT.

**Blocked by:** 036 (implements the `AuditSink` interface 036 stubs; consumes `ApprovalDecision`).

## Deliverables

- `src/ibkr_trader/audit/sink.py` (new): the durable `AuditSink` — structured JSON record (verdict, plan/
  context digests, every `RuleResult`, `policy.version`, `VerifiedProjection`, correlation id) **+** a
  human line (*"At T, NLV X, drift on QQQ Y → REJECTED: leverage-cap … "*). Durable-write semantics: a
  failed write **raises** so the approver mints nothing.
- Wire `RiskApprover` (036) so the mint occurs **only after** a successful durable audit write.
- `tests/test_audit.py`.

## The contract (ADR-0003 D8; design §6.2)

1. **Rejections carry equal audit weight** to approvals — both fully recorded.
2. **Mint-contingent** — `audit-completeness` is not a ledger predicate; the durable write *is* the gate.
   A write failure ⇒ no `ApprovedOrderIntent`.
3. **No PII**; best-effort telemetry is separate from this durable record (this one must not be dropped).

## Definition of done

- An approve and a reject each produce a complete JSON record + human line.
- A simulated durable-write failure ⇒ the approver mints **no** `ApprovedOrderIntent` (fail-closed).
- The record contains every field needed to reconstruct the decision (digests, rule results,
  `policy.version`, projection).
- `ruff` + `pyright` green; full suite green.
