---
to: builder
from: reviewer
id: 030-riskpolicy-riskcontrolstate
title: RiskPolicy (versioned Decimal) + RiskControlState
priority: normal
date: 2026-07-11
status: pending
guardrails: [money, data-integrity]
depends_on: [029]
adr: docs/design/adr/0003-pt5-rules-ledger.md
---

## Summary

Implement the **control-plane** decision inputs from **ADR-0003 Decision 5** (lanes L1/L3). Convert the
float `RiskLimits` to a versioned `Decimal` `RiskPolicy`, and add the `RiskControlState` container that
carries control-plane facts separate from the sealed `RiskContext`. **Follow ADR-0003 exactly.** STAGED
DRAFT — promote to `pending/` after 029 signs off.

**Blocked by:** 029 (needs the domain-model package settled).

## Deliverables

- `src/ibkr_trader/config.py`: replace `RiskLimits` (`config.py:45`, currently `float`) with a frozen
  `Decimal` `RiskPolicy`: `version: str`, `max_risk_per_trade`, `daily_realized_lockout_pct` (`pct_a`),
  `session_drawdown_pct` (`pct_d`), `leverage_cap`, `stop_loss_required`. Update `Settings.risk` + any
  reference. (`ConcentrationPolicy` is **not** added yet — Open.)
- `src/ibkr_trader/risk/control_state.py` (new): `RiskControlState(_Frozen)` = `policy: RiskPolicy`,
  `session_date: date`, `realized_daily_pnl: Decimal`, `observed_at: datetime`.
- `tests/test_config.py` / `tests/test_control_state.py`.

## The contract (ADR-0003 D5)

1. **Decimal before money math.** No `float` participates in any limit comparison.
2. **`mode` is NOT in `RiskControlState`** (ModeController owns it); **reservation/idempotency state is NOT
   here** (Execution Control owns it).
3. **`policy.version` is load-bearing** — it binds into plans + `ApprovalDecision` (staleness detection).
4. Defaults: `max_risk_per_trade=0.01`, `daily_realized_lockout_pct=0.03`; **`session_drawdown_pct` is a
   wider, paper-calibrated value — do NOT default it equal to `pct_a`** (ADR-0003 D7). Ship it with a
   deliberately conservative placeholder + a `# TODO(pct_d): paper-calibrate` marker; Control 2 enforcement
   is dark regardless (036).

## Definition of done

- `RiskPolicy` is frozen, all-`Decimal`, versioned; threshold-boundary tests use `Decimal`.
- `RiskControlState` excludes `mode` and reservation state (asserted by a field test).
- `ruff` + `pyright` green; full suite green.
