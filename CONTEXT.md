# Domain Context

This file records the shared domain language for the ibkr-auto-trader collab. Use these terms consistently in handoffs, implementation, tests, and review.

## Trading Safety Modules

### Risk & Sizing

The deep module that owns whether a strategy intent may become an executable order intent. Its public interface should stay narrow: callers submit intent plus context and receive an approval decision. Its implementation owns sizing math, risk limits, daily loss enforcement, leverage checks, causality checks, and audit evidence.

Approved design: Design B with a single facade.

```text
RiskSizing.decide(intent, context) -> ApprovalDecision
```

Internally, the facade delegates to a planner and approver:

```text
RiskPlanner.plan(intent, context) -> ProposedTradePlan
RiskApprover.approve(plan, context, rules_ledger) -> ApprovalDecision
```

### Risk Planner

The internal part of Risk & Sizing that translates a strategy intent into a proposed trade plan. It may reduce size to fit the per-trade risk budget, estimate stop loss impact, project exposure, and explain the sizing calculation. It never approves execution.

### Risk Approver

The internal part of Risk & Sizing that decides whether a proposed trade plan may cross the execution seam. It runs the rules ledger and produces the final approval decision.

### Rules Ledger

The explicit, auditable list of risk rules evaluated by the Risk Approver. Initial rules include paper-first mode, 1% max risk per trade, 3% daily loss limit, buying power, maintenance margin, leverage cap, stop-loss requirement, no direct strategy orders, causal data only, duplicate order prevention, concentration checks, taxable account guardrails, and audit completeness.

## Core Risk Objects

### Strategy Intent

A non-executable request from a strategy. It describes desired exposure, symbol, side, target kind, thesis, timestamps, expected edge if known, and proposed stop. It is not an order.

### Risk Context

The causal snapshot used by Risk & Sizing: trading mode, clock, account equity, Net Liquidation Value, buying power, margin, daily P&L, positions, open orders, market data, and risk config.

### Proposed Trade Plan

The planner output: estimated quantity, notional, entry price, stop price, max loss if stopped, resulting position, resulting exposure, resulting leverage, and sizing explanation. It is still not executable.

### Approval Decision

The approver output. It records whether the proposed trade plan is approved, reduced, rejected, or requires pause, plus rule results and audit evidence.

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
- New opening risk is rejected when daily loss reaches 3%.
- Live mode is rejected unless explicitly enabled by reviewed config and approved workflow state.
- Backtest, paper, and live paths use the same Risk & Sizing implementation.
- Market data must be causal at decision time.
- Stop-loss or reviewed equivalent risk control is mandatory.
- Rejections are audited with the same seriousness as approvals.
- Paper execution is the default execution path.
- Kill switch or paused mode prevents broker submission.
