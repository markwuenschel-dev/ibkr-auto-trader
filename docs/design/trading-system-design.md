# ibkr-auto-trader — Trading System Design

> Status: **Design (pre-implementation)**. Authoritative internal design for the trading system.
> Derived from [`../../CONTEXT.md`](../../CONTEXT.md) (domain model) and [`../../PROTOCOL.md`](../../PROTOCOL.md)
> (safety rules). **Nothing here places even a paper order until reviewed** per the collab process.

The design is a **two-deep-module** architecture with provenance/type seams: the strategy interface
emits `StrategyIntent`, and trusted execution code must enforce the `no-direct-strategy-orders`
release gate before broker submission. Every order flows Strategy → **Risk & Sizing** → **Execution
Control** → adapter. This internal safety layer is independent of, and complementary to, the
collab-kit review gate (see [`README.md`](./README.md) → two-gate model).

---

## 1. Repo layout **[DECIDED: Python 3.14 + uv, ib_async, pydantic]**

> The `pnpm` mention in handoff 001 is a stray JS artifact and is dropped — this is a pure-Python
> project.

```
ibkr-auto-trader/
  pyproject.toml             # uv-managed; deps: ib_async, pydantic, pandas, numpy, structlog
  src/trader/
    domain/                  # frozen pydantic models — the core objects (§2)
    risk/                    # Risk & Sizing deep module (§3)
      __init__.py            #   RiskSizing facade
      planner.py             #   RiskPlanner
      approver.py            #   RiskApprover
      rules.py               #   Rules Ledger (data-driven, §3.3)
    execution/               # Execution Control deep module (§4)
      __init__.py            #   ExecutionControl facade
      mode.py                #   ModeController
      gate.py                #   ExecutionGate
      adapters/
        base.py              #   ExecutionAdapter protocol
        simulated.py         #   default
        paper_ibkr.py        #   paper IBKR
        live_ibkr.py         #   FUTURE-ONLY, guarded
    strategy/                # emits StrategyIntent only (§5)
      base.py                #   Strategy / SignalSource protocols
      rebalancer.py          #   first impl
    ibkr/                    # connection, account snapshot → RiskContext
    audit/                   # structured JSON + human-readable decision log (§6.2)
    state/                   # SQLite: daily P&L, positions, idempotency keys (§6.1)
    app.py                   # main loop + heartbeat/watchdog + kill switch
  tests/                     # pytest incl. property-based tests for risk math (§7)
```

---

## 2. Core domain objects

All are **frozen** pydantic models. The *type* of an object encodes how far through the pipeline it
has legitimately traveled — this is a safety mechanism, not just typing hygiene.

| Object | Produced by | Executable? | Key fields |
|---|---|---|---|
| `StrategyIntent` | Strategy | **No** | symbol, side, target kind, desired exposure, thesis, timestamps, expected edge?, proposed stop |
| `RiskContext` | IBKR snapshot | n/a | mode, clock, equity, NLV, buying power, margin, daily P&L, positions, open orders, market data, risk config |
| `ProposedTradePlan` | RiskPlanner | **No** | qty, notional, entry, stop, max-loss-if-stopped, resulting position/exposure/leverage, sizing explanation |
| `ApprovalDecision` | RiskApprover | n/a | verdict (approved/reduced/rejected/pause), rule results, audit evidence |
| `ApprovedOrderIntent` | RiskSizing (post-approval) | **No** (gateable) | the plan + approval provenance; minted by Risk & Sizing in the trusted pipeline |
| `ExecutionDecision` | ExecutionControl | n/a | authorized/rejected/paused, mode decision, routing decision, idempotency evidence, audit evidence |
| `ExecutableOrder` | ExecutionGate (post-auth) | **Yes** | the ordinary adapter input; minted by ExecutionGate in the trusted pipeline |

**Constructibility rule:** the trusted pipeline mints `ApprovedOrderIntent` and `ExecutableOrder`
inside Risk & Sizing / Execution Control respectively (e.g. via a module-private token/factory).
This makes ordinary direct API use detectable, but it is not a hard in-process security boundary:
public authority imports and Pydantic `model_construct()` can bypass it. The current architecture
permits trusted execution-core code in process only; a future less-trusted plugin requires a
separate boundary design before it ships.

---

## 3. Risk & Sizing (deep module, single facade)

```
RiskSizing.decide(intent: StrategyIntent, context: RiskContext) -> ApprovalDecision
  internally:
    RiskPlanner.plan(intent, context)            -> ProposedTradePlan
    RiskApprover.approve(plan, context, ledger)  -> ApprovalDecision
```

### 3.1 RiskPlanner (never approves)
Translates intent → plan: sizing math, stop-loss impact, exposure/leverage projection, and a
human-readable **sizing explanation**. May *reduce* size to fit the per-trade risk budget. Produces
a `ProposedTradePlan` that is still non-executable.

