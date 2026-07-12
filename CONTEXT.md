# Domain Context

This file records the shared domain language for the ibkr-auto-trader collab. Use these terms consistently in handoffs, implementation, tests, and review.

## Trading Safety Modules

### Risk & Sizing

The deep module that owns whether a strategy intent may become an executable order intent. Its public interface should stay narrow: callers submit intent plus context and receive an approval decision. Its implementation owns sizing math, risk limits, daily loss enforcement, leverage checks, causality checks, and audit evidence.

Approved design: Design B with a single facade.

```text
RiskSizing.decide(intent, context) -> ApprovalDecision
```

Internally, the facade delegates to a planner and approver (ADR-0003, recompute-for-authority):

```text
RiskSizing.decide(intent, context, control_state) -> ApprovalDecision
RiskPlanner.plan(intent, context, control_state) -> RiskPlan     # owns reduction/rounding/decline
RiskApprover.approve(plan, context, control_state) -> ApprovalDecision
```

The approver recomputes a `VerifiedProjection` from canonical order terms and runs the ledger over
`RiskEvaluation(plan, context, control_state, verified_projection)`; the plan's own `planner_projection`
is non-authoritative evidence. A planner-vs-verified mismatch is a hard reject + alarm, never a silent
repair. Control-plane facts (mode elsewhere, reviewed `RiskPolicy`, Eastern `session_date`, realized daily
P&L) live in `RiskControlState`, separate from the sealed causal `RiskContext`.

### Risk Planner

The internal part of Risk & Sizing that translates a strategy intent into a proposed trade plan. It may reduce size to fit the per-trade risk budget, estimate stop loss impact, project exposure, and explain the sizing calculation. It never approves execution.

### Risk Approver

The internal part of Risk & Sizing that decides whether a proposed trade plan may cross the execution seam. It runs the rules ledger and produces the final approval decision.

### Rules Ledger

The explicit, auditable list of risk rules evaluated by the Risk Approver — each a pure predicate over the
`RiskEvaluation` tuple. **Active v1 (ADR-0003):** max-risk-per-trade, daily-realized-opening-lockout
(Control 1), buying-power (incremental debit), leverage-cap (fail-closed on unpriced holdings),
stop-loss-required, causal-data-only (belt), unvalued-holding reduce-only, concentration (target-weight
fallback), maintenance-margin (degraded current-headroom), and session-drawdown-breaker (Control 2 —
detect+alarm active, enforcement dark until `pct_d` calibrated). **Relocated/out of the ledger:**
paper-first → Mode Controller/Execution Control; duplicate-prevention → Execution Control (durable
submission state machine); no-direct-strategy-orders → cross-cutting release gate; audit-completeness →
the mint is contingent on a durable audit write (PT-7/PT-12); taxable-account-guardrails → deferred.

## Core Risk Objects

### Strategy Intent

A non-executable request from a strategy. It describes desired exposure, symbol, side, target kind, thesis, timestamps, expected edge if known, and proposed stop. It is not an order.

### Risk Context

The **sealed causal snapshot** used by Risk & Sizing (ADR-0002/0003; `ASSEMBLER_AUTHORITY`-minted): `as_of`
(the decision seal), `account_observed_at`, Net Liquidation Value, buying power, maintenance margin, per-
instrument `prices`/`price_basis`/`data_as_of`, and a `holdings` map (`InstrumentId → HoldingValuation`
carrying quantity, `ValuationStatus`, `broker_market_value`, `mark_available_at`) from which `positions`
is derived. It carries **only decision-data facts** — trading mode, reviewed limits, Eastern session date,
and realized daily P&L are control-plane facts and live in `RiskControlState`, not here.

### Risk Control State

The control-plane decision input owned by PT-5, separate from the sealed `RiskContext`: the reviewed
versioned `RiskPolicy`, the canonical US/Eastern `session_date`, `realized_daily_pnl`, and `observed_at`.
Excludes `mode` (Mode Controller owns it) and reservation/idempotency state (Execution Control owns it).

### Proposed Trade Plan

The planner output: estimated quantity, notional, entry price, stop price, max loss if stopped, resulting position, resulting exposure, resulting leverage, and sizing explanation. It is still not executable.

### Approval Decision

The approver output. Verdict is `APPROVED | REJECTED` only (no `reduced`, no `requires pause` — the planner owns reduction; mode/operational pause live elsewhere). It records the verdict, plan/context digests, the full tuple of rule results, an audit ref, and the minted `ApprovedOrderIntent` if approved.

### Approved Order Intent

The only object that may cross into execution. It can exist only after Risk & Sizing approval.

### Execution Control

The deep module that owns whether an approved order intent may become an executable order. Its public interface should stay narrow: callers submit an approved order intent plus execution context and receive an execution decision. Its implementation owns trading mode evaluation, paper-first enforcement, live lockouts, kill switch behavior, duplicate prevention, account routing checks, adapter selection, and execution audit evidence.

Approved design: Design B with a single facade: ExecutionControl.submit(approved_order, context) -> ExecutionDecision. Internally, the facade delegates to ModeController.evaluate(context), ExecutionGate.authorize(approved_order, context, mode_decision), and ExecutionAdapter.send(executable_order).

### Mode Controller

The internal part of Execution Control that evaluates the current trading mode and operational state. It owns PAPER, LIVE_SMALL_TEST, LIVE, PAUSED, and KILL_SWITCHED mode semantics. It never sends orders.

### Execution Gate

The internal part of Execution Control that authorizes an approved order intent after mode evaluation. It checks that the order came from Risk & Sizing, routes only to allowed accounts, prevents duplicates through idempotency, enforces open-order constraints, and creates the executable order only when all execution rules pass.

### Execution Adapter

The external-system adapter that sends executable orders to a target environment. Initial adapters should include simulated execution and paper IBKR execution. A live IBKR adapter is future-only and unavailable unless reviewed live mode gates pass.

### Execution Decision

The Execution Control output. It records whether an approved order intent was authorized, rejected, or paused before broker submission, plus mode decision, routing decision, idempotency evidence, and audit evidence.

### Executable Order

The only object an execution adapter may send. It can exist only after Execution Control authorization.

## Safety Invariants

- Strategy output is never executable.
- Every executable order intent must pass through Risk & Sizing.
- Every executable order must pass through Execution Control.
- Execution adapters accept only executable orders.
- Approved risk per trade must be at most 1% of current equity.
- New opening risk is rejected when the **realized** daily loss reaches `pct_a` of session-start equity E0 (Control 1); strict risk-reducing exits remain eligible.
- A **session drawdown breaker** on total equity `(NLV−E0)/E0 ≤ −pct_d` de-risks the book (Control 2); it detects+alarms in v1 and enforces once `pct_d` is paper-calibrated.
- Live mode is rejected unless explicitly enabled by reviewed config and approved workflow state.
- Backtest, paper, and live paths use the same Risk & Sizing implementation.
- Market data must be causal at decision time.
- Stop-loss or reviewed equivalent risk control is mandatory.
- Rejections are audited with the same seriousness as approvals.
- Paper execution is the default execution path.
- Kill switch or paused mode prevents broker submission.
