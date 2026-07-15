---
to: builder
from: reviewer
id: 035-pt-6-planner-portfolioprojector
title: PT-6 — RiskPlanner + PortfolioProjector (VerifiedProjection)
priority: normal
date: 2026-07-11
status: pending
guardrails: [money, data-integrity, safety]
depends_on: [029, 030, 033]
adr: docs/design/adr/0003-pt5-rules-ledger.md
---

## Summary

Implement the **`PortfolioProjector`** (one deep, pure module → `VerifiedProjection`) and the
**`RiskPlanner`**, per **ADR-0003 Decision 1** (lanes L4/L5). The planner owns all reduction/rounding/
decline and emits a **non-authoritative** `planner_projection`; the projector is what the approver (036)
re-runs for authority. **Follow ADR-0003 exactly.** STAGED DRAFT.

**Blocked by:** 029 (holdings RiskContext), 030 (RiskPolicy/RiskControlState), 033 (a real sealed context
to project against).

## Deliverables

- `src/ibkr_trader/risk/projector.py` (new): `PortfolioProjector.project(order_terms, context,
  control_state) -> VerifiedProjection`. `VerifiedProjection` = notional, **incremental buying-power
  debit** (not naïve notional), resulting **gross leverage** over `Σ abs(broker_market_value)`, resulting
  **maintenance headroom** (conservative current-state; degraded), resulting **concentration**,
  **max_loss_if_stopped**. **Fail closed on any unpriced/`UNAVAILABLE` holding** (a leverage claim over
  empty prices would fail *open*).
- `src/ibkr_trader/risk/planner.py` (new): `RiskPlanner.plan(intent, context, control_state) -> RiskPlan`.
  Uses the projector to select a candidate; may **reduce size / round lots / decline to plan**; emits
  `RiskPlan` carrying `stop_price`, `est_risk_amount`, the **non-authoritative** `planner_projection`, and
  binds `context_digest` + decision/session generation + `policy.version`.
- `tests/test_projector.py` / `tests/test_planner.py`.

## The contract (ADR-0003 D1)

1. **Pure projector.** Deterministic from `(order_terms, context, control_state)`; uses
   `broker_market_value` for existing holdings, never `quantity × price`.
2. **Planner owns reduction.** No reduction lives downstream; the approver never re-sizes.
3. **`planner_projection` is evidence, not authority** — labelled as such; the approver recomputes and a
   mismatch is a hard reject (036).
4. **Fail-closed unpriced** — any `UNAVAILABLE` holding ⇒ no safe opening-risk projection.

## Definition of done

- Projector: incremental BP debit ≠ naïve notional; short/multiplier exposure uses `marketValue`;
  zero/negative/non-finite price rejected; an `UNAVAILABLE` holding ⇒ fail-closed (no opening projection).
- Planner: a reduced size is emitted as a fixed plan (no loop); decline-to-plan is representable; binds the
  digests + `policy.version`.
- `ruff` + `pyright` green; full suite green.