### 3.2 RiskApprover (the gate)
Runs the **Rules Ledger** over `(plan, context)` and emits an `ApprovalDecision`. A rejection carries
the same audit weight as an approval.

### 3.3 Rules Ledger (data-driven) **[DECIDED: ADR-0003, PT-5 — recompute-for-authority]**

Each rule is a **pure predicate** over `RiskEvaluation(plan, context, control_state, verified_projection)`
→ `RuleResult(id, pass/fail, severity, evidence)`. **All rules are evaluated** (no short-circuit) for a
complete audit; a single blocking failure yields `REJECTED`. The verdict space is **`APPROVED | REJECTED`
only** — the *planner* owns all reduction/rounding/decline, and the *approver* recomputes a
**`VerifiedProjection`** from canonical order terms (the rules never read the planner's own figures). See
[ADR-0003](adr/0003-pt5-rules-ledger.md) for the full design; the v1 inventory:

| id | Rule (verdict = reject on fail) | Status |
|---|---|---|
| `max-risk-per-trade` | `verified.max_loss_if_stopped ≤ 1% · current NLV` | active |
| `daily-loss-lockout` | Control 1: `realized_daily_pnl ≤ −pct_a·E0` ⇒ `REDUCE_ONLY` (opening blocked, exits eligible) | active |
| `buying-power` | **incremental BP debit** ≤ `buying_power` (not naïve notional) | active (no Open) |
| `leverage-cap` | `verified.resulting_gross_leverage < 1.5x` over `Σ abs(broker_market_value)`; **fail-closed** on any unpriced holding | active (fail-closed) |
| `stop-loss-required` | valid stop present, correct side | active |
| `causal-data-only` (belt) | `data_as_of`/`account_observed_at ≤ as_of`, aligned basis/time, UTC; breach ⇒ reject + **alarm** | active |
| `unvalued-holding reduce-only` | `UNAVAILABLE` holding blocks opens/increases/zero-cross | active (NEW) |
| `concentration` | ceiling = strategy target-weight + drift (fallback); else **reject-as-unconfigured** | active (fallback) |
| `maintenance-margin` | conservative current-headroom check (long-equity-bounded, over-rejects) | active (degraded) |
| `session-drawdown-breaker` | Control 2: `(NLV−E0)/E0 ≤ −pct_d` ⇒ `REDUCE_ONLY` + escalate | detect+alarm active; **enforcement dark** until `pct_d` calibrated |
| `paper-first` | mode enforcement | **relocated → PT-8/PT-9** (`submission_allowed`) |
| `duplicate-prevention` | idempotency | **relocated → PT-9** durable submission state machine |
| `no-direct-strategy-orders` | provenance | **release gate** (process isolation, issuer containment, bypass tests) — not a ledger predicate |
| `audit-completeness` | decision fully auditable | **PT-7/PT-12** — mint is *contingent on a durable audit write* |
| `taxable-account-guardrails` | wash-sale / short-term-gain | **deferred** (not modelled at the paper milestone) |

Rules being pure predicates over the verified projection means each is unit-testable in isolation, the
ledger is auditable at a glance, and the safety gate never trusts the planner.

---

## 4. Execution Control (deep module, single facade)

```
ExecutionControl.submit(order: ApprovedOrderIntent, context: RiskContext) -> ExecutionDecision
  internally:
    ModeController.evaluate(context)                     -> ModeDecision
    ExecutionGate.authorize(order, context, mode_dec)    -> (authorized, ExecutableOrder | None)
    ExecutionAdapter.send(executable_order)              # only if authorized
```

### 4.1 ModeController
Owns `PAPER | LIVE_SMALL_TEST | LIVE | PAUSED | KILL_SWITCHED`. Live is rejected unless reviewed
config **and** approved workflow state enable it. `PAUSED`/`KILL_SWITCHED` block all broker
submission. Never sends orders.

### 4.2 ExecutionGate
After mode evaluation, authorizes an `ApprovedOrderIntent`: checks **provenance** (came from Risk &
Sizing), **routing** (only allowed accounts), **idempotency** (duplicate prevention via persisted
keys, §6.1), and **open-order constraints** — and only then mints the `ExecutableOrder`.

### 4.3 Adapters (`ExecutionAdapter` protocol)
`send(order: ExecutableOrder) -> Fill | Ack`. Implementations: `simulated` (default), `paper_ibkr`.
`live_ibkr` is future-only and unavailable unless reviewed live-mode gates pass. **Adapters accept
only `ExecutableOrder`** as their ordinary API contract; that does not make arbitrary in-process
bypass impossible.

---

## 5. Strategy layer **[DECIDED: rebalancer first, ML seam open]**

```
Strategy.propose(context: RiskContext) -> list[StrategyIntent]   # the seam
SignalSource.signals(context) -> list[Signal]                    # future ML/rule signals
```

- **First impl — `RebalancerStrategy`:** target weights **35% QQQ / 30% SPY / 20% VXUS / 10% DFSI /
  5% cash**, drift thresholds → `StrategyIntent`s. Fully rules-based.
- **ML seam:** a later `SignalStrategy` consumes a `SignalSource` behind the *same* `Strategy.propose`
  interface. The ML path must be causal (walk-forward / purged K-fold per PROTOCOL) and, like every
  path, clears Risk & Sizing. **The seam adds zero execution authority** — a strategy still emits
  only non-executable `StrategyIntent`s.

Rationale for building the seam now (the one place we abstract ahead of need): retrofitting a signal
source after strategy code is wired to order-adjacent logic is exactly where non-causal data leaks
in. The seam keeps the ML door open without ever widening what a strategy can *do*.

---

## 6. Cross-cutting concerns

### 6.1 State persistence — SQLite **[assumed; open Q]**
- **Daily realized P&L** (survives restart — answers handoff 001's loss-limit-across-restarts
  question).
- **Positions cache** and **idempotency keys** (duplicate-order prevention).
- SQLite chosen over flat JSON for transactional integrity on the P&L/idempotency writes that gate
  real risk. *(Open: revisit if flat-file simplicity is preferred.)*

### 6.2 Audit log
Every decision — **approve *and* reject** — writes a structured JSON record **and** a human-readable
line: *"At T, equity X, drift on QQQ Y → decided to buy Z shares because… expected impact…"*.
Mandatory per PROTOCOL, not optional. Rejections logged with equal seriousness.

### 6.3 Resilience
IBKR reconnection with backoff; heartbeat/watchdog on the main loop. On stall or disconnect → pause
safely, alert, and never leave risky open positions. Rate-limiting respects IBKR API limits.

### 6.4 Backtest harness
Re-imports the **exact** Risk & Sizing + Strategy modules used in paper/live — no train/serve skew.
Same `decide()` path for backtest, paper, and live.

---

## 7. Safety invariants → test map

Each invariant from `CONTEXT.md` maps to at least one planned test (property-based where math is
involved).

| Invariant | Test |
|---|---|
| Strategy output is non-executable | Type test: `Strategy.propose` return type is `list[StrategyIntent]`; trusted execution enforces the release gate |
| Every executable order intent passes Risk & Sizing | Provenance test: `ExecutionGate` rejects any `ApprovedOrderIntent` lacking planner provenance |
| Every executable order passes Execution Control | Adapter contract test: `send()` accepts `ExecutableOrder`; release-gate bypass tests cover the trusted-pipeline minter |
| Adapters accept only `ExecutableOrder` | Static + runtime type test on every adapter |
| ≤1% risk per trade | Property test: ∀ equity, ∀ stop distance → planner/approver never approves max-loss > 1% |
| New opening risk rejected at realized daily loss (Control 1) | Test: `realized_daily_pnl ≤ −pct_a·E0` → opening intent rejected, strict exit allowed (`REDUCE_ONLY`) |
| Session drawdown breaker on total equity (Control 2) | Test: `(NLV−E0)/E0 ≤ −pct_d` → detect+alarm (dark); once armed → `REDUCE_ONLY` + escalate; recompute is restart-stable |
| Live rejected unless explicitly enabled | Test: default config → `ModeController` rejects LIVE |
| Backtest/paper/live share Risk & Sizing | Test: all three call the same `RiskSizing.decide` object (no shadow impl) |
| Market data causal at decision time | Property test: any datum with ts > decision clock → `causal-data-only` rejects |
| Stop-loss mandatory | Test: intent without stop → `stop-loss-required` rejects |
| Rejections fully audited | Test: a rejected decision produces a complete audit record |
| Paper is default | Test: unset mode resolves to PAPER |
| Kill/paused blocks broker submission | Test: KILL_SWITCHED/PAUSED → `ExecutionControl.submit` never calls `adapter.send` |

---

## 8. First milestone (paper only, from handoff 001)

1. Robust connection + account snapshot (NLV, positions, buying power, margin) → `RiskContext`.
2. `RebalancerStrategy` maintaining target allocation with drift thresholds.
3. Risk module sizing + rejecting on 1% / daily-loss / margin violations.
4. Full decision logging (approve + reject).
5. Kill switch via config file / command.
6. Telegram alerts (large drift, paper order placed, daily summary, errors) via collab-kit's bridge.

---

## 9. Open questions (non-blocking)

- **State store:** SQLite (assumed) vs. flat JSON.
- **Partial fills / order-status tracking:** depth to model in the first paper loop.
- **Small-account gotchas:** odd lots, minimums — model now or record as a known risk.
