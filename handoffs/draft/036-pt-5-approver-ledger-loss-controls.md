---
to: builder
from: reviewer
id: 036-pt-5-approver-ledger-loss-controls
title: PT-5 — Risk Approver + 9-rule ledger + two loss controls
priority: normal
date: 2026-07-11
status: pending
guardrails: [money, safety, data-integrity, bounded-autonomy]
depends_on: [030, 031, 034, 035]
adr: docs/design/adr/0003-pt5-rules-ledger.md
---

## Summary

Implement the **Risk Approver**, the **9-rule ledger**, and the **two loss controls**, per **ADR-0003
Decisions 2/6/7/8/9/10** (lanes L6/L7). The approver recomputes a `VerifiedProjection` (via the 035
projector) and runs pure-predicate rules over `RiskEvaluation`; verdict is `APPROVED | REJECTED` only.
**Follow ADR-0003 exactly.** STAGED DRAFT — the largest slice; keep it under review.

**Blocked by:** 030 (RiskPolicy/RiskControlState), 031 (`REDUCE_ONLY` primitive), 034 (E0 + Eastern
`session_date`), 035 (projector + planner).

## Deliverables

- `src/ibkr_trader/risk/evaluation.py`: `RiskEvaluation(plan, context, control_state, verified_projection)`,
  `RuleResult(id, passed, severity, evidence)`, `ApprovalVerdict(APPROVED|REJECTED)`,
  `ApprovalDecision(verdict, plan_digest, context_digest, rule_results, audit_ref, approved_order?)`.
- `src/ibkr_trader/risk/ledger.py`: the **9 active rules**, each a pure predicate over `RiskEvaluation`:
  `max-risk-per-trade`, `daily-realized-opening-lockout` (Control 1), `buying-power` (incremental debit),
  `leverage-cap` (fail-closed unpriced), `stop-loss-required`, `causal-data-only` (belt; breach ⇒ reject +
  **alarm**), `unvalued-holding reduce-only`, `concentration` (**target-weight fallback**; reject-as-
  unconfigured if the strategy declares no weights), `maintenance-margin` (**degraded current-headroom**,
  long-equity-bounded, over-rejects).
- `src/ibkr_trader/risk/approver.py`: `RiskApprover.approve(plan, context, control_state) -> ApprovalDecision`.
  **Evaluate all rules** (no short-circuit) for a full audit; a planner-vs-verified mismatch ⇒ `REJECTED` +
  alarm; the `APPROVED` mint of `ApprovedOrderIntent` is **contingent on a durable audit write** (037
  interface — ship a stubbed `AuditSink` here, 037 implements).
- `src/ibkr_trader/risk/loss_controls.py`: **Control 1** `realized_daily_pnl ≤ −pct_a·E0 ⇒ REDUCE_ONLY`
  (latch via 031); **Control 2** `(NLV−E0)/E0 ≤ −pct_d` — **detect + alarm active, enforcement DARK** until
  `pct_d` is calibrated — `REDUCE_ONLY` + escalate to PT-13 once armed. Stateless-recompute.
- `src/ibkr_trader/risk/sizing.py`: the `RiskSizing.decide(intent, context, control_state)` facade
  (Planner → Approver).
- Telemetry: `risk.decide`/`risk.rule`/`risk.projection_mismatch`/`risk.e0`/`risk.realized_lockout`/
  `risk.drawdown`/`risk.reduce_only`/`risk.causal_breach`/`risk.snapshot_incomplete`/`risk.unpriced_holding`.
- `tests/test_ledger.py` / `tests/test_approver.py` / `tests/test_loss_controls.py` / `tests/test_sizing.py`.

## The contract (ADR-0003 D2/6/7/8/9/10)

1. **Recompute-for-authority.** Rules read the `VerifiedProjection`, never `planner_projection`. Mismatch =
   hard reject + alarm, never silent repair.
2. **Binary verdict; no approver-side sizing.** No `REDUCED`/`REPLAN`/quantity-hint. `PAUSED` is not a
   verdict.
3. **Denominators:** E0 for daily-loss; current NLV for leverage + max-risk.
4. **Two controls, one shared E0**, `pct_a`/`pct_d` independent (unequal defaults); Control 2 dark.
5. **Reject-as-unconfigured** for a policy-blocked rule; **degraded rules over-reject**, never approximate.
6. **Mint contingent on durable audit** — a durable-audit-write failure mints nothing.

## Definition of done — test surface

- Max-risk / concentration / leverage violation ⇒ `REJECTED`, **no second plan**.
- Projection mismatch ⇒ `REJECTED` + `risk.projection_mismatch`.
- Control 1: `realized ≤ −pct_a·E0` ⇒ `REDUCE_ONLY`; strict exit still submits; **absent E0 ⇒ fail closed**.
- Control 2: breach ⇒ detect + alarm (dark); once armed ⇒ `REDUCE_ONLY` + escalate; recompute restart-stable.
- Unvalued holding / latched session ⇒ strict-reduce-only (no open/increase/zero-cross).
- Buying-power = incremental debit; causal-breach ⇒ reject + alarm; concentration w/o weights ⇒
  reject-as-unconfigured; margin over-rejects for shorts/options (live-blocked).
- Durable-audit-write failure mints no `ApprovedOrderIntent`; direct mint raises.
- Eastern/DST daily-P&L boundary; `model_construct` bypass exercised.
- `ruff` + `pyright` green; full suite green.
